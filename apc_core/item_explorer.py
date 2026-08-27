import json
import ipaddress
import os
import re
import sqlite3
import stat
import threading
import hashlib
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


_PRIVATE_LAN_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _customer_client_allowed(client_address: str, customer_lan_ingress: bool) -> bool:
    """Allow only direct loopback or, with opt-in, RFC1918 IPv4 peers."""
    try:
        address = ipaddress.ip_address(client_address)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return customer_lan_ingress and type(address) is ipaddress.IPv4Address and any(
        address in network for network in _PRIVATE_LAN_NETWORKS
    )



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
        limit = max(1, min(int(limit), 250))
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
        total = len(items); next_offset = offset + limit; return {"total": total, "limit": limit, "offset": offset, "has_more": next_offset < total, "next_offset": next_offset if next_offset < total else None, **filters, "items": items[offset:next_offset]}

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


def _staff_identity_shell(html: str) -> str:
    """Gate each browser page behind the same attribution-only staff picker."""
    shell = """<script>try{if(localStorage.getItem("apc-core-identity"))document.documentElement.classList.add("apc-core-known-user")}catch(_){}</script><style>html.apc-core-known-user #identity-confirm{display:none}html.apc-core-known-user #identity-content{display:block!important}</style><section id="identity-confirm" aria-live="polite" style="position:fixed;right:16px;top:16px;z-index:10"><div id="identity-picker" class="identity-card" style="width:min(360px,calc(100vw - 32px));padding:20px;border:1px solid #e5e1db;border-radius:18px;background:#fffdfa;box-shadow:0 16px 42px rgba(36,39,43,.14)"><p style="margin:0 0 14px"><strong style="display:block;font-size:16px">Who is using APC Program?</strong><span style="display:block;margin-top:5px;color:#68717c;line-height:1.4">Activity attribution only — not security, authentication, or authorization.</span></p><label style="display:grid;gap:6px;font-weight:600">Choose user <select id="identity-user" style="min-height:38px;border:1px solid #e5e1db;border-radius:10px;padding:0 10px;background:#fff"></select></label><button id="confirm-user" type="button" style="margin-top:14px;min-height:38px;border:0;border-radius:10px;padding:0 14px;background:#1d6b57;color:#fff;font-weight:700">Continue</button></div><button id="identity-change-user" type="button" class="identity-action secondary" style="min-height:38px;border:1px solid #1d6b57;border-radius:10px;padding:0 14px;background:#fff;color:#1d6b57;font-weight:700" hidden>Change user</button></section><div id="identity-content" hidden>"""
    script = """</div><script>(()=>{const key="apc-core-identity",content=document.getElementById("identity-content"),picker=document.getElementById("identity-picker"),select=document.getElementById("identity-user"),confirm=document.getElementById("confirm-user"),change=document.getElementById("identity-change-user");function apply(value){window.apcCoreActiveStaff=value||"";document.documentElement.classList.toggle("apc-core-known-user",!!value);if(value)localStorage.setItem(key,value);else localStorage.removeItem(key);content.hidden=!value;picker.hidden=!!value;change.hidden=!value;window.dispatchEvent(new CustomEvent("apc-core-identity",{detail:value||""}))}function choose(){apply("");picker.hidden=false;content.hidden=true;select.focus()}window.apcCoreChooseUser=choose;change.onclick=choose;confirm.onclick=()=>apply(select.value);const saved=localStorage.getItem(key);fetch("api/staff").then(response=>response.json()).then(payload=>{for(const person of payload.staff||[]){const option=document.createElement("option");option.value=person.username;option.textContent=person.username+" · "+person.role;select.append(option)}if(saved&&[...select.options].some(option=>option.value===saved))apply(saved);else choose()}).catch(()=>choose())})()</script>"""
    return html.replace("<body>", "<body>" + shell, 1).replace("</body>", script + "</body>", 1)


def _menu_html_body() -> str:
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core</title><style>:root{--ink:#202124;--muted:#6e737b;--line:#e6e6e8;--canvas:#f5f5f7;--paper:#fff;--blue:#0071e3}*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:900px;margin:auto;padding:56px 24px}.brand{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--blue);margin-bottom:18px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{min-height:156px;border:1px solid var(--line);border-radius:20px;background:var(--paper);padding:22px;text-decoration:none;color:inherit;display:flex;flex-direction:column;justify-content:space-between}.card h2{font-size:21px;margin:0}.card:hover,.card:focus-visible{border-color:#a9cfee;box-shadow:0 10px 24px rgba(0,113,227,.12);transform:translateY(-2px);transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}.card p{color:var(--muted)}.open{font-weight:600;color:var(--blue)}.soon{opacity:.72}.label{font-size:12px}@media(max-width:620px){.grid{grid-template-columns:1fr}}</style><body><main class="shell"><div class="brand">APC Core</div><section class="grid" aria-label="APC Core modules"><a class="card" href="items/"><div><h2>Items</h2><p>Search and inspect the item catalogue.</p></div><span class="open">Open Item Explorer →</span></a><div class="card soon"><div><h2>Orders</h2><p>Order work will appear here.</p></div><span class="label">Coming soon</span></div><a class="card" href="customers/"><div><h2>Customers</h2><p>Search and inspect Core-owned customer records.</p></div><span class="open">Open Customer Explorer →</span></a><a class="card" href="customer-prices/"><div><h2>Customer Prices</h2><p>Search and safely edit imported customer-item price rows.</p></div><span class="open">Open Customer Price →</span></a><div class="card soon"><div><h2>Shipments</h2><p>Shipment tracking will appear here.</p></div><span class="label">Coming soon</span></div><div class="card soon"><div><h2>Activity</h2><p>Shared activity will appear here.</p></div><span class="label">Coming soon</span></div></section></main></body></html>"""


def _menu_html() -> str:
    return _staff_identity_shell(_menu_html_body())


def _item_explorer_html_body() -> str:
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core · Item Explorer</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#faf7f2;--paper:#fffdfa;--accent:#1d6b57;--list-alt:#f1ede4;--list-hover:#dcefe5}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}.shell{max-width:1440px;margin:auto;padding:28px}.back{color:var(--accent);font-weight:700;text-decoration:none}.top{margin:16px 0}.workspace{display:grid;grid-template-columns:minmax(0,1fr) 470px;border:1px solid var(--line);border-radius:16px;background:var(--paper)}.queue{padding:18px;min-width:0}.detail{position:sticky;top:20px;align-self:start;border-left:1px solid var(--line);padding:20px;background:#fdfbf8}input,select,button{padding:10px 11px;border:1px solid var(--line);border-radius:9px;font:inherit}input,select{width:100%}button{cursor:pointer;background:#fff}.toolbar{position:sticky;top:0;z-index:2;margin:0 -18px 12px;padding:12px 18px;background:#fffdfa;border-bottom:1px solid var(--line);display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:center}.advanced-search{margin:0 0 12px;border:1px solid var(--line);border-radius:9px;padding:0 10px}.advanced-search summary{cursor:pointer;padding:10px 0;font-weight:700;color:var(--accent)}.filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:0 0 12px}.save{background:var(--accent);color:#fff;font-weight:700}.chips{display:flex;gap:6px;flex-wrap:wrap;margin:9px 0}.chip{background:#eaf4ee;color:#174d3e;border-radius:999px;padding:4px 8px;font-size:12px}.status,.thai{color:var(--muted);font-size:12px}.copy-id{font-size:11px;margin-left:7px;padding:3px 6px}table{border-collapse:collapse;width:100%;text-align:left;table-layout:fixed}th{position:sticky;top:62px;z-index:1;background:var(--paper);font-size:11px;color:var(--muted);text-transform:uppercase;padding:8px 10px;border-bottom:1px solid var(--line)}th:first-child{width:58%}td{padding:8px 10px;border-bottom:1px solid #eeeae4;vertical-align:top;overflow-wrap:anywhere}tbody tr:nth-child(even){background:var(--list-alt)}tr{cursor:pointer}tr:hover,tr:focus{background:var(--list-hover);outline:2px solid #b9dbcf;outline-offset:-2px}.empty{padding:28px;text-align:center;color:var(--muted)}.form-section{border:0;border-top:1px solid var(--line);margin:13px 0 0;padding:13px 0 0}.form-section legend{font-size:11px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--accent)}.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.wide{grid-column:1/-1}.edit-form label{display:block;color:var(--muted);font-size:12px;font-weight:700}.edit-form label input,.edit-form label select{margin-top:4px}.locked{background:#f0f0ef}.actions{display:flex;gap:8px;margin-top:14px}.detail-fields{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0}.detail-fields div{border:1px solid var(--line);border-radius:9px;padding:9px}.detail-fields dt{font-size:11px;font-weight:700;color:var(--muted)}.detail-fields dd{margin:4px 0 0;overflow-wrap:anywhere}.apc-group-row{grid-column:1/-1;border:0;margin:0;padding:0}.apc-group-row legend{font-size:12px;font-weight:700;color:var(--muted)}.radio-group{display:flex;gap:12px;align-items:center;margin-top:4px}.radio-group label{display:flex;align-items:center;gap:4px}.radio-group input{width:auto}@media(max-width:900px){.shell{padding:16px}.workspace{grid-template-columns:1fr}.detail{position:static;border-left:0;border-top:1px solid var(--line)}.toolbar{margin:0 -18px 12px;grid-template-columns:1fr auto}.toolbar strong{grid-column:1/-1}.filters,.field-grid{grid-template-columns:1fr}}</style><body><main class=\"shell\"><a class=\"back\" href=\"../\">← APC Core</a><header class=\"top\"><div class=\"status\">APC Core</div><h1>Item Explorer</h1><div id=\"identity\" class=\"status\" aria-live=\"polite\"></div></header><section class=\"workspace\"><div class=\"queue\"><div class=\"toolbar\"><input id=\"search\" type=\"search\" autocomplete=\"off\" placeholder=\"Search item ID, English or Thai description\" aria-label=\"Search items\"><button id=\"clear\" type=\"button\">Clear all</button><button id=\"add-item\" type=\"button\">Add item</button><strong id=\"result-count\">0 results</strong></div><details class=\"advanced-search\"><summary>Advanced Search</summary><div class=\"filters\"><label>Item ID prefix<input id=\"item-id\" list=\"item-id-options\" autocomplete=\"off\" placeholder=\"Start typing ID\"></label><label>Description / Thai description<input id=\"description\" type=\"search\" autocomplete=\"off\"></label><label>Type<select id=\"type\"><option value=\"\">All types</option></select></label><label>Family<select id=\"family\"><option value=\"\">All families</option></select></label><label>Group<select id=\"group\"><option value=\"\">All groups</option></select></label><label>Pack Sequence<select id=\"pack\"><option value=\"\">All sequences</option></select></label></div></details><datalist id=\"item-id-options\"></datalist><div id=\"chips\" class=\"chips\" aria-label=\"Active filters\"></div><table><thead><tr><th>Item ID / Description</th><th>Type</th><th>Family</th></tr></thead><tbody id=\"rows\"></tbody></table><div id=\"empty\" class=\"empty\" hidden>No items match these filters. Clear all or try another search.</div><button id=\"more\" type=\"button\" class=\"save\" hidden>Load more</button></div><aside class=\"detail\" id=\"detail\" aria-live=\"polite\"><p class=\"status\">Select an item to edit it locally.</p></aside></section></main><template id=\"detail-template\"><section class=\"item-detail\"><div class=\"actions\"><button type=\"button\" data-edit-item>Edit</button></div><dl class=\"detail-fields\"><div><dt>Item ID</dt><dd data-field=\"item_id\"></dd></div><div><dt>Description</dt><dd data-field=\"description\"></dd></div><div><dt>Description (Thai)</dt><dd data-field=\"description_th\"></dd></div><div><dt>USA Name</dt><dd data-field=\"usa_name\"></dd></div><div><dt>Type</dt><dd data-field=\"type\"></dd></div><div><dt>Phyto Family</dt><dd data-field=\"phyto_family\"></dd></div><div><dt>Keset Family</dt><dd data-field=\"keset_family\"></dd></div><div><dt>Scientific Family</dt><dd data-field=\"scientific_family\"></dd></div><div><dt>Thai Family</dt><dd data-field=\"thai_family\"></dd></div><div><dt>APC Team</dt><dd data-field=\"apc_team\"></dd></div><div><dt>APC Group</dt><dd data-field=\"apc_group\"></dd></div><div><dt>Pack sequence</dt><dd data-field=\"pack_sequence\"></dd></div></dl></section></template><template id=\"edit-template\"><form class=\"edit-form\"><fieldset class=\"form-section\"><legend>Item identity</legend><div class=\"field-grid\"><label class=\"wide\">Item ID<input name=\"item_id\" readonly class=\"locked\"></label><label>Description<input name=\"description\"></label><label>Description (Thai)<input name=\"description_th\"></label><label>USA Name<input name=\"usa_name\"></label><label>Type<input name=\"type\" list=\"type-options\" autocomplete=\"off\"></label></div></fieldset><fieldset class=\"form-section\"><legend>Families &amp; Group</legend><div class=\"field-grid\"><label>Phyto Family<input name=\"phyto_family\" list=\"phyto-family-options\" autocomplete=\"off\"></label><label>Keset Family<input name=\"keset_family\" list=\"keset-family-options\" autocomplete=\"off\"></label><label>Scientific Family<input name=\"scientific_family\" list=\"scientific-family-options\" autocomplete=\"off\"></label><label>Thai Family<input name=\"thai_family\" list=\"thai-family-options\" autocomplete=\"off\"></label><label class=\"wide\">APC Team<input name=\"apc_team\" list=\"apc-team-options\" autocomplete=\"off\"></label><fieldset class=\"apc-group-row\"><legend>APC Group</legend><div class=\"radio-group\"><label><input name=\"apc_group\" type=\"radio\" value=\"A\"> A</label><label><input name=\"apc_group\" type=\"radio\" value=\"B\"> B</label><label><input name=\"apc_group\" type=\"radio\" value=\"\"> None</label></div></fieldset></div></fieldset><fieldset class=\"form-section\"><legend>Packaging</legend><div class=\"field-grid\"><label>Quantity per piece<input name=\"quantity_per_piece\" type=\"number\" min=\"0\" step=\"any\"></label><label>Europe Price<input name=\"price_eu\" type=\"number\" min=\"0\" step=\"any\"></label><label>Japan Price<input name=\"price_jp\" type=\"number\" min=\"0\" step=\"any\"></label><label>Thailand Price<input name=\"price_th\" type=\"number\" min=\"0\" step=\"any\"></label><label>Quantity per Carton<input name=\"quantity_per_carton\" type=\"number\" min=\"0\" step=\"1\"></label><label>Quantity per Styrofoam<input name=\"quantity_per_styrofoam\" type=\"number\" min=\"0\" step=\"1\"></label><label>Pack sequence<select name=\"pack_sequence\"><option value=\"1\">1</option><option value=\"2\">2</option><option value=\"3\">3</option><option value=\"4\">4</option><option value=\"5\">5</option></select></label><label>Quantity per Bag<input name=\"quantity_per_bag\" type=\"number\" min=\"0\" step=\"1\"></label></div></fieldset><div class=\"status\" data-unsaved>Saved locally.</div><div class=\"actions\"><button class=\"save\" type=\"submit\">Save changes</button><button type=\"button\" data-cancel>Cancel</button><button type=\"button\" data-duplicate>Duplicate Item</button><button type=\"button\" data-archive>Archive item</button></div></form></template><datalist id=\"type-options\"></datalist><datalist id=\"phyto-family-options\"></datalist><datalist id=\"keset-family-options\"></datalist><datalist id=\"scientific-family-options\"></datalist><datalist id=\"thai-family-options\"></datalist><datalist id=\"apc-team-options\"></datalist><datalist id=\"group-options\"></datalist><script>const $=s=>document.querySelector(s),advancedControls=['item-id','description','type','family','group','pack'],staff=['YIM','WAT','BON','YA','BIAS','DERRICK'];let timer,current,typeOptions=[],fieldChoices={},actor=localStorage.getItem('apc-core-identity')||'';function renderIdentity(){const label=$('#identity');label.textContent=actor?'Current user: '+actor:'Choose user before saving';}function chooseUser(){const panel=document.createElement('form');panel.id='identity-confirm';panel.innerHTML='<label>Choose user <select name=\"actor\">'+staff.map(name=>'<option>'+name+'</option>').join('')+'</select></label><p class=\"status\">Stored on this browser/PC only; not security or authentication.</p><button type=\"submit\">Confirm user</button>';panel.onsubmit=event=>{event.preventDefault();actor=panel.elements.actor.value;localStorage.setItem('apc-core-identity',actor);renderIdentity();panel.remove()};document.body.append(panel)}function requireActor(){if(actor)return actor;chooseUser();return null}renderIdentity();const text=(n,v)=>n.textContent=v||'—';function options(id,values,label){const n=$('#'+id),currentValue=n.value;n.replaceChildren(new Option(label,''));values.forEach(v=>n.add(new Option(v,v)));n.value=currentValue}function datalist(id,values){const n=$('#'+id);n.replaceChildren(...[...new Set(values.filter(Boolean))].sort().map(v=>new Option(v,v)))}function addChoices(items){for(const field of ['type','phyto_family','keset_family','scientific_family','thai_family','apc_team','apc_group'])fieldChoices[field]=[...new Set([...(fieldChoices[field]||[]),...items.map(item=>item[field]).filter(Boolean)])];datalist('type-options',fieldChoices.type||[]);datalist('phyto-family-options',fieldChoices.phyto_family||[]);datalist('keset-family-options',fieldChoices.keset_family||[]);datalist('scientific-family-options',fieldChoices.scientific_family||[]);datalist('thai-family-options',fieldChoices.thai_family||[]);datalist('apc-team-options',fieldChoices.apc_team||[]);datalist('group-options',['A','B',...(fieldChoices.apc_group||[])]);}function query(offset=0){const p=new URLSearchParams({limit:100,offset,q:$('#search').value});p.set('item_id_prefix',$('#item-id').value);p.set('description',$('#description').value);p.set('type',$('#type').value);p.set('family',$('#family').value);p.set('group',$('#group').value);p.set('pack_sequence',$('#pack').value);return p}function chips(){const box=$('#chips');box.replaceChildren();[['search','Search'],...advancedControls.map(id=>[id,$('#'+id).closest('label').childNodes[0].textContent.trim()])].forEach(([id,label])=>{const value=$('#'+id).value;if(value){const chip=document.createElement('span');chip.className='chip';chip.textContent=label+': '+value;box.append(chip)}})}function select(item){current=item;renderDetail(item)}function renderEmptyDetail(){current=null;$('#detail').innerHTML='<p class="status">Select an item to edit it locally.</p>'}function renderDetail(item){const detail=$('#detail-template').content.firstElementChild.cloneNode(true);detail.querySelectorAll('[data-field]').forEach(field=>text(field,item[field.dataset.field]));detail.querySelector('[data-edit-item]').onclick=()=>renderEdit(current);$('#detail').replaceChildren(detail)}function renderEdit(item){current=item;const form=$('#edit-template').content.firstElementChild.cloneNode(true);Object.entries(item).forEach(([key,value])=>{const control=form.elements.namedItem(key);if(control)control.value=value||''});const status=form.querySelector('[data-unsaved]');if(item.core_created&&!item.item_id){const idControl=form.elements.namedItem('item_id');idControl.readOnly=false;idControl.classList.remove('locked');form.querySelector('.save').textContent='Create item';form.querySelector('[data-duplicate]').hidden=true;status.textContent='Unsaved duplicate — choose a new Item ID, then Create.';}form.oninput=()=>status.textContent='Unsaved changes';form.querySelector('[data-cancel]').onclick=()=>current&&current.core_created&&!current.item_id?renderEmptyDetail():renderDetail(current);form.querySelector('[data-duplicate]').onclick=()=>{if(!requireActor())return;const proposal={...current,item_id:'',original_item_id:current.item_id,core_created:true,source_label:'Core-created'};current=proposal;renderEdit(proposal)};form.querySelector('[data-archive]').onclick=async()=>{const selectedActor=requireActor();if(!selectedActor||!current.item_id||!confirm('Archive this item? It will be hidden, not deleted.'))return;const response=await fetch('api/items/'+encodeURIComponent(current.item_id)+'/archive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:selectedActor})});if(response.ok){await load();$('#detail').innerHTML='<p class="status">Item archived.</p>'}};form.onsubmit=async event=>{event.preventDefault();const selectedActor=requireActor();if(!selectedActor)return;const changes=Object.fromEntries(new FormData(form));changes.actor=selectedActor;const creating=Boolean(current.core_created&&!current.item_id);const response=await fetch(creating?'api/items':'api/items/'+encodeURIComponent(current.item_id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(creating?changes:(delete changes.item_id,changes))});if(!response.ok){status.textContent='Check the values and try again.';return}current=(await response.json()).item;status.textContent='Saved locally.';await load();renderDetail(current)};$('#detail').replaceChildren(form)}async function load(append=false){const offset=append?$('#rows').children.length:0,response=await fetch('api/items?'+query(offset),{cache:'no-store'}),data=await response.json(),body=$('#rows');typeOptions=data.type_options;options('type',data.type_options,'All types');options('family',data.family_options,'All families');options('group',data.group_options,'All groups');options('pack',data.pack_sequence_options,'All sequences');addChoices(data.items);const ids=$('#item-id-options');ids.replaceChildren(...data.items.map(item=>new Option(item.item_id)));if(!append)body.replaceChildren();text($('#result-count'),data.total+' results');$('#empty').hidden=Boolean(data.total);chips();data.items.forEach(item=>{const row=document.createElement('tr'),cell=document.createElement('td'),id=document.createElement('strong');row.tabIndex=0;text(id,item.item_id);cell.append(id,document.createElement('br'),document.createTextNode((item.source_label?item.source_label+' · ':'')+item.description),document.createElement('div'));cell.lastChild.className='thai';text(cell.lastChild,item.description_th);row.append(cell,Object.assign(document.createElement('td'),{textContent:item.type}),Object.assign(document.createElement('td'),{textContent:item.phyto_family}));row.onclick=()=>select(item);row.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select(item)}};body.append(row)});$('#more').hidden=offset+data.items.length>=data.total}function schedule(){clearTimeout(timer);timer=setTimeout(()=>load(),120)}$('#search').addEventListener('input',schedule);advancedControls.forEach(id=>$('#'+id).addEventListener('input',schedule));$('#clear').onclick=()=>{fieldChoices={};$('#search').value='';advancedControls.forEach(id=>$('#'+id).value='');load()};$('#add-item').onclick=()=>{if(!requireActor())return;const proposal={item_id:'',core_created:true,source_label:'Core-created'};current=proposal;renderEdit(proposal)};$('#more').onclick=()=>load(true);load();</script></body></html>"""



def _item_explorer_html() -> str:
    html = _item_explorer_html_body()
    html = re.sub(r",staff=\[[^]]+\];let timer", ";let timer", html)
    html = html.replace(
        "actor=localStorage.getItem('apc-core-identity')||'';",
        "actor=window.apcCoreActiveStaff||'';window.addEventListener('apc-core-identity',event=>{actor=event.detail});",
    )
    html = re.sub(
        r"function chooseUser\(\)\{.*?document\.body\.append\(panel\)\}",
        "function chooseUser(){window.apcCoreChooseUser()}",
        html,
    )
    return _staff_identity_shell(html)


def _customer_explorer_html_existing() -> str:
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core · Customer Explorer</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#faf7f2;--paper:#fffdfa;--accent:#1d6b57;--danger:#a42e22;--list-alt:#f1ede4;--list-hover:#dcefe5}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:1200px;margin:auto;padding:24px}.toolbar,.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.toolbar{position:sticky;top:0;background:var(--cream);padding:10px 0}input,select,textarea,button{font:inherit;padding:9px;border:1px solid var(--line);border-radius:8px}input{flex:1;min-width:180px}button{background:var(--accent);color:#fff;font-weight:700;cursor:pointer}.secondary{background:#fff;color:var(--accent)}.danger{background:var(--danger)}.workspace{display:grid;grid-template-columns:1fr 1.35fr;background:var(--paper);border:1px solid var(--line);border-radius:14px}.list,.profile{padding:16px}.profile{border-left:1px solid var(--line)}.customer{display:block;width:100%;text-align:left;background:#fff;color:var(--ink);border:0;border-bottom:1px solid var(--line)}.customer:nth-child(even){background:var(--list-alt)}.customer:hover,.customer:focus{background:var(--list-hover);outline:2px solid #b9dbcf;outline-offset:-2px}.tabs{display:flex;gap:6px;margin:12px 0}.tab[aria-selected="true"]{background:var(--accent);color:#fff}.panel[hidden],[hidden]{display:none!important}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.field{border:1px solid var(--line);padding:8px;border-radius:8px}.field b{display:block;font-size:11px;color:var(--muted)}.drawer{position:fixed;right:16px;top:16px;width:min(430px,calc(100vw - 32px));background:#fff;padding:16px;border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 32px #0004}@media(max-width:760px){.shell{padding:12px}.workspace{grid-template-columns:1fr}.profile{border:0;border-top:1px solid var(--line)}.field-grid{grid-template-columns:1fr}}</style><body><main class="shell"><a href="../">← APC Core</a><h1>Customer Explorer</h1><p class="meta">Core-owned customer records</p><p class="meta">LAN-only editor: selected active staff is accountability attribution, not authentication.</p><p class="meta">Order Entry side-panel contract remains future consumption only.</p><div class="toolbar"><input id="q" aria-label="Search customers" placeholder="Search customers"><button id="search" type="button">Search</button><button id="new-customer" type="button">Add customer</button></div><p id="status" aria-live="polite"></p><div class="workspace"><section class="list"><div id="results" aria-live="polite"></div></section><section id="profile" class="profile" tabindex="-1">Select a customer.</section></div></main><div id="note-manager" class="drawer" role="dialog" aria-modal="true" aria-label="Manage customer notes" hidden><h2>Manage customer notes</h2><select id="note-kind"><option value="order">Order</option><option value="invoice">Invoice</option></select><textarea id="note-body" aria-label="Note"></textarea><div class="actions"><button id="save-note" type="button">Save note</button><button id="close-notes" class="secondary" type="button">Cancel</button></div></div><script>(()=>{const $=s=>document.querySelector(s),q=$('#q'),results=$('#results'),profile=$('#profile'),status=$('#status'),staff=$('#active-staff'),notes=$('#note-manager');let current;const fields=['name','address_1','address_2','address_3','tel','fax','email','price_type','box_type','invoice_header','invoice_type','invoice_year'];function el(tag,text){const x=document.createElement(tag);if(text!==undefined)x.textContent=text;return x}function clean(x){x.replaceChildren()}function say(x){status.textContent=x}function activeStaff(){return staff.value}function path(id){return 'api/customers/'+encodeURIComponent(id)}function configPath(id){return 'api/customers/'+encodeURIComponent(id)+'/export-config'}function consigneePath(id){return 'api/customers/'+encodeURIComponent(id)+'/consignees'}function notePath(id){return 'api/customers/'+encodeURIComponent(id)+'/notes'}async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw Error('Save failed');return r.json()}function data(){const x={actor:activeStaff()};fields.forEach(k=>{const i=profile.querySelector('[name="'+k+'"]');if(i)x[k]=i.value});return x}function button(id,label,fn,klass){const b=el('button',label);b.id=id;b.type='button';b.className=klass||'';b.onclick=fn;return b}function tabs(basic,additional){const box=el('div');box.className='tabs';box.setAttribute('role','tablist');[['Basic',basic],['Additional',additional]].forEach(([name,p],i)=>{const b=el('button',name);b.type='button';b.className='tab';b.setAttribute('role','tab');b.setAttribute('aria-selected',String(!i));b.onclick=()=>{box.querySelectorAll('button').forEach(x=>x.setAttribute('aria-selected',String(x===b)));basic.hidden=p!==basic;additional.hidden=p!==additional};box.append(b)});return box}function render(d,edit){current=d;clean(profile);const c=d.customer;profile.append(el('h2',c.customer_id+' · '+c.name));const actions=el('div');actions.className='actions';actions.append(button('edit-customer','Edit',()=>render(current,true),'secondary'),button('save-customer','Save',async()=>{try{await post(path(c.customer_id),data());say('Customer saved.');open(c.customer_id)}catch(e){say(e.message)}}),button('cancel-customer','Cancel',()=>render(current,false),'secondary'),button('archive-customer','Archive',async()=>{if(confirm('Archive this customer?'))try{await post(path(c.customer_id)+'/archive',{actor:activeStaff()});say('Customer archived.');clean(profile);load()}catch(e){say(e.message)}},'danger'));edit ? actions.append(noteActions(c.customer_id)) : null;profile.append(actions);const basic=el('section'),additional=el('section');basic.className=additional.className='panel';additional.hidden=true;const grid=el('div');grid.className='field-grid';fields.forEach(k=>{const f=el('label');f.className='field';f.append(el('b',k.replaceAll('_',' ')));if(edit){const i=document.createElement('input');i.name=k;i.value=c[k]||'';f.append(i)}else f.append(el('span',c[k]||'—'));grid.append(f)});basic.append(grid);const cfg=el('div');cfg.className='field-grid';['exporter','commercial','order_settings','hc_settings','awb_configuration'].forEach(k=>{const i=document.createElement('input');i.name=k;i.value=d.export_config[k]||'';cfg.append(i)});additional.append(el('h2','Export configuration'),cfg,button('save-config','Save configuration',async()=>{try{const x={actor:activeStaff()};cfg.querySelectorAll('input').forEach(i=>x[i.name]=i.value);await post(configPath(c.customer_id),x);say('Configuration saved.');open(c.customer_id)}catch(e){say(e.message)}}));const cons=el('section');cons.append(el('h2','Consignees'));d.consignees.forEach(r=>{const row=el('div',r.consignee+' · '+r.country);row.append(' ',button('', 'Archive',async()=>{try{await post(consigneePath(c.customer_id)+'/'+r.id+'/archive',{actor:activeStaff()});open(c.customer_id)}catch(e){say(e.message)}},'danger'));cons.append(row)});cons.append(button('add-consignee','Add consignee',async()=>{const consignee=prompt('Consignee')||'',country=prompt('Country')||'';try{await post(consigneePath(c.customer_id),{consignee,country,actor:activeStaff()});open(c.customer_id)}catch(e){say(e.message)}},'secondary'));additional.append(cons,button('manage-notes','Manage customer notes',()=>{notes.hidden=false;$('#note-body').focus()},'secondary'));if(!edit){actions.querySelectorAll('#save-customer,#cancel-customer,#archive-customer').forEach(x=>x.hidden=true);cfg.querySelectorAll('input').forEach(x=>{x.readOnly=true;x.tabIndex=-1});const saveConfig=additional.querySelector('#save-config');if(saveConfig)saveConfig.hidden=true}profile.append(tabs(basic,additional),basic,additional);profile.focus()}async function open(id){try{const r=await fetch(path(id));render(await r.json(),false)}catch(e){say('Customer could not be loaded.')}}async function load(){try{const r=await fetch('api/customers?q='+encodeURIComponent(q.value));const d=await r.json();clean(results);d.customers.forEach(c=>results.append(button('',c.customer_id+' · '+c.name+' · Type '+(c.price_type||'—'),()=>open(c.customer_id),'customer')));if(!d.customers.length)results.append(el('p','No customers match.'))}catch(e){say('Customers could not be loaded.')}}async function staffLoad(){try{const r=await fetch('api/staff');const d=await r.json();clean(staff);d.staff.forEach(p=>{const o=el('option',p.username+' · '+p.role);o.value=p.username;staff.append(o)})}catch(e){say('Active staff could not be loaded.')}}$('#search').onclick=load;q.onkeydown=e=>{if(e.key==='Enter')load()};$('#new-customer').onclick=async()=>{const customer_id=prompt('Customer ID')||'',name=prompt('Customer name')||'';try{const r=await post('api/customers',{customer_id,name,actor:activeStaff()});say('Customer created.');open(r.customer.customer_id)}catch(e){say(e.message)}};function closeNotes(){notes.hidden=true;lastNotesTrigger?.focus()}$('#close-notes').onclick=closeNotes;document.addEventListener('keydown',event=>{if(notes.hidden)return;if(event.key==='Escape'){event.preventDefault();closeNotes();return}if(event.key==='Tab'){const focusable=[...notes.querySelectorAll('button,textarea,[href],input,select')].filter(x=>!x.hidden&&!x.disabled);const first=focusable[0],last=focusable.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});$('#save-note').onclick=async()=>{if(!current)return;try{await post(notePath(current.customer.customer_id),{kind:noteKind,body:$('#note-body').value,actor:activeStaff()});notes.hidden=true;say('Note saved.');open(current.customer.customer_id)}catch(e){say(e.message)}};staffLoad();load()})()</script></body></html>"""


def _customer_explorer_html_base() -> str:
    """Render the local Core customer editor with safe, explicit child lifecycles."""
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core · Customer Explorer</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#faf7f2;--paper:#fffdfa;--accent:#1d6b57;--danger:#a42e22;--list-alt:#f1ede4;--list-hover:#dcefe5}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:1200px;margin:auto;padding:24px}.toolbar,.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.toolbar{position:sticky;top:0;background:var(--cream);padding:10px 0}input,select,textarea,button{font:inherit;padding:9px;border:1px solid var(--line);border-radius:8px}input{flex:1;min-width:180px}button{background:var(--accent);color:#fff;font-weight:700;cursor:pointer}.secondary{background:#fff;color:var(--accent)}.danger{background:var(--danger)}.workspace{display:grid;grid-template-columns:1fr 1.35fr;background:var(--paper);border:1px solid var(--line);border-radius:14px}.list,.profile{padding:16px}.profile{border-left:1px solid var(--line)}.customer{display:block;width:100%;text-align:left;background:#fff;color:var(--ink);border:0;border-bottom:1px solid var(--line)}.customer:nth-child(even){background:var(--list-alt)}.customer:hover,.customer:focus{background:var(--list-hover);outline:2px solid #b9dbcf;outline-offset:-2px}.tabs{display:flex;gap:6px;margin:12px 0}.tab[aria-selected="true"]{background:var(--accent);color:#fff}.panel[hidden],[hidden]{display:none!important}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.field,.child{border:1px solid var(--line);padding:8px;border-radius:8px;margin:8px 0}.field b{display:block;font-size:11px;color:var(--muted)}.drawer{position:fixed;right:16px;top:16px;width:min(430px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:auto;background:#fff;padding:16px;border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 32px #0004}.reconciliation{display:flex;gap:10px;align-items:flex-start;margin:12px 0;padding:10px 12px;border:1px solid #c9a843;border-radius:10px;background:#fff8df;color:#5f4900}.reconciliation[data-state="ready"]{border-color:#8ec7ae;background:#eff8f2;color:#174d3e}.reconciliation strong{display:block}.reconciliation p{margin:3px 0 0;font-size:12px;line-height:1.4}@media(max-width:760px){.shell{padding:12px}.workspace{grid-template-columns:1fr}.profile{border:0;border-top:1px solid var(--line)}.field-grid{grid-template-columns:1fr}}</style><body><main class="shell"><a href="../">← APC Core</a><h1>Customer Explorer</h1><p class="meta">Core-owned customer records</p><p class="meta">LAN-only editor: selected active staff is accountability attribution, not authentication.</p><p class="meta">Order Entry side-panel contract remains future consumption only.</p><section id="reconciliation-status" class="reconciliation" data-state="loading" aria-live="polite"><div><strong>Checking reconciliation status…</strong><p>Customer reads never start reconciliation.</p></div></section><div class="toolbar"><input id="q" aria-label="Search customers" placeholder="Search customers"><button id="search" type="button">Search</button><button id="new-customer" type="button">Add customer</button></div><p id="status" aria-live="polite"></p><div class="workspace"><section class="list"><div id="results" aria-live="polite"></div></section><section id="profile" class="profile" tabindex="-1">Select a customer.</section></div></main><div id="note-manager" class="drawer" role="dialog" aria-modal="true" aria-label="Manage customer notes" hidden><h2 id="note-manager-title">Manage customer notes</h2><div id="note-list" aria-live="polite"></div><h3>Add note</h3><label>Note body <textarea id="note-body" aria-label="Note"></textarea></label><div class="actions"><button id="save-note" type="button">Save note</button><button id="close-notes" class="secondary" type="button">Cancel</button></div></div><script>(()=>{const $=s=>document.querySelector(s),q=$('#q'),results=$('#results'),profile=$('#profile'),status=$('#status'),reconciliation=$('#reconciliation-status'),staff=$('#active-staff'),notes=$('#note-manager'),noteList=$('#note-list'),noteTitle=$('#note-manager-title');let current,noteKind='order',lastNotesTrigger=null;const fields=['name','address_1','address_2','address_3','tel','fax','email','price_type','box_type','invoice_header','invoice_type','invoice_year'];function el(tag,text){const x=document.createElement(tag);if(text!==undefined)x.textContent=text;return x}function clean(x){x.replaceChildren()}function say(x){status.textContent=x}function activeStaff(){return staff.value}function path(id){return 'api/customers/'+encodeURIComponent(id)}function configPath(id){return 'api/customers/'+encodeURIComponent(id)+'/export-config'}function consigneePath(id){return 'api/customers/'+encodeURIComponent(id)+'/consignees'}function notePath(id){return 'api/customers/'+encodeURIComponent(id)+'/notes'}async function post(url,data){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw Error('Save failed');return r.json()}function button(id,label,fn,klass){const b=el('button',label);b.id=id;b.type='button';b.className=klass||'';b.onclick=fn;return b}function data(){const x={actor:activeStaff()};fields.forEach(k=>{const i=profile.querySelector('[name="'+k+'"]');if(i)x[k]=i.value});return x}function tabs(basic,additional){const box=el('div');box.className='tabs';box.setAttribute('role','tablist');[['Basic',basic],['Additional',additional]].forEach(([name,p],i)=>{const b=el('button',name);b.type='button';b.className='tab';b.setAttribute('role','tab');b.setAttribute('aria-selected',String(!i));b.onclick=()=>{box.querySelectorAll('button').forEach(x=>x.setAttribute('aria-selected',String(x===b)));basic.hidden=p!==basic;additional.hidden=p!==additional};box.append(b)});return box}function child(label){const row=el('div');row.className='child';row.append(el('strong',label));return row}function renderConsignee(customerId,row){const box=child(row.consignee+' · '+row.country);box.append(' ',button('', 'Edit consignee',async()=>{const consignee=prompt('Consignee',row.consignee);if(consignee===null)return;const country=prompt('Country',row.country);if(country===null)return;try{await post(consigneePath(customerId)+'/'+encodeURIComponent(row.id),{consignee,country,province:row.province||'',broker:row.broker||'',flight:row.flight||'',actor:activeStaff()});open(customerId)}catch(e){say(e.message)}}),' ',button('', 'Archive consignee',async()=>{if(!confirm('Archive this consignee?'))return;try{await post(consigneePath(customerId)+'/'+encodeURIComponent(row.id)+'/archive',{actor:activeStaff()});open(customerId)}catch(e){say(e.message)}},'danger'));return box}function renderNote(customerId,note,kind){const label=kind==='order'?'order':'invoice',box=child(label+' note #'+note.id+': '+note.body);box.append(' ',button('', 'Edit '+label+' note',async()=>{const body=prompt('Note body',note.body);if(body===null)return;try{await post(notePath(customerId)+'/'+encodeURIComponent(note.id),{body,actor:activeStaff()});open(customerId)}catch(e){say(e.message)}}),' ',button('', 'Archive '+label+' note',async()=>{if(!confirm('Archive this '+label+' note?'))return;try{await post(notePath(customerId)+'/'+encodeURIComponent(note.id)+'/archive',{actor:activeStaff()});open(customerId)}catch(e){say(e.message)}},'danger'));return box}function renderNotes(){clean(noteList);if(!current)return;const rows=noteKind==='order'?current.order_notes:current.invoice_notes;noteList.append(el('h3',noteKind==='order'?'Order Notes':'Invoice Notes'));rows.forEach(note=>noteList.append(renderNote(current.customer.customer_id,note,noteKind)))}function openNotes(kind,trigger){lastNotesTrigger=trigger||lastNotesTrigger;noteKind=kind;noteTitle.textContent=kind==='order'?'Manage Order Notes':'Manage Invoice Notes';renderNotes();notes.hidden=false;$('#note-body').focus()}function noteActions(customerId){return [button('manage-order-notes','Manage Order Notes',event=>openNotes('order',event.currentTarget),'secondary'),button('manage-invoice-notes','Manage Invoice Notes',event=>openNotes('invoice',event.currentTarget),'secondary')]}function render(d,edit){current=d;clean(profile);const c=d.customer;profile.append(el('h2',c.customer_id+' · '+c.name));const actions=el('div');actions.className='actions';actions.append(button('edit-customer','Edit',()=>render(current,true),'secondary'),button('save-customer','Save',async()=>{try{await post(path(c.customer_id),data());say('Customer saved.');open(c.customer_id)}catch(e){say(e.message)}}),button('cancel-customer','Cancel',()=>render(current,false),'secondary'),button('archive-customer','Archive',async()=>{if(!confirm('Archive this customer?'))return;try{await post(path(c.customer_id)+'/archive',{actor:activeStaff()});say('Customer archived.');clean(profile);load()}catch(e){say(e.message)}},'danger'));edit ? actions.append(noteActions(c.customer_id)) : null;profile.append(actions);const basic=el('section'),additional=el('section');basic.className=additional.className='panel';additional.hidden=true;const grid=el('div');grid.className='field-grid';fields.forEach(k=>{const f=el('label');f.className='field';f.append(el('b',k.replaceAll('_',' ')));if(edit){const i=el('input');i.name=k;i.value=c[k]||'';f.append(i)}else f.append(el('span',c[k]||'—'));grid.append(f)});basic.append(grid);const cfg=el('div');cfg.className='field-grid';['exporter','commercial','order_settings','hc_settings','awb_configuration'].forEach(k=>{const i=el('input');i.name=k;i.value=d.export_config[k]||'';cfg.append(i)});additional.append(el('h2','Export configuration'),cfg,button('save-config','Save configuration',async()=>{try{const x={actor:activeStaff()};cfg.querySelectorAll('input').forEach(i=>x[i.name]=i.value);await post(configPath(c.customer_id),x);say('Configuration saved.');open(c.customer_id)}catch(e){say(e.message)}}));const cons=el('section');cons.append(el('h2','Consignees'));d.consignees.forEach(row=>cons.append(renderConsignee(c.customer_id,row)));cons.append(button('add-consignee','Add consignee',async()=>{const consignee=prompt('Consignee')||'',country=prompt('Country')||'';try{await post(consigneePath(c.customer_id),{consignee,country,actor:activeStaff()});open(c.customer_id)}catch(e){say(e.message)}},'secondary'));additional.append(cons);if(!edit){actions.querySelectorAll('#save-customer,#cancel-customer,#archive-customer').forEach(x=>x.hidden=true);cfg.querySelectorAll('input').forEach(x=>{x.readOnly=true;x.tabIndex=-1});const saveConfig=additional.querySelector('#save-config');if(saveConfig)saveConfig.hidden=true}profile.append(tabs(basic,additional),basic,additional);profile.focus()}async function open(id){try{const r=await fetch(path(id));if(!r.ok)throw Error();render(await r.json(),false);if(!notes.hidden)renderNotes()}catch(e){say('Customer could not be loaded.')}}async function load(){try{const r=await fetch('api/customers?q='+encodeURIComponent(q.value),{cache:'no-store'}),d=await r.json();clean(results);d.customers.forEach(c=>results.append(button('',c.customer_id+' · '+c.name+' · Type '+(c.price_type||'—'),()=>open(c.customer_id),'customer')));if(!d.customers.length)results.append(el('p','No customers match.'))}catch(e){say('Customers could not be loaded.')}}function reconciliationMessage(title,detail){clean(reconciliation);const box=el('div');box.append(el('strong',title),el('p',detail));reconciliation.append(box)}async function reconciliationLoad(){try{const r=await fetch('api/reconciliation-status',{cache:'no-store'});if(!r.ok)throw Error();const d=await r.json(),ready=d.state==='ready';reconciliation.dataset.state=d.state;reconciliationMessage(ready?'Customer data ready':'Reconciliation required',ready?'Customer data matches the accepted artifact.':'Customer data may be stale. Reads will not start reconciliation.')}catch(e){reconciliation.dataset.state='unknown';reconciliationMessage('Reconciliation status unavailable','Customer reads remain non-mutating.')}}async function staffLoad(){try{const r=await fetch('api/staff'),d=await r.json();clean(staff);d.staff.forEach(p=>{const o=el('option',p.username+' · '+p.role);o.value=p.username;staff.append(o)})}catch(e){say('Active staff could not be loaded.')}}$('#search').onclick=load;q.onkeydown=e=>{if(e.key==='Enter')load()};$('#new-customer').onclick=async()=>{const customer_id=prompt('Customer ID')||'',name=prompt('Customer name')||'';try{const r=await post('api/customers',{customer_id,name,actor:activeStaff()});say('Customer created.');open(r.customer.customer_id)}catch(e){say(e.message)}};function closeNotes(){notes.hidden=true;lastNotesTrigger?.focus()}$('#close-notes').onclick=closeNotes;document.addEventListener('keydown',event=>{if(notes.hidden)return;if(event.key==='Escape'){event.preventDefault();closeNotes();return}if(event.key==='Tab'){const focusable=[...notes.querySelectorAll('button,textarea,[href],input,select')].filter(x=>!x.hidden&&!x.disabled);const first=focusable[0],last=focusable.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}});$('#save-note').onclick=async()=>{if(!current)return;try{await post(notePath(current.customer.customer_id),{kind:noteKind,body:$('#note-body').value,actor:activeStaff()});$('#note-body').value='';say('Note saved.');open(current.customer.customer_id)}catch(e){say(e.message)}};reconciliationLoad();staffLoad();load()})()</script></body></html>"""


def _customer_explorer_html_legacy() -> str:
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core · Customer Explorer</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#faf7f2;--paper:#fffdfa;--accent:#1d6b57;--list-alt:#f1ede4;--list-hover:#dcefe5}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:1440px;margin:auto;padding:28px}.back{color:var(--accent);font-weight:700;text-decoration:none}.toolbar{position:sticky;top:0;z-index:2;display:flex;gap:8px;padding:12px 0;background:var(--cream)}input,button{font:inherit;border:1px solid var(--line);border-radius:9px;padding:10px}input{flex:1}.workspace{display:grid;grid-template-columns:minmax(340px,1fr) minmax(380px,1.1fr);border:1px solid var(--line);border-radius:16px;background:var(--paper);overflow:hidden}.list,.profile{padding:18px}.profile{border-left:1px solid var(--line)}button{background:var(--accent);color:#fff;font-weight:700;cursor:pointer}.customer{padding:12px;border-bottom:1px solid var(--line);cursor:pointer}.customer:hover,.customer:focus-visible{background:#f1f8f5}.meta{color:var(--muted);font-size:12px}.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0}.tab{background:#eaf4ee;color:#174d3e}.tab[aria-selected="true"]{background:var(--accent);color:#fff}.panel[hidden]{display:none}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.field{padding:9px;border:1px solid var(--line);border-radius:9px;background:#fff}.field b{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}.note-contract{margin-top:14px;padding:10px 12px;border-left:3px solid var(--accent);background:#f1f8f5;color:#174d3e;font-size:12px}.card{border:1px solid var(--line);border-radius:11px;padding:12px;margin:10px 0}h1{font-size:25px;margin:16px 0 4px}h2{font-size:17px;margin:0 0 8px}@media(max-width:820px){.workspace{grid-template-columns:1fr}.profile{border-left:0;border-top:1px solid var(--line)}.shell{padding:16px}}</style><body><main class="shell"><a class="back" href="../">← APC Core</a><h1>Customer Explorer</h1><p class="meta">Core-owned customer records</p><p class="meta">LAN-only editor: selected active staff is accountability attribution, not authentication.</p><p class="meta">Core-owned customer records with immutable source provenance.</p><div class="workspace"><section class="list"><div class="toolbar"><input id="q" placeholder="Search customer ID, name, address, phone or email"><button id="search">Search</button></div><div id="results" aria-live="polite"></div></section><section class="profile" id="profile"><p class="meta">Select a customer to inspect its Core-owned profile.</p></section></div></main><script>const q=document.querySelector('#q'),results=document.querySelector('#results'),profile=document.querySelector('#profile');function esc(v){const e=document.createElement('span');e.textContent=v??'';return e.innerHTML}async function load(){const r=await fetch('api/customers?q='+encodeURIComponent(q.value));const d=await r.json();results.innerHTML=d.customers.map(c=>`<div class="customer" tabindex="0" data-id="${esc(c.customer_id)}"><b>${esc(c.customer_id)}</b><br>${esc(c.name)}<div class="meta">${esc(c.email)} · ${esc(c.price_type)} / ${esc(c.box_type)}</div></div>`).join('')||'<p class="meta">No customers match.</p>';document.querySelectorAll('.customer').forEach(el=>el.onclick=()=>open(el.dataset.id))}async function open(id){const r=await fetch('api/customers/'+encodeURIComponent(id));const p=await r.json();const c=p.customer;profile.innerHTML=`<h2>${esc(c.customer_id)} · ${esc(c.name)}</h2><p class="meta">Core-owned · ${esc(c.source_artifact_sha256||'Core-created')}</p><div class="tabs" role="tablist" aria-label="Customer master sections"><button class="tab" data-tab="basic" role="tab" aria-selected="true">Basic</button><button class="tab" data-tab="additional" role="tab" aria-selected="false">Additional</button></div><div class="panel" data-panel="basic"><div class="field-grid"><div class="field"><b>Customer ID</b>${esc(c.customer_id)}</div><div class="field"><b>Price / Box type</b>${esc(c.price_type)} / ${esc(c.box_type)}</div><div class="field"><b>Address</b>${esc([c.address_1,c.address_2,c.address_3].filter(Boolean).join(', '))||'—'}</div><div class="field"><b>Contact</b>${esc(c.tel)}${c.fax?' · Fax '+esc(c.fax):''}<br>${esc(c.email)}</div><div class="field"><b>Invoice header / type</b>${esc(c.invoice_header)} / ${esc(c.invoice_type)}</div><div class="field"><b>Invoice year</b>${esc(c.invoice_year)}</div></div></div><div class="panel" data-panel="additional" hidden><div class="field-grid"><div class="field"><b>Exporter</b>${esc(p.export_config.exporter)||'—'}</div><div class="field"><b>Commercial</b>${esc(p.export_config.commercial)||'—'}</div><div class="field"><b>Order settings</b>${esc(p.export_config.order_settings)||'—'}</div><div class="field"><b>HC settings</b>${esc(p.export_config.hc_settings)||'—'}</div><div class="field"><b>AWB configuration</b>${esc(p.export_config.awb_configuration)||'—'}</div></div><div class="card"><h2>Consignees</h2>${p.consignees.map(x=>esc(x.consignee)+' · '+esc(x.country)+(x.province?' · '+esc(x.province):'')).join('<br>')||'—'}</div></div><p class="note-contract">Order and invoice notes stay editable and auditable in Customer. Their consumption is the <b>Order Entry side-panel contract</b> at <code>/customers/api/customers/${encodeURIComponent(c.customer_id)}/order-entry-notes</code>; no Order Entry UI is built here.</p>`;profile.querySelectorAll('.tab').forEach(tab=>tab.onclick=()=>{profile.querySelectorAll('.tab').forEach(x=>x.setAttribute('aria-selected',String(x===tab)));profile.querySelectorAll('.panel').forEach(panel=>panel.hidden=panel.dataset.panel!==tab.dataset.tab)})}document.querySelector('#search').onclick=load;q.addEventListener('keydown',e=>{if(e.key==='Enter')load()});load();</script></body></html>"""


def _customer_explorer_html() -> str:
    """Add the four-tab customer parity shell without changing Core endpoints or records."""
    html = _customer_explorer_html_base()
    html = html.replace('<input id="q" aria-label="Search customers" placeholder="Search customers">', '<input id="q" aria-label="Search customers" list="customer-code-options" role="combobox" aria-autocomplete="list" placeholder="Type a customer code"><datalist id="customer-code-options"></datalist>')
    html = html.replace("let current,noteKind='order',lastNotesTrigger=null;", "let current,noteKind='order',lastNotesTrigger=null,customerCodes=[];")
    html = html.replace("clean(results);d.customers.forEach(c=>results.append(button('',c.customer_id+' · '+c.name+' · Type '+(c.price_type||'—'),()=>open(c.customer_id),'customer')));", "clean(results);customerCodes=d.customers.map(c=>c.customer_id);const codeOptions=$('#customer-code-options');codeOptions.replaceChildren(...customerCodes.map(code=>{const option=document.createElement('option');option.value=code;return option}));d.customers.forEach(c=>results.append(button('',c.customer_id+' · '+c.name+' · Type '+(c.price_type||'—'),()=>open(c.customer_id),'customer')));")
    html = html.replace("$('#search').onclick=load;q.onkeydown=e=>{if(e.key==='Enter')load()};", "$('#search').onclick=load;async function commitCustomerCode(){const typed=q.value.trim().toLowerCase();let match=customerCodes.find(code=>code.toLowerCase()===typed)||customerCodes.find(code=>code.toLowerCase().startsWith(typed));if(!match){await load();match=customerCodes.find(code=>code.toLowerCase()===typed)||customerCodes.find(code=>code.toLowerCase().startsWith(typed))}if(match){q.value=match;open(match)}}q.onkeydown=e=>{if(e.key==='Enter'||e.key==='Tab'){e.preventDefault();commitCustomerCode();if(e.key==='Tab')setTimeout(()=>profile.focus(),0)}};")
    html = html.replace("staff=$('#active-staff'),", "")
    html = html.replace(
        "function tabs(basic,additional){const box=el('div');box.className='tabs';box.setAttribute('role','tablist');[['Basic',basic],['Additional',additional]].forEach(([name,p],i)=>{const b=el('button',name);b.type='button';b.className='tab';b.setAttribute('role','tab');b.setAttribute('aria-selected',String(!i));b.onclick=()=>{box.querySelectorAll('button').forEach(x=>x.setAttribute('aria-selected',String(x===b)));basic.hidden=p!==basic;additional.hidden=p!==additional};box.append(b)});return box}",
        "function tabs(basic,additional,noteOrder,noteInvoice){const box=el('div');box.className='tabs';box.setAttribute('role','tablist');[['Basic',basic],['Additional',additional],['Note - Order',noteOrder],['Note - Invoice',noteInvoice]].forEach(([name,p],i)=>{const b=el('button',name);b.type='button';b.className='tab';b.dataset.tab=p.dataset.tab;b.setAttribute('role','tab');b.setAttribute('aria-selected',String(!i));b.onclick=()=>{box.querySelectorAll('button').forEach(x=>x.setAttribute('aria-selected',String(x===b)));[basic,additional,noteOrder,noteInvoice].forEach(panel=>panel.hidden=panel!==p)};box.append(b)});return box}",
    )
    html = html.replace(
        "function openNotes(kind,trigger){",
        "function notePanel(customerId,kind){const panel=el('section');panel.className='panel';panel.dataset.tab='note-'+kind;panel.hidden=true;const label=kind==='order'?'Order':'Invoice',rows=kind==='order'?current.order_notes:current.invoice_notes;panel.append(el('h2','Note - '+label),el('p','Core-owned '+label.toLowerCase()+' notes. Changes are attributed to the active staff member.'));rows.forEach(note=>panel.append(renderNote(customerId,note,kind)));panel.append(button('manage-'+kind+'-notes','Manage '+label+' Notes',event=>openNotes(kind,event.currentTarget),'secondary'),button('add-'+kind+'-note','Add '+label+' note',async()=>{const body=prompt('Note body');if(body===null)return;try{await post(notePath(customerId),{kind,body,actor:activeStaff()});say(label+' note saved.');open(customerId)}catch(e){say(e.message)}},'secondary'));return panel}function openNotes(kind,trigger){",
    )
    html = html.replace(
        "if(!edit){actions.querySelectorAll('#save-customer,#cancel-customer,#archive-customer').forEach(x=>x.hidden=true);cfg.querySelectorAll('input').forEach(x=>{x.readOnly=true;x.tabIndex=-1});const saveConfig=additional.querySelector('#save-config');if(saveConfig)saveConfig.hidden=true}profile.append(tabs(basic,additional),basic,additional);profile.focus()",
        "const noteOrder=notePanel(c.customer_id,'order'),noteInvoice=notePanel(c.customer_id,'invoice');if(!edit){actions.querySelectorAll('#save-customer,#cancel-customer,#archive-customer').forEach(x=>x.hidden=true);cfg.querySelectorAll('input').forEach(x=>{x.readOnly=true;x.tabIndex=-1});[additional,noteOrder,noteInvoice].forEach(panel=>panel.querySelectorAll('button').forEach(button=>button.hidden=true));const saveConfig=additional.querySelector('#save-config');if(saveConfig)saveConfig.hidden=true}profile.append(tabs(basic,additional,noteOrder,noteInvoice),basic,additional,noteOrder,noteInvoice);profile.focus()",
    )
    html = html.replace(
        "function activeStaff(){return staff.value}",
        "function activeStaff(){return window.apcCoreActiveStaff||''}",
    )
    html = re.sub(r"async function staffLoad\(\)\{.*?\}\}\$\('#search'\)", "$('#search')", html)
    html = html.replace("basic.append(grid);", "function choiceField(field,label,entries,match){const card=[...grid.children].find(node=>node.querySelector('b')?.textContent===field.replaceAll('_',' '));if(!card)return;card.dataset.choiceField=field;card.querySelector('b').textContent=label;const raw=card.querySelector('[name=\"'+field+'\"]'),value=(raw?.value||card.querySelector('span')?.textContent||'').trim(),group=el('div');group.className='radio-group';entries.forEach(([text,stored])=>{const labelNode=el('label'),radio=el('input');radio.type='radio';radio.name=field+'-choice';radio.value=stored;radio.checked=match(value,stored);radio.disabled=!edit;radio.onchange=()=>{if(raw&&stored){raw.value=stored;raw.focus()}};labelNode.append(radio,document.createTextNode(' '+text));group.append(labelNode)});card.append(group)}choiceField('price_type','Customer Type',[['Grower','1'],['Wholeseller','1.12'],['Retail','1.2'],['THB','THB'],['SGD','1.41']],(value,stored)=>stored==='THB'?Number(value)>30:value===stored);choiceField('box_type','Box Type',[['Paper','Paper'],['Foam','Foam'],['Season','Season'],['Bag','Bag']],(value,stored)=>value===stored);basic.append(grid);")
    html = html.replace(".customer{display:block;width:100%;text-align:left", ".customer{display:grid;grid-template-columns:72px minmax(0,1fr) 72px;gap:10px;align-items:center;width:100%;text-align:left")
    html = html.replace("d.customers.forEach(c=>results.append(button('',c.customer_id+' · '+c.name+' · Type '+(c.price_type||'—'),()=>open(c.customer_id),'customer')));", "d.customers.forEach(c=>{const row=button('', '',()=>open(c.customer_id),'customer');const code=el('span',c.customer_id),name=el('span',c.name),kind=el('span','Type '+(c.price_type||'—'));code.className='customer-code';name.className='customer-name';kind.className='customer-type';row.append(code,name,kind);results.append(row)});")
    html = html.replace('<section class="list"><div id="results"', '<section class="list"><div class="customer-list-header"><span>Code</span><span>Name</span><span>Type</span></div><div id="results"')
    html = html.replace(".customer{display:grid;grid-template-columns:72px minmax(0,1fr) 72px;", ".customer-list-header,.customer{display:grid;grid-template-columns:72px minmax(0,1fr) 72px;gap:10px;align-items:center}.customer-list-header{position:sticky;top:0;z-index:1;padding:7px 9px;background:var(--paper);border-bottom:1px solid var(--line);font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase}.customer{")
    html = html.replace("kind=el('span','Type '+(c.price_type||'—'));", "kind=el('span',c.price_type||'—');")
    html = html.replace(".customer-list-header{position:sticky;top:0", ".customer-list-header{position:sticky;top:58px")
    html = html.replace(".drawer{position:fixed", ".radio-group{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:8px}.radio-group label{display:flex;align-items:center;gap:4px;font-size:12px}.radio-group input{appearance:auto;width:16px;height:16px;padding:0;min-width:16px;flex:0;border:initial;border-radius:50%}.drawer{position:fixed")
    # Staff screens use concise operational status; source evidence remains internal.
    html = html.replace("Checking reconciliation status…", "Checking data status…")
    html = html.replace("Customer reads never start reconciliation.", "")
    html = html.replace("Customer data ready", "Ready")
    html = html.replace("Reconciliation required", "Data check required")
    html = html.replace("Customer data matches accepted artifact.", "Customer records are ready.")
    html = html.replace("Customer data may be stale. Reads will not start reconciliation.", "Customer records need attention.")
    html = html.replace("Core-owned customer records", "Customer records")
    html = html.replace("staffLoad();", "")
    return _staff_identity_shell(html)


def make_handler(explorer: ItemExplorer, manifest: dict, customer_explorer=None, customer_price_module=None, *, customer_lan_ingress: bool = False):
    def _canonical_program_path(path: str) -> str:
        """Accept the canonical /program/ mount while keeping proxy-stripped routes compatible."""
        return path.removeprefix("/program") if path.startswith("/program/") else path

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
            if parsed.path == "/program":
                self.send_response(HTTPStatus.PERMANENT_REDIRECT)
                self.send_header("Location", "/program/")
                self.end_headers()
                return
            parsed = parsed._replace(path=_canonical_program_path(parsed.path))
            price_read_path = (parsed.path == "/customer-prices/" or parsed.path == "/customer-prices/api/staff" or parsed.path == "/customer-prices/api/customers" or parsed.path.startswith("/customer-prices/api/customers/"))
            if customer_price_module is not None and price_read_path and not _customer_client_allowed(self.client_address[0], customer_lan_ingress):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "customer price access is loopback-only unless customer LAN ingress is enabled"})
                return
            customer_read_path = (
                parsed.path == "/customers/"
                or parsed.path == "/customers/api/staff"
                or parsed.path == "/customers/api/reconciliation-status"
                or parsed.path == "/customers/api/customers"
                or parsed.path.startswith("/customers/api/customers/")
            )
            if customer_explorer is not None and customer_read_path and not _customer_client_allowed(self.client_address[0], customer_lan_ingress):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "customer access is loopback-only unless customer LAN ingress is enabled"})
                return
            if parsed.path == "/":
                body = _menu_html().encode("utf-8")
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if parsed.path == "/items/":
                body = _item_explorer_html().encode("utf-8")
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if customer_price_module is not None and parsed.path == "/customer-prices/":
                body = _staff_identity_shell(customer_price_module.html()).encode("utf-8")
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if customer_price_module is not None and parsed.path == "/customer-prices/api/staff":
                self._send_json(
                    HTTPStatus.OK,
                    {"staff": [{"username": username, "role": role} for username, role in explorer._local_store().active_staff()]},
                )
                return
            if customer_price_module is not None and parsed.path == "/customer-prices/api/customers":
                self._send_json(HTTPStatus.OK, {"customer_codes": customer_price_module.customer_codes()})
                return
            if customer_price_module is not None and parsed.path.startswith("/customer-prices/api/customers/"):
                try:
                    suffix = parsed.path.removeprefix("/customer-prices/api/customers/")
                    self._send_json(HTTPStatus.OK, customer_price_module.search(unquote(suffix), parse_qs(parsed.query).get("q", [""])[0]))
                except (ValueError, sqlite3.Error):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid customer price query"})
                return
            if customer_explorer is not None and parsed.path == "/customers/":
                body = _customer_explorer_html().encode("utf-8")
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if customer_explorer is not None and parsed.path == "/customers/api/reconciliation-status":
                self._send_json(HTTPStatus.OK, customer_explorer.reconciliation_status())
                return
            if customer_explorer is not None and parsed.path == "/customers/api/staff":
                self._send_json(HTTPStatus.OK, {"staff": customer_explorer.active_staff()})
                return
            if customer_explorer is not None and (parsed.path == "/customers/api/customers" or parsed.path.startswith("/customers/api/customers/")):
                try:
                    suffix = parsed.path.removeprefix("/customers/api/customers").lstrip("/")
                    if suffix.endswith("/order-entry-notes"):
                        customer_id = unquote(suffix.removesuffix("/order-entry-notes"))
                        self._send_json(HTTPStatus.OK, customer_explorer.order_entry_note_panel(customer_id))
                    elif suffix:
                        self._send_json(HTTPStatus.OK, customer_explorer.profile(unquote(suffix)))
                    else:
                        query = parse_qs(parsed.query)
                        self._send_json(HTTPStatus.OK, customer_explorer.search(query.get("q", [""])[0], query.get("limit", [250])[0], query.get("offset", [0])[0]))
                except (ValueError, sqlite3.Error):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid customer query"})
                return
            if parsed.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "ok", "mode": "local_core"}); return
            api_path = parsed.path.removeprefix("/items")
            if api_path == "/api/staff":
                self._send_json(
                    HTTPStatus.OK,
                    {"staff": [{"username": username, "role": role} for username, role in explorer._local_store().active_staff()]},
                )
                return
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
            customer_path = _canonical_program_path(urlparse(self.path).path)
            if customer_price_module is not None and customer_path.startswith("/customer-prices/api/customers/"):
                if not _customer_client_allowed(self.client_address[0], customer_lan_ingress):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "customer price mutations are loopback-only unless customer LAN ingress is enabled"})
                    return
                try:
                    content_length = int(self.headers.get("Content-Length", "-1"))
                    if content_length < 0 or content_length > 200_000:
                        raise ValueError
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if type(payload) is not dict:
                        raise ValueError
                    suffix = customer_path.removeprefix("/customer-prices/api/customers/")
                    parts = tuple(unquote(part) for part in suffix.split("/") if part)
                    if len(parts) == 3 and parts[1:] == ("paste", "preview") and set(payload) == {"tsv"}:
                        self._send_json(HTTPStatus.OK, customer_price_module.preview_tsv(parts[0], payload["tsv"]))
                    elif len(parts) == 3 and parts[1:] == ("paste", "apply") and set(payload) == {"preview_id", "actor"}:
                        self._send_json(HTTPStatus.OK, customer_price_module.apply_preview_id(parts[0], payload["preview_id"], payload["actor"]))
                    elif len(parts) == 3 and parts[1] == "items" and set(payload) == {"price", "actor"}:
                        self._send_json(HTTPStatus.OK, {"row": customer_price_module.edit(parts[0], parts[2], payload["price"], payload["actor"])})
                    else:
                        raise ValueError
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, sqlite3.Error):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid customer price mutation"})
                return
            if customer_explorer is not None and (customer_path == "/customers/api/customers" or customer_path.startswith("/customers/api/customers/")):
                if not _customer_client_allowed(self.client_address[0], customer_lan_ingress):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "customer mutations are loopback-only unless customer LAN ingress is enabled"})
                    return
                try:
                    content_length = int(self.headers.get("Content-Length", "-1"))
                    if content_length < 0 or content_length > 8192: raise ValueError
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if type(payload) is not dict: raise ValueError
                    actor = payload.pop("actor", None)
                    suffix = customer_path.removeprefix("/customers/api/customers").lstrip("/")
                    parts = tuple(unquote(part) for part in suffix.split("/") if part)
                    if not parts:
                        self._send_json(HTTPStatus.CREATED, {"customer": customer_explorer.create(payload, actor)})
                    elif len(parts) == 2 and parts[1] == "archive":
                        self._send_json(HTTPStatus.OK, {"customer": customer_explorer.archive(parts[0], actor)})
                    elif len(parts) > 1:
                        self._send_json(HTTPStatus.OK, {"result": customer_explorer.mutate_child(parts[0], parts[1:], payload, actor)})
                    else:
                        self._send_json(HTTPStatus.OK, {"customer": customer_explorer.edit(parts[0], payload, actor)})
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, sqlite3.Error):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid customer mutation"})
                return
            api_path = _canonical_program_path(urlparse(self.path).path).removeprefix("/items")
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
