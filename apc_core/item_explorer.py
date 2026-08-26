import json
import os
import sqlite3
import stat
import threading
import hashlib
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


# Verified VB6 frmItemAdd/edit ↔ accepted-SQLite mapping.  Do not reuse the
# legacy `Family` field for Phyto Family: it is the scientific-family value.
_SNAPSHOT_COLUMNS = {
    "item_id": "Item ID", "description": "Description", "description_th": "Description TH",
    "type": "Type", "scientific_family": ("Scientific Family", "Family"), "usa_name": "USA Name",
    "quantity_per_piece": "QtyPerPcs", "phyto_family": "Phyto Family",
    "keset_family": "Keset Family", "thai_family": "Thai Family", "apc_group": "APC Group",
    "apc_team": "APC Team", "quantity_per_carton": "QtyPerCarton",
    "quantity_per_styrofoam": "QtyPerStyrofoam", "pack_sequence": "PackSeq",
    "quantity_per_bag": "PcsPerPack", "price_eu": "Price EU", "price_jp": "Price JP",
    "price_th": "Price TH",
}
_AUXILIARY_SNAPSHOT_COLUMNS = {
    "MainDB__PHYTO_GROUP": (
        "ITEM ID",
        {
            "phyto_family": "DESC SPP", "thai_family": "DESC TH SPP", "apc_team": "GROUP",
            "apc_group": "GROUP2", "keset_family": "KESIT GROUP", "pack_sequence": "Hardness",
        },
    ),
    "MainDB__PACKING": (
        "Item ID",
        {"quantity_per_carton": "Paper18", "quantity_per_styrofoam": "Styrofoam31"},
    ),
}
EDITABLE_FIELDS = (
    "description", "description_th", "usa_name", "type", "quantity_per_piece", "price_eu", "price_jp",
    "price_th", "phyto_family", "keset_family", "scientific_family", "thai_family", "apc_group",
    "apc_team", "quantity_per_carton", "quantity_per_styrofoam", "pack_sequence", "quantity_per_bag",
)
_TEXT_FIELDS = {"description", "description_th", "usa_name", "type", "phyto_family", "keset_family", "scientific_family", "thai_family", "apc_team"}
_DECIMAL_FIELDS = {"quantity_per_piece", "price_eu", "price_jp", "price_th"}
_INTEGER_FIELDS = {"quantity_per_carton", "quantity_per_styrofoam", "pack_sequence", "quantity_per_bag"}


def display_text(value: object) -> str:
    """Repair legacy TIS-620/CP874 bytes mis-decoded as Latin-1, without source writes."""
    text = str(value or "")
    if not any("\u00a0" <= char <= "\u00ff" for char in text):
        return text
    try:
        candidate = text.encode("latin1").decode("cp874")
    except UnicodeError:
        return text
    return candidate if any("\u0e00" <= char <= "\u0e7f" for char in candidate) else text


def _number_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if "." not in text:
        return text
    try:
        return format(Decimal(text), "f").rstrip("0").rstrip(".")
    except InvalidOperation:
        return text


class CoreStore:
    """App-owned local overrides and append-only audit; it never opens the source snapshot."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "apc_core.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        fields = ", ".join(f"{field} TEXT" for field in EDITABLE_FIELDS)
        self.connection.execute(f"CREATE TABLE IF NOT EXISTS item_overrides (item_id TEXT PRIMARY KEY, {fields})")
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(item_overrides)")}
        for field in EDITABLE_FIELDS:
            if field not in existing:
                self.connection.execute(f"ALTER TABLE item_overrides ADD COLUMN {field} TEXT")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS core_users (username TEXT PRIMARY KEY, role TEXT NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)))"
        )
        self.connection.executemany(
            "INSERT INTO core_users (username, role, active) VALUES (?, ?, 1) "
            "ON CONFLICT(username) DO UPDATE SET role = excluded.role, active = 1",
            (("YIM", "Editor"), ("WAT", "Editor"), ("BON", "Editor"), ("YA", "Editor"),
             ("BIAS", "Admin"), ("DERRICK", "Admin")),
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, "
            "changes_json TEXT NOT NULL, actor_username TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        activity_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(activity)")}
        if "actor_username" not in activity_columns:
            self.connection.execute("ALTER TABLE activity ADD COLUMN actor_username TEXT")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS item_backfill_quarantine (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "item_id TEXT NOT NULL, reason TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        quarantine_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(item_backfill_quarantine)")}
        if "active" not in quarantine_columns:
            self.connection.execute("ALTER TABLE item_backfill_quarantine ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS core_item_drafts (item_id TEXT PRIMARY KEY, original_item_id TEXT NOT NULL, "
            "item_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        # Canonical Items are app-owned.  The accepted VB6 SQLite file is input only:
        # it is never updated or used as the mutable record of truth after import.
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS core_items (item_id TEXT PRIMARY KEY, source_item_id TEXT, "
            "source_artifact_path TEXT, source_artifact_sha256 TEXT, imported_at TEXT, "
            "core_created INTEGER NOT NULL DEFAULT 0 CHECK(core_created IN (0, 1)), "
            "archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)), "
            f"{fields}, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self.connection.commit()

    def active_staff(self) -> list[tuple[str, str]]:
        return self.connection.execute(
            "SELECT username, role FROM core_users WHERE active = 1 ORDER BY username"
        ).fetchall()

    def require_active_actor(self, actor_username: object) -> str:
        if type(actor_username) is not str or not (1 <= len(actor_username) <= 32):
            raise ValueError("invalid actor")
        row = self.connection.execute(
            "SELECT username FROM core_users WHERE username = ? AND active = 1", (actor_username,)
        ).fetchone()
        if row is None:
            raise ValueError("invalid actor")
        return row[0]

    def override_for(self, item_id: str) -> dict[str, str]:
        row = self.connection.execute(
            f"SELECT {', '.join(EDITABLE_FIELDS)} FROM item_overrides WHERE item_id = ?", (item_id,)
        ).fetchone()
        return {} if row is None else {field: row[index] for index, field in enumerate(EDITABLE_FIELDS) if row[index] is not None}

    def save(self, item_id: str, changes: dict[str, str], actor_username: str | None = None) -> None:
        columns = ["item_id", *changes]
        assignments = ", ".join(f"{field} = excluded.{field}" for field in changes)
        placeholders = ", ".join("?" for _ in columns)
        with self.connection:
            self.connection.execute(
                f"INSERT INTO item_overrides ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(item_id) DO UPDATE SET {assignments}", [item_id, *changes.values()]
            )
            self.connection.execute("INSERT INTO activity (item_id, changes_json, actor_username) VALUES (?, ?, ?)",
                                    (item_id, json.dumps(changes, ensure_ascii=False, sort_keys=True), actor_username))

    def activity_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    def override_ids(self) -> set[str]:
        return {row[0] for row in self.connection.execute("SELECT item_id FROM item_overrides")}

    def canonical_for(self, item_id: str) -> dict[str, object] | None:
        columns = ["item_id", "source_item_id", "source_artifact_path", "source_artifact_sha256", "imported_at",
                   "core_created", "archived", *EDITABLE_FIELDS]
        row = self.connection.execute(f"SELECT {', '.join(columns)} FROM core_items WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        item = dict(zip(columns, row))
        item["core_created"] = bool(item["core_created"])
        item["archived"] = bool(item["archived"])
        for field in EDITABLE_FIELDS:
            item[field] = item[field] or ""
        item["family"] = item["phyto_family"]
        return item

    def canonical_items(self, *, include_archived: bool = False) -> list[dict[str, object]]:
        where = "" if include_archived else " WHERE archived = 0"
        ids = [row[0] for row in self.connection.execute("SELECT item_id FROM core_items" + where + " ORDER BY item_id")]
        return [item for item_id in ids if (item := self.canonical_for(item_id)) is not None]

    def import_canonical(self, item: dict[str, str], *, artifact_path: str, artifact_sha256: str) -> bool:
        """Insert one accepted source row once; never overwrite an existing Core record."""
        if self.canonical_for(item["item_id"]) is not None:
            return False
        legacy = self.override_for(item["item_id"])
        values = {field: legacy.get(field, item[field]) for field in EDITABLE_FIELDS}
        columns = ["item_id", "source_item_id", "source_artifact_path", "source_artifact_sha256", "imported_at", "core_created", "archived", *EDITABLE_FIELDS]
        with self.connection:
            self.connection.execute(
                f"INSERT INTO core_items ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [item["item_id"], item["item_id"], artifact_path, artifact_sha256, "CURRENT_TIMESTAMP", 0, 0,
                 *[values[field] for field in EDITABLE_FIELDS]],
            )
            self.connection.execute("UPDATE core_items SET imported_at = CURRENT_TIMESTAMP WHERE item_id = ?", (item["item_id"],))
        return True

    def create_canonical(self, item: dict[str, str], actor_username: str) -> dict[str, object]:
        if self.canonical_for(item["item_id"]) is not None:
            raise ValueError("duplicate item id")
        columns = ["item_id", "source_item_id", "core_created", "archived", *EDITABLE_FIELDS]
        with self.connection:
            self.connection.execute(
                f"INSERT INTO core_items ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [item["item_id"], None, 1, 0, *[item[field] for field in EDITABLE_FIELDS]],
            )
            self.connection.execute("INSERT INTO activity (item_id, changes_json, actor_username) VALUES (?, ?, ?)",
                                    (item["item_id"], json.dumps({"created": True}, sort_keys=True), actor_username))
        return self.canonical_for(item["item_id"]) or {}

    def update_canonical(self, item_id: str, changes: dict[str, str], actor_username: str) -> dict[str, object]:
        if self.canonical_for(item_id) is None:
            raise ValueError("unknown item")
        with self.connection:
            self.connection.execute(f"UPDATE core_items SET {', '.join(f'{field} = ?' for field in changes)}, updated_at = CURRENT_TIMESTAMP WHERE item_id = ?",
                                    [*changes.values(), item_id])
            self.connection.execute("INSERT INTO activity (item_id, changes_json, actor_username) VALUES (?, ?, ?)",
                                    (item_id, json.dumps(changes, ensure_ascii=False, sort_keys=True), actor_username))
        return self.canonical_for(item_id) or {}

    def archive_canonical(self, item_id: str, actor_username: str) -> dict[str, object]:
        if self.canonical_for(item_id) is None:
            raise ValueError("unknown item")
        with self.connection:
            self.connection.execute("UPDATE core_items SET archived = 1, updated_at = CURRENT_TIMESTAMP WHERE item_id = ?", (item_id,))
            self.connection.execute("INSERT INTO activity (item_id, changes_json, actor_username) VALUES (?, ?, ?)",
                                    (item_id, json.dumps({"archived": True}, sort_keys=True), actor_username))
        return self.canonical_for(item_id) or {}

    def draft_items(self) -> list[dict[str, object]]:
        return [json.loads(row[0]) for row in self.connection.execute("SELECT item_json FROM core_item_drafts ORDER BY item_id")]

    def save_draft(self, item: dict[str, object], actor_username: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO core_item_drafts (item_id, original_item_id, item_json) VALUES (?, ?, ?)",
                (item["item_id"], item["original_item_id"], json.dumps(item, ensure_ascii=False, sort_keys=True)),
            )
            self.connection.execute("INSERT INTO activity (item_id, changes_json, actor_username) VALUES (?, ?, ?)",
                                    (item["item_id"], json.dumps({"duplicate_of": item["original_item_id"]}, ensure_ascii=False), actor_username))

    def update_draft(self, item_id: str, changes: dict[str, str], actor_username: str) -> dict[str, object] | None:
        for item in self.draft_items():
            if item["item_id"] == item_id:
                item.update(changes); item["family"] = item.get("phyto_family", "")
                with self.connection:
                    self.connection.execute("UPDATE core_item_drafts SET item_json = ? WHERE item_id = ?",
                                            (json.dumps(item, ensure_ascii=False, sort_keys=True), item_id))
                    self.connection.execute("INSERT INTO activity (item_id, changes_json, actor_username) VALUES (?, ?, ?)",
                                            (item_id, json.dumps(changes, ensure_ascii=False, sort_keys=True), actor_username))
                return item
        return None

    def backfill_missing(self, item_id: str, values: dict[str, str]) -> int:
        """Add only absent Core fields; existing local override values always win."""
        existing = self.override_for(item_id)
        missing = {field: value for field, value in values.items() if value and field not in existing}
        preserved = sum(1 for field, value in values.items() if field in existing and existing[field] != value)
        if missing:
            self.save(item_id, missing)
        return preserved

    def clear_active_quarantines(self) -> None:
        with self.connection:
            self.connection.execute("UPDATE item_backfill_quarantine SET active = 0 WHERE active = 1")

    def quarantine(self, item_id: str, reason: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO item_backfill_quarantine (item_id, reason, active) VALUES (?, ?, 1)", (item_id, reason)
            )

    def quarantined_item_ids(self) -> set[str]:
        return {row[0] for row in self.connection.execute(
            "SELECT DISTINCT item_id FROM item_backfill_quarantine "
            "WHERE active = 1 AND reason <> 'unmatched_override'"
        )}

    def has_active_quarantines(self) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM item_backfill_quarantine WHERE active = 1 LIMIT 1"
        ).fetchone() is not None

    def quarantine_reasons(self) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT reason FROM item_backfill_quarantine ORDER BY id"
        )]

    def close(self) -> None:
        self.connection.close()


class ItemExplorer:
    def __init__(self, source_path: Path, data_dir: Path | None = None):
        self.source_path = Path(source_path)
        self.data_dir = Path(data_dir or os.environ.get("APC_CORE_DATA_DIR", "state"))
        self._store: CoreStore | None = None
        descriptor = os.open(self.source_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self._initialize_from_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def from_open_descriptor(cls, descriptor: int, source_path: Path, data_dir: Path | None = None) -> "ItemExplorer":
        explorer = cls.__new__(cls)
        explorer.source_path, explorer.data_dir, explorer._store = Path(source_path), Path(data_dir or os.environ.get("APC_CORE_DATA_DIR", "state")), None
        explorer._initialize_from_descriptor(descriptor)
        return explorer

    def _initialize_from_descriptor(self, descriptor: int) -> None:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("item explorer source must be a regular SQLite file")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True, check_same_thread=False)
        self._connection.execute("PRAGMA query_only = ON")
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as source:
            source.seek(0)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        # Duplicated descriptors share a file offset.  Callers such as the
        # confirm-gated importer retain this descriptor for a later certified
        # copy, so leave it at the beginning after inspecting the snapshot.
        os.lseek(descriptor, 0, os.SEEK_SET)
        self._source_sha256 = digest.hexdigest()
        self._source_columns = {row[1] for row in self._connection.execute('PRAGMA table_info("MainDB__ITEM")')}
        if "Item ID" not in self._source_columns:
            raise ValueError("item explorer source lacks item ID")

    def _local_store(self) -> CoreStore:
        if self._store is None:
            self._store = CoreStore(self.data_dir)
        return self._store

    def close(self) -> None:
        with self._lock:
            self._connection.close()
            if self._store is not None:
                self._store.close()

    def _auxiliary_item_values(self) -> tuple[dict[str, dict[str, object]], set[str]]:
        values: dict[str, dict[str, object]] = {}
        conflicts: set[str] = set()
        with self._lock:
            for table, (id_name, field_map) in _AUXILIARY_SNAPSHOT_COLUMNS.items():
                present = self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()
                if present is None:
                    continue
                columns = {row[1].casefold(): row[1] for row in self._connection.execute(f'PRAGMA table_info("{table}")')}
                id_column = columns.get(id_name.casefold())
                selected = {field: columns[name.casefold()] for field, name in field_map.items() if name.casefold() in columns}
                if id_column is None or not selected:
                    continue
                select_columns = [id_column, *selected.values()]
                select_clause = ", ".join('"' + column + '"' for column in select_columns)
                rows = self._connection.execute(f'SELECT {select_clause} FROM "{table}"').fetchall()
                seen: set[str] = set()
                for row in rows:
                    item_id = display_text(row[0]).strip()
                    if not item_id:
                        continue
                    if item_id in seen:
                        conflicts.add(item_id)
                        continue
                    seen.add(item_id)
                    values.setdefault(item_id, {}).update(dict(zip(selected, row[1:])))
        return values, conflicts

    def _baseline_items(self) -> list[dict[str, str]]:
        source_by_casefold = {column.casefold(): column for column in self._source_columns}
        available = {
            field: next((source_by_casefold.get(column.casefold())
                         for column in (columns if type(columns) is tuple else (columns,))
                         if column.casefold() in source_by_casefold), None)
            for field, columns in _SNAPSHOT_COLUMNS.items()
        }
        available = {field: column for field, column in available.items() if column is not None}
        selected = ", ".join(f'"{column}"' for column in available.values())
        with self._lock:
            rows = self._connection.execute(f'SELECT {selected} FROM "MainDB__ITEM" ORDER BY "Item ID"').fetchall()
        auxiliary_values, auxiliary_conflicts = self._auxiliary_item_values()
        items = []
        for row in rows:
            values = dict(zip(available, row))
            item = {"item_id": display_text(values["item_id"])}
            for field in EDITABLE_FIELDS:
                value = values.get(field, "")
                item[field] = _number_text(value) if field in _DECIMAL_FIELDS | _INTEGER_FIELDS else display_text(value)
            for field, value in auxiliary_values.get(item["item_id"], {}).items():
                item[field] = _number_text(value) if field in _DECIMAL_FIELDS | _INTEGER_FIELDS else display_text(value)
            if item["item_id"] in auxiliary_conflicts:
                item["_auxiliary_conflict"] = "duplicate_auxiliary_item_id"
            item["family"] = item["phyto_family"]
            items.append(item)
        return items

    def _merged_item(self, item: dict[str, str]) -> dict[str, str]:
        merged = {field: value for field, value in item.items() if not field.startswith("_")}
        if self._store is not None:
            merged.update(self._store.override_for(item["item_id"]))
        merged["family"] = merged["phyto_family"]
        return merged

    def item_types(self) -> tuple[str, ...]:
        """Return the closed, display-safe Type values from the accepted snapshot."""
        if "Type" not in self._source_columns:
            return ()
        with self._lock:
            values = [row[0] for row in self._connection.execute('SELECT DISTINCT "Type" FROM "MainDB__ITEM"')]
        return tuple(sorted({display_text(value).strip() for value in values
                             if type(value) is str and 0 < len(display_text(value).strip()) <= 500}))

    def filter_options(self) -> dict[str, list[str]]:
        """Options are sourced from visible Core-owned records."""
        items = self._local_store().canonical_items()
        option = lambda field: sorted({item[field].strip() for item in items if item.get(field, "").strip()})
        return {"type_options": list(self.item_types()), "family_options": option("phyto_family"),
                "group_options": option("apc_group"), "pack_sequence_options": ["1", "2", "3", "4", "5"]}

    def search(self, query: str = "", limit: int = 50, offset: int = 0, *, item_id_prefix: str = "",
               description: str = "", family: str = "", group: str = "", item_type: str = "",
               pack_sequence: str = "") -> dict:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        # Runtime reads operate on Core-owned canonical records.  A newly
        # accepted artifact has no records until its first local read, so
        # initialize those records before applying the query.  An active
        # quarantine records a completed failed import; do not re-audit the
        # same rejected rows just because a caller reads them again.
        store = self._local_store()
        if not store.canonical_items(include_archived=True) and not store.has_active_quarantines():
            self.backfill_from_snapshot()
        term, prefix, description = (query or "").strip().casefold(), (item_id_prefix or "").strip().casefold(), (description or "").strip().casefold()
        filters = self.filter_options()
        for value, choices in ((family, filters["family_options"]), (group, filters["group_options"]),
                               (item_type, filters["type_options"]), (pack_sequence, filters["pack_sequence_options"])):
            if value and value not in choices:
                return {"total": 0, "limit": limit, "offset": offset, **filters, "items": []}
        items = self._local_store().canonical_items()
        def matches(item: dict[str, object]) -> bool:
            text = lambda key: str(item.get(key, ""))
            return ((not term or any(term in text(key).casefold() for key in ("item_id", "description", "description_th"))) and
                    (not prefix or text("item_id").casefold().startswith(prefix)) and
                    (not description or description in text("description").casefold() or description in text("description_th").casefold()) and
                    (not family or text("phyto_family") == family) and (not group or text("apc_group") == group) and
                    (not item_type or text("type") == item_type) and (not pack_sequence or text("pack_sequence") == pack_sequence))
        items = [item for item in items if matches(item)]
        items.sort(key=lambda item: str(item["item_id"]))
        return {"total": len(items), "limit": limit, "offset": offset, **filters, "items": items[offset:offset + limit]}

    def duplicate(self, item_id: str, actor_username: object = None) -> dict[str, object]:
        actor = self._local_store().require_active_actor(actor_username)
        self.backfill_from_snapshot()
        source = self._local_store().canonical_for(item_id)
        if source is None:
            raise ValueError("unknown item")
        existing = {item["item_id"] for item in self._local_store().canonical_items(include_archived=True)}
        index = 1
        new_id = f"{item_id}-C{index:03d}"
        while new_id in existing:
            index += 1; new_id = f"{item_id}-C{index:03d}"
        draft = {field: str(source.get(field, "")) for field in EDITABLE_FIELDS}
        draft.update({"item_id": new_id, "family": str(source.get("phyto_family", "")), "original_item_id": item_id,
                      "core_created": True, "source_label": "Core-created"})
        # This is intentionally a client-editable proposal only: no DB/audit write
        # occurs until create() receives an explicit unique item ID.
        return draft

    def _clean_changes(self, changes: dict[str, object], existing_type: str) -> dict[str, str]:
        if set(changes) - (set(EDITABLE_FIELDS) | {"family", "item_id"}) or "item_id" in changes:
            raise ValueError("unsupported item field")
        cleaned = {}
        for field, value in changes.items():
            field = "phyto_family" if field == "family" else field
            if type(value) is not str or len(value.strip()) > 500:
                raise ValueError("invalid item field")
            value = value.strip()
            if field in _TEXT_FIELDS:
                if not value:
                    raise ValueError("invalid item field")
                if field == "type" and value not in self.item_types() and value != existing_type:
                    raise ValueError("invalid item field")
            elif field in _DECIMAL_FIELDS:
                try:
                    number = Decimal(value)
                except InvalidOperation:
                    raise ValueError("invalid item field") from None
                if not number.is_finite() or number < 0:
                    raise ValueError("invalid item field")
            elif field in _INTEGER_FIELDS:
                if not value.isdecimal() or int(value) < 0 or (field == "pack_sequence" and not 1 <= int(value) <= 5):
                    raise ValueError("invalid item field")
            elif field == "apc_group" and value not in {"", "A", "B"}:
                raise ValueError("invalid item field")
            cleaned[field] = value
        return cleaned

    def edit(self, item_id: str, changes: dict[str, object], actor_username: object = None) -> dict[str, object]:
        if type(item_id) is not str or not item_id or type(changes) is not dict or not changes:
            raise ValueError("invalid item edit")
        actor = self._local_store().require_active_actor(actor_username)
        self.backfill_from_snapshot()
        existing = self._local_store().canonical_for(item_id)
        if existing is None:
            raise ValueError("unknown item")
        with self._lock:
            cleaned = self._clean_changes(changes, str(existing["type"]))
            return self._local_store().update_canonical(item_id, cleaned, actor)

    def create(self, item: dict[str, object], actor_username: object = None) -> dict[str, object]:
        """Persist a new Core-created record only after explicit user Create."""
        if type(item) is not dict or type(item.get("item_id")) is not str:
            raise ValueError("invalid item create")
        actor = self._local_store().require_active_actor(actor_username)
        item_id = item["item_id"].strip()
        if not item_id or len(item_id) > 500:
            raise ValueError("invalid item create")
        self.backfill_from_snapshot()
        if self._local_store().canonical_for(item_id) is not None:
            raise ValueError("duplicate item id")
        raw = {field: item.get(field, "") for field in EDITABLE_FIELDS}
        # A duplicate can legitimately retain blank optional legacy fields. Validate
        # supplied/nonblank fields exactly as edits do, while requiring the identity
        # fields a newly created Item must have.
        if any(type(raw[field]) is not str or not raw[field].strip() for field in ("description", "description_th", "type")):
            raise ValueError("invalid item create")
        cleaned = self._clean_changes({field: value for field, value in raw.items() if value != ""}, str(raw["type"]))
        created = {field: cleaned.get(field, "") for field in EDITABLE_FIELDS}
        created["item_id"] = item_id
        return self._local_store().create_canonical(created, actor)

    def archive(self, item_id: str, actor_username: object = None) -> dict[str, object]:
        if type(item_id) is not str or not item_id:
            raise ValueError("invalid item archive")
        actor = self._local_store().require_active_actor(actor_username)
        self.backfill_from_snapshot()
        return self._local_store().archive_canonical(item_id, actor)

    def _backfill_invalid_fields(self, item: dict[str, str]) -> list[str]:
        invalid = []
        for field in EDITABLE_FIELDS:
            value = item[field]
            if not value:
                continue
            if field in _DECIMAL_FIELDS:
                try:
                    valid = Decimal(value).is_finite() and Decimal(value) >= 0
                except InvalidOperation:
                    valid = False
            elif field in _INTEGER_FIELDS:
                valid = value.isdecimal() and int(value) >= 0
                if field == "pack_sequence":
                    valid = valid and 1 <= int(value) <= 5
            elif field == "apc_group":
                valid = value in {"A", "B"}
            else:
                valid = len(value) <= 500
            if not valid:
                invalid.append(field)
        return invalid

    def backfill_from_snapshot(self) -> dict[str, int]:
        """Fill only missing Core fields from an accepted snapshot; never replace overrides.

        Ambiguous, unmatched, and out-of-range rows are recorded in app-owned quarantine
        instead of becoming visible Core data.
        """
        store = self._local_store()
        store.clear_active_quarantines()
        rows = self._baseline_items()
        counts = {"accepted": 0, "duplicate": 0, "unmatched": 0, "out_of_range": 0, "preserved": 0}
        ids = [row["item_id"] for row in rows]
        source_item_ids = {item_id for item_id in ids if item_id}
        duplicate_ids = {item_id for item_id in source_item_ids if ids.count(item_id) > 1}
        for row in rows:
            item_id = row["item_id"]
            if not item_id:
                store.quarantine("", "unmatched_item_id")
                counts["unmatched"] += 1
            elif item_id in duplicate_ids:
                store.quarantine(item_id, "duplicate_item_id")
                counts["duplicate"] += 1
            elif "_auxiliary_conflict" in row:
                store.quarantine(item_id, row["_auxiliary_conflict"])
                counts["duplicate"] += 1
            else:
                invalid = self._backfill_invalid_fields(row)
                if invalid:
                    store.quarantine(item_id, f"out_of_range:{','.join(invalid)}")
                    counts["out_of_range"] += 1
                else:
                    existing = store.canonical_for(item_id)
                    if existing is not None:
                        counts["preserved"] += sum(
                            1 for field in EDITABLE_FIELDS if row[field] and existing.get(field) != row[field]
                        )
                    else:
                        counts["preserved"] += sum(
                            1 for field in EDITABLE_FIELDS
                            if row[field] and field in store.override_for(item_id) and store.override_for(item_id)[field] != row[field]
                        )
                    store.import_canonical(row, artifact_path=str(self.source_path), artifact_sha256=self._source_sha256)
                    counts["accepted"] += 1
        for item_id in sorted(store.override_ids() - source_item_ids):
            store.quarantine(item_id, "unmatched_override")
            counts["unmatched"] += 1
        return counts

    def activity_count(self) -> int:
        with self._lock:
            return self._local_store().activity_count()


def _menu_html() -> str:
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core</title><style>:root{--ink:#202124;--muted:#6e737b;--line:#e6e6e8;--canvas:#f5f5f7;--paper:#fff;--blue:#0071e3}*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:900px;margin:auto;padding:56px 24px}.brand{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--blue);margin-bottom:18px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{min-height:156px;border:1px solid var(--line);border-radius:20px;background:var(--paper);padding:22px;text-decoration:none;color:inherit;display:flex;flex-direction:column;justify-content:space-between}.card h2{font-size:21px;margin:0}.card:hover,.card:focus-visible{border-color:#a9cfee;box-shadow:0 10px 24px rgba(0,113,227,.12);transform:translateY(-2px);transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}.card p{color:var(--muted)}.open{font-weight:600;color:var(--blue)}.soon{opacity:.72}.label{font-size:12px}@media(max-width:620px){.grid{grid-template-columns:1fr}}</style><body><main class="shell"><div class="brand">APC Core</div><section class="grid" aria-label="APC Core modules"><a class="card" href="items/"><div><h2>Items</h2><p>Search and inspect the item catalogue.</p></div><span class="open">Open Item Explorer →</span></a><div class="card soon"><div><h2>Orders</h2><p>Order work will appear here.</p></div><span class="label">Coming soon</span></div><div class="card soon"><div><h2>Customers</h2><p>Customer context will appear here.</p></div><span class="label">Coming soon</span></div><div class="card soon"><div><h2>Shipments</h2><p>Shipment tracking will appear here.</p></div><span class="label">Coming soon</span></div><div class="card soon"><div><h2>Activity</h2><p>Shared activity will appear here.</p></div><span class="label">Coming soon</span></div></section></main></body></html>"""


def _item_explorer_html() -> str:
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core · Item Explorer</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#faf7f2;--paper:#fffdfa;--accent:#1d6b57}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}.shell{max-width:1440px;margin:auto;padding:28px}.back{color:var(--accent);font-weight:700;text-decoration:none}.top{margin:16px 0}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 470px;border:1px solid var(--line);border-radius:16px;background:var(--paper)}.queue{padding:18px;min-width:0}.detail{position:sticky;top:20px;align-self:start;border-left:1px solid var(--line);padding:20px;background:#fdfbf8}input,select,button{padding:10px 11px;border:1px solid var(--line);border-radius:9px;font:inherit}input,select{width:100%}button{cursor:pointer;background:#fff}.toolbar{position:sticky;top:0;z-index:2;margin:0 -18px 12px;padding:12px 18px;background:#fffdfa;border-bottom:1px solid var(--line);display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:center}.advanced-search{margin:0 0 12px;border:1px solid var(--line);border-radius:9px;padding:0 10px}.advanced-search summary{cursor:pointer;padding:10px 0;font-weight:700;color:var(--accent)}.filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:0 0 12px}.save{background:var(--accent);color:#fff;font-weight:700}.chips{display:flex;gap:6px;flex-wrap:wrap;margin:9px 0}.chip{background:#eaf4ee;color:#174d3e;border-radius:999px;padding:4px 8px;font-size:12px}.status,.thai{color:var(--muted);font-size:12px}.copy-id{font-size:11px;margin-left:7px;padding:3px 6px}table{border-collapse:collapse;width:100%;text-align:left;table-layout:fixed}th{position:sticky;top:62px;z-index:1;background:var(--paper);font-size:11px;color:var(--muted);text-transform:uppercase;padding:8px 10px;border-bottom:1px solid var(--line)}th:first-child{width:58%}td{padding:8px 10px;border-bottom:1px solid #eeeae4;vertical-align:top;overflow-wrap:anywhere}tbody tr:nth-child(even){background:#fcfaf6}tr{cursor:pointer}tr:hover,tr:focus{background:#eaf4ee;outline:2px solid #b9dbcf;outline-offset:-2px}.empty{padding:28px;text-align:center;color:var(--muted)}.form-section{border:0;border-top:1px solid var(--line);margin:13px 0 0;padding:13px 0 0}.form-section legend{font-size:11px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--accent)}.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.wide{grid-column:1/-1}.edit-form label{display:block;color:var(--muted);font-size:12px;font-weight:700}.edit-form label input,.edit-form label select{margin-top:4px}.locked{background:#f0f0ef}.actions{display:flex;gap:8px;margin-top:14px}@media(max-width:900px){.shell{padding:16px}.workspace{grid-template-columns:1fr}.detail{position:static;border-left:0;border-top:1px solid var(--line)}.toolbar{margin:0 -18px 12px;grid-template-columns:1fr auto}.toolbar strong{grid-column:1/-1}.filters,.field-grid{grid-template-columns:1fr}}</style><body><main class=\"shell\"><a class=\"back\" href=\"../\">← APC Core</a><header class=\"top\"><div class=\"status\">APC Core</div><h1>Item Explorer</h1><div id=\"identity\" class=\"status\" aria-live=\"polite\"></div><button id=\"change-user\" type=\"button\">Change user</button></header><section class=\"workspace\"><div class=\"queue\"><div class=\"toolbar\"><input id=\"search\" type=\"search\" autocomplete=\"off\" placeholder=\"Search item ID, English or Thai description\" aria-label=\"Search items\"><button id=\"clear\" type=\"button\">Clear all</button><strong id=\"result-count\">0 results</strong></div><details class=\"advanced-search\"><summary>Advanced Search</summary><div class=\"filters\"><label>Item ID prefix<input id=\"item-id\" list=\"item-id-options\" autocomplete=\"off\" placeholder=\"Start typing ID\"></label><label>Description / Thai description<input id=\"description\" type=\"search\" autocomplete=\"off\"></label><label>Type<select id=\"type\"><option value=\"\">All types</option></select></label><label>Family<select id=\"family\"><option value=\"\">All families</option></select></label><label>Group<select id=\"group\"><option value=\"\">All groups</option></select></label><label>Pack Sequence<select id=\"pack\"><option value=\"\">All sequences</option></select></label></div></details><datalist id=\"item-id-options\"></datalist><div id=\"chips\" class=\"chips\" aria-label=\"Active filters\"></div><table><thead><tr><th>Item ID / Description</th><th>Type</th><th>Family</th></tr></thead><tbody id=\"rows\"></tbody></table><div id=\"empty\" class=\"empty\" hidden>No items match these filters. Clear all or try another search.</div><button id=\"more\" type=\"button\" class=\"save\" hidden>Load more</button></div><aside class=\"detail\" id=\"detail\" aria-live=\"polite\"><p class=\"status\">Select an item to edit it locally.</p></aside></section></main><template id=\"edit-template\"><form class=\"edit-form\"><fieldset class=\"form-section\"><legend>Item identity</legend><div class=\"field-grid\"><label class=\"wide\">Item ID<input name=\"item_id\" readonly class=\"locked\"></label><label>Description<input name=\"description\"></label><label>Description (Thai)<input name=\"description_th\"></label><label>USA Name<input name=\"usa_name\"></label><label>Type<input name=\"type\" list=\"type-options\" autocomplete=\"off\"></label></div></fieldset><fieldset class=\"form-section\"><legend>Families &amp; Group</legend><div class=\"field-grid\"><label>Phyto Family<input name=\"phyto_family\" list=\"phyto-family-options\" autocomplete=\"off\"></label><label>Keset Family<input name=\"keset_family\" list=\"keset-family-options\" autocomplete=\"off\"></label><label>Scientific Family<input name=\"scientific_family\" list=\"scientific-family-options\" autocomplete=\"off\"></label><label>Thai Family<input name=\"thai_family\" list=\"thai-family-options\" autocomplete=\"off\"></label><label class=\"wide\">APC Team<input name=\"apc_team\" list=\"apc-team-options\" autocomplete=\"off\"></label><label>APC Group<span class=\"radio-group\"><label><input name=\"apc_group\" type=\"radio\" value=\"A\"> A</label><label><input name=\"apc_group\" type=\"radio\" value=\"B\"> B</label><label><input name=\"apc_group\" type=\"radio\" value=\"\"> None</label></span></label></div></fieldset><fieldset class=\"form-section\"><legend>Packaging</legend><div class=\"field-grid\"><label>Quantity per piece<input name=\"quantity_per_piece\" type=\"number\" min=\"0\" step=\"any\"></label><label>Europe Price<input name=\"price_eu\" type=\"number\" min=\"0\" step=\"any\"></label><label>Japan Price<input name=\"price_jp\" type=\"number\" min=\"0\" step=\"any\"></label><label>Thailand Price<input name=\"price_th\" type=\"number\" min=\"0\" step=\"any\"></label><label>Quantity per Carton<input name=\"quantity_per_carton\" type=\"number\" min=\"0\" step=\"1\"></label><label>Quantity per Styrofoam<input name=\"quantity_per_styrofoam\" type=\"number\" min=\"0\" step=\"1\"></label><label>Pack sequence<select name=\"pack_sequence\"><option value=\"1\">1</option><option value=\"2\">2</option><option value=\"3\">3</option><option value=\"4\">4</option><option value=\"5\">5</option></select></label><label>Quantity per Bag<input name=\"quantity_per_bag\" type=\"number\" min=\"0\" step=\"1\"></label></div></fieldset><div class=\"status\" data-unsaved>Saved locally.</div><div class=\"actions\"><button class=\"save\" type=\"submit\">Save changes</button><button type=\"button\" data-cancel>Cancel</button><button type=\"button\" data-duplicate>Duplicate Item</button><button type=\"button\" data-archive>Archive item</button></div></form></template><datalist id=\"type-options\"></datalist><datalist id=\"phyto-family-options\"></datalist><datalist id=\"keset-family-options\"></datalist><datalist id=\"scientific-family-options\"></datalist><datalist id=\"thai-family-options\"></datalist><datalist id=\"apc-team-options\"></datalist><datalist id=\"group-options\"></datalist><script>const $=s=>document.querySelector(s),advancedControls=['item-id','description','type','family','group','pack'],staff=['YIM','WAT','BON','YA','BIAS','DERRICK'];let timer,current,typeOptions=[],fieldChoices={},actor=localStorage.getItem('apc-core-identity')||'';function renderIdentity(){const label=$('#identity');label.textContent=actor?'Current user: '+actor:'Choose user before saving';}function chooseUser(){const panel=document.createElement('form');panel.id='identity-confirm';panel.innerHTML='<label>Choose user <select name=\"actor\">'+staff.map(name=>'<option>'+name+'</option>').join('')+'</select></label><p class=\"status\">Stored on this browser/PC only; not security or authentication.</p><button type=\"submit\">Confirm user</button>';panel.onsubmit=event=>{event.preventDefault();actor=panel.elements.actor.value;localStorage.setItem('apc-core-identity',actor);renderIdentity();panel.remove()};document.body.append(panel)}function requireActor(){if(actor)return actor;chooseUser();return null}renderIdentity();$('#change-user').onclick=chooseUser;const text=(n,v)=>n.textContent=v||'—';function options(id,values,label){const n=$('#'+id),currentValue=n.value;n.replaceChildren(new Option(label,''));values.forEach(v=>n.add(new Option(v,v)));n.value=currentValue}function datalist(id,values){const n=$('#'+id);n.replaceChildren(...[...new Set(values.filter(Boolean))].sort().map(v=>new Option(v,v)))}function addChoices(items){for(const field of ['type','phyto_family','keset_family','scientific_family','thai_family','apc_team','apc_group'])fieldChoices[field]=[...new Set([...(fieldChoices[field]||[]),...items.map(item=>item[field]).filter(Boolean)])];datalist('type-options',fieldChoices.type||[]);datalist('phyto-family-options',fieldChoices.phyto_family||[]);datalist('keset-family-options',fieldChoices.keset_family||[]);datalist('scientific-family-options',fieldChoices.scientific_family||[]);datalist('thai-family-options',fieldChoices.thai_family||[]);datalist('apc-team-options',fieldChoices.apc_team||[]);datalist('group-options',['A','B',...(fieldChoices.apc_group||[])]);}function query(offset=0){const p=new URLSearchParams({limit:100,offset,q:$('#search').value});p.set('item_id_prefix',$('#item-id').value);p.set('description',$('#description').value);p.set('type',$('#type').value);p.set('family',$('#family').value);p.set('group',$('#group').value);p.set('pack_sequence',$('#pack').value);return p}function chips(){const box=$('#chips');box.replaceChildren();[['search','Search'],...advancedControls.map(id=>[id,$('#'+id).closest('label').childNodes[0].textContent.trim()])].forEach(([id,label])=>{const value=$('#'+id).value;if(value){const chip=document.createElement('span');chip.className='chip';chip.textContent=label+': '+value;box.append(chip)}})}function select(item){current=item;const form=$('#edit-template').content.firstElementChild.cloneNode(true);Object.entries(item).forEach(([key,value])=>{const control=form.elements.namedItem(key);if(control)control.value=value||''});const status=form.querySelector('[data-unsaved]');if(item.core_created&&!item.item_id){const idControl=form.elements.namedItem('item_id');idControl.readOnly=false;idControl.classList.remove('locked');form.querySelector('.save').textContent='Create item';form.querySelector('[data-duplicate]').hidden=true;status.textContent='Unsaved duplicate — choose a new Item ID, then Create.';}form.oninput=()=>status.textContent='Unsaved changes';form.querySelector('[data-cancel]').onclick=()=>select(current);form.querySelector('[data-duplicate]').onclick=()=>{if(!requireActor())return;const proposal={...current,item_id:'',original_item_id:current.item_id,core_created:true,source_label:'Core-created'};current=proposal;select(proposal)};form.querySelector('[data-archive]').onclick=async()=>{const selectedActor=requireActor();if(!selectedActor||!current.item_id||!confirm('Archive this item? It will be hidden, not deleted.'))return;const response=await fetch('api/items/'+encodeURIComponent(current.item_id)+'/archive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:selectedActor})});if(response.ok){await load();$('#detail').innerHTML='<p class="status">Item archived.</p>'}};form.onsubmit=async event=>{event.preventDefault();const selectedActor=requireActor();if(!selectedActor)return;const changes=Object.fromEntries(new FormData(form));changes.actor=selectedActor;const creating=Boolean(current.core_created&&!current.item_id);const response=await fetch(creating?'api/items':'api/items/'+encodeURIComponent(current.item_id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(creating?changes:(delete changes.item_id,changes))});if(!response.ok){status.textContent='Check the values and try again.';return}current=(await response.json()).item;status.textContent='Saved locally.';await load();select(current)};$('#detail').replaceChildren(form)}async function load(append=false){const offset=append?$('#rows').children.length:0,response=await fetch('api/items?'+query(offset),{cache:'no-store'}),data=await response.json(),body=$('#rows');typeOptions=data.type_options;options('type',data.type_options,'All types');options('family',data.family_options,'All families');options('group',data.group_options,'All groups');options('pack',data.pack_sequence_options,'All sequences');addChoices(data.items);const ids=$('#item-id-options');ids.replaceChildren(...data.items.map(item=>new Option(item.item_id)));if(!append)body.replaceChildren();text($('#result-count'),data.total+' results');$('#empty').hidden=Boolean(data.total);chips();data.items.forEach(item=>{const row=document.createElement('tr'),cell=document.createElement('td'),id=document.createElement('strong');row.tabIndex=0;text(id,item.item_id);cell.append(id,document.createElement('br'),document.createTextNode((item.source_label?item.source_label+' · ':'')+item.description),document.createElement('div'));cell.lastChild.className='thai';text(cell.lastChild,item.description_th);row.append(cell,Object.assign(document.createElement('td'),{textContent:item.type}),Object.assign(document.createElement('td'),{textContent:item.phyto_family}));row.onclick=()=>select(item);row.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select(item)}};body.append(row)});$('#more').hidden=offset+data.items.length>=data.total}function schedule(){clearTimeout(timer);timer=setTimeout(()=>load(),120)}$('#search').addEventListener('input',schedule);advancedControls.forEach(id=>$('#'+id).addEventListener('input',schedule));$('#clear').onclick=()=>{fieldChoices={};$('#search').value='';advancedControls.forEach(id=>$('#'+id).value='');load()};$('#more').onclick=()=>load(true);load();</script></body></html>"""


def make_handler(explorer: ItemExplorer, manifest: dict):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                # The peer may disconnect after a malformed request; the
                # response was already formed, so do not crash the handler.
                return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = _menu_html().encode("utf-8")
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if parsed.path == "/items/":
                body = _item_explorer_html().encode("utf-8")
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if parsed.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "ok", "mode": "local_core"}); return
            api_path = parsed.path.removeprefix("/items")
            if api_path == "/api/snapshot": self._send_json(HTTPStatus.OK, manifest); return
            if api_path == "/api/items":
                query = parse_qs(parsed.query)
                try:
                    result = explorer.search(query.get("q", [""])[0], query.get("limit", [100])[0], query.get("offset", [0])[0],
                                             item_id_prefix=query.get("item_id_prefix", [""])[0], description=query.get("description", [""])[0],
                                             family=query.get("family", [""])[0], group=query.get("group", [""])[0],
                                             item_type=query.get("type", [""])[0], pack_sequence=query.get("pack_sequence", [""])[0])
                except (ValueError, sqlite3.Error): self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid item query"})
                else: self._send_json(HTTPStatus.OK, result)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            api_path = urlparse(self.path).path.removeprefix("/items")
            if api_path == "/api/items":
                try:
                    content_length = int(self.headers.get("Content-Length", "-1"))
                    if content_length < 0 or content_length > 8192: raise ValueError
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if type(payload) is not dict: raise ValueError
                    actor = payload.pop("actor", None)
                    self._send_json(HTTPStatus.CREATED, {"item": explorer.create(payload, actor)})
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, sqlite3.Error):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid item create"})
                return
            if not api_path.startswith("/api/items/"):
                self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}); return
            try:
                content_length = int(self.headers.get("Content-Length", "-1"))
                if content_length < 0 or content_length > 8192: raise ValueError
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                if type(payload) is not dict: raise ValueError
                actor = payload.pop("actor", None)
                item_id = unquote(api_path[len("/api/items/"):])
                if item_id.endswith("/archive"):
                    item = explorer.archive(item_id.removesuffix("/archive"), actor)
                    self._send_json(HTTPStatus.OK, {"item": item}); return
                if item_id.endswith("/duplicate"):
                    raise ValueError
                item = explorer.edit(item_id, payload, actor)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, sqlite3.Error): self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid item edit"})
            else: self._send_json(HTTPStatus.OK, {"item": item})
        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        def log_message(self, format: str, *args) -> None: return
    return Handler
