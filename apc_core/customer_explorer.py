"""Core-owned Customer Explorer for immutable accepted Mini/VB6 SQLite snapshots.

The customer master, contacts, consignees and notes are copied into Core-owned
SQLite tables. No pricing relations are included.
"""

import hashlib
import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

from apc_core.item_explorer import CoreStore, display_text


CUSTOMER_FIELDS = (
    "name", "address_1", "address_2", "address_3", "tel", "fax", "email",
    "price_type", "box_type", "invoice_header", "invoice_type", "invoice_year",
)
CONFIG_FIELDS = (
    "exporter", "commercial", "order_settings", "hc_settings", "awb_configuration",
    "order_clean", "order_sticker", "hc_exporter_address", "awb_formula_type", "awb_rate", "awb_charges",
)
CONSIGNEE_FIELDS = ("consignee", "consignee_address", "country", "province", "broker", "flight", "time", "hc_set_2")

_CUSTOMER_COLUMNS = {
    "customer_id": ("Cust ID", "Customer ID"), "name": ("Name",),
    "address_1": ("Address 1", "Address1", "Add1"), "address_2": ("Address 2", "Address2", "Add2"),
    "address_3": ("Address 3", "Address3", "Add3"), "tel": ("Tel", "Telephone"), "fax": ("Fax",),
    "email": ("Email",), "price_type": ("Price Type",), "box_type": ("BoxType", "Box Type"),
    "invoice_header": ("Inv Header", "Invoice Header"), "invoice_type": ("Inv Type", "Invoice Type"),
    "invoice_year": ("Year No", "Inv Year", "Invoice Year", "This Yr No"),
}
_CONFIG_COLUMNS = {
    "customer_id": ("Cust ID", "Customer ID"), "exporter": ("Exporter",), "commercial": ("Commercial", "Com Code"),
    "order_settings": ("Order Settings", "Order"), "hc_settings": ("HC Settings", "HC"),
    "awb_configuration": ("AWB Configuration", "AWB"),
    "order_clean": ("Clean",), "order_sticker": ("Sticker",), "hc_exporter_address": ("Exporter Add",),
    "awb_formula_type": ("Formula Type",), "awb_rate": ("RATE", "Rate"), "awb_charges": ("Charges",),
}
_CONSIGNEE_COLUMNS = {
    "customer_id": ("Cust ID", "Customer ID"), "consignee": ("Consignee",), "consignee_address": ("Con Add", "Consignee Address"),
    "country": ("Country",), "province": ("Province",), "broker": ("Broker",), "flight": ("Flight",),
    "time": ("Time",), "hc_set_2": ("HC Set2", "HC Set 2"),
}
_NOTE_COLUMNS = {"customer_id": ("Cust ID", "Customer ID"), "note_type": ("Note Type", "Type"), "body": ("Note", "Body")}


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _rows(connection: sqlite3.Connection, table: str, mapping: dict[str, tuple[str, ...]]) -> list[dict[str, str]]:
    if not _table_exists(connection, table):
        return []
    actual = {str(row[1]).casefold(): str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
    selected: dict[str, str] = {}
    for field, aliases in mapping.items():
        selected[field] = next((actual[name.casefold()] for name in aliases if name.casefold() in actual), "")
    columns = [column for column in dict.fromkeys(selected.values()) if column]
    if not columns:
        return []
    query = ", ".join(f'"{column}"' for column in columns)
    result = []
    for source_row in connection.execute(f'SELECT {query} FROM "{table}"'):
        raw = dict(zip(columns, source_row))
        result.append({field: display_text(raw.get(column, "")).strip() if column else "" for field, column in selected.items()})
    return result


def _note_rows(connection: sqlite3.Connection) -> list[dict[str, str]]:
    """Normalize canonical row notes and the verified VB6 wide Invoice/Order notes."""
    if not _table_exists(connection, "MainDB__CUST_NOTE"):
        return []
    actual = {str(row[1]).casefold(): str(row[1]) for row in connection.execute('PRAGMA table_info("MainDB__CUST_NOTE")')}
    if "invoice" not in actual and "order" not in actual:
        return _rows(connection, "MainDB__CUST_NOTE", _NOTE_COLUMNS)
    customer_column = next((actual[name.casefold()] for name in _NOTE_COLUMNS["customer_id"] if name.casefold() in actual), "")
    if not customer_column:
        return []
    selected = [customer_column, *[actual[name] for name in ("order", "invoice") if name in actual]]
    result = []
    query = ", ".join(f'"{column}"' for column in selected)
    for source_row in connection.execute(f'SELECT {query} FROM "MainDB__CUST_NOTE"'):
        raw = dict(zip(selected, source_row))
        customer_id = display_text(raw.get(customer_column, "")).strip()
        for note_type in ("order", "invoice"):
            column = actual.get(note_type)
            body = display_text(raw.get(column, "")).strip() if column else ""
            if body:
                result.append({"customer_id": customer_id, "note_type": note_type, "body": body})
    return result


class CustomerStore:
    """Core-owned customer persistence and audit; never opens a source artifact."""

    def __init__(self, data_dir: Path):
        self.shared = CoreStore(data_dir)
        self.connection = self.shared.connection
        customer_columns = ", ".join(f"{field} TEXT NOT NULL DEFAULT ''" for field in CUSTOMER_FIELDS)
        config_columns = ", ".join(f"{field} TEXT NOT NULL DEFAULT ''" for field in CONFIG_FIELDS)
        consignee_columns = ", ".join(f"{field} TEXT NOT NULL DEFAULT ''" for field in CONSIGNEE_FIELDS)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS core_customers (customer_id TEXT PRIMARY KEY, source_customer_id TEXT, "
            "source_artifact_path TEXT, source_artifact_sha256 TEXT, imported_at TEXT, core_created INTEGER NOT NULL "
            "CHECK(core_created IN (0,1)), archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)), "
            + customer_columns + ", created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self.connection.execute("CREATE TABLE IF NOT EXISTS customer_export_config (customer_id TEXT PRIMARY KEY, core_created INTEGER NOT NULL DEFAULT 0 CHECK(core_created IN (0,1)), archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)), " + config_columns + ")")
        config_names = {row[1] for row in self.connection.execute("PRAGMA table_info(customer_export_config)")}
        for field in (*CONFIG_FIELDS, "archived"):
            if field not in config_names:
                self.connection.execute(f"ALTER TABLE customer_export_config ADD COLUMN {field} " + ("INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))" if field == "archived" else "TEXT NOT NULL DEFAULT ''"))
        self.connection.execute("CREATE TABLE IF NOT EXISTS customer_consignees (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, source_key TEXT, core_created INTEGER NOT NULL DEFAULT 0 CHECK(core_created IN (0,1)), archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)), " + consignee_columns + ", UNIQUE(customer_id, source_key))")
        consignee_names = {row[1] for row in self.connection.execute("PRAGMA table_info(customer_consignees)")}
        for field in CONSIGNEE_FIELDS:
            if field not in consignee_names:
                self.connection.execute(f"ALTER TABLE customer_consignees ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        self.connection.execute("CREATE TABLE IF NOT EXISTS customer_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, note_kind TEXT NOT NULL CHECK(note_kind IN ('order','invoice')), source_key TEXT, core_created INTEGER NOT NULL DEFAULT 0 CHECK(core_created IN (0,1)), archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)), body TEXT NOT NULL, UNIQUE(customer_id, source_key))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS customer_field_provenance (customer_id TEXT NOT NULL, field_name TEXT NOT NULL, PRIMARY KEY(customer_id, field_name))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS customer_quarantine (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, reason TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS customer_reconciliation_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), source_artifact_sha256 TEXT NOT NULL, reconciled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS customer_activity (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, action TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, actor_username TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        self.connection.commit()

    def require_actor(self, actor: object) -> str:
        return self.shared.require_active_actor(actor)

    def close(self) -> None:
        self.shared.close()

    def customer(self, customer_id: str) -> dict[str, object] | None:
        columns = ["customer_id", "source_customer_id", "source_artifact_path", "source_artifact_sha256", "imported_at", "core_created", "archived", *CUSTOMER_FIELDS]
        row = self.connection.execute(f"SELECT {', '.join(columns)} FROM core_customers WHERE customer_id=?", (customer_id,)).fetchone()
        if row is None:
            return None
        result = dict(zip(columns, row)); result["core_created"] = bool(result["core_created"]); result["archived"] = bool(result["archived"])
        return result

    def visible_customers(self, include_archived: bool = False) -> list[dict[str, object]]:
        where = "" if include_archived else " WHERE archived=0"
        ids = [row[0] for row in self.connection.execute("SELECT customer_id FROM core_customers" + where + " ORDER BY customer_id")]
        return [customer for customer_id in ids if (customer := self.customer(customer_id)) is not None]

    def audit(self, customer_id: str, action: str, before: dict, after: dict, actor: str) -> None:
        self.connection.execute("INSERT INTO customer_activity(customer_id,action,before_json,after_json,actor_username) VALUES (?,?,?,?,?)", (customer_id, action, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), actor))

    def clear_active_quarantine(self) -> None:
        self.connection.execute("UPDATE customer_quarantine SET active=0 WHERE active=1")

    def quarantine(self, customer_id: str, reason: str) -> None:
        self.connection.execute("INSERT INTO customer_quarantine(customer_id,reason,active) VALUES (?,?,1)", (customer_id, reason))

    def reconciled_source_sha256(self) -> str | None:
        row = self.connection.execute("SELECT source_artifact_sha256 FROM customer_reconciliation_state WHERE singleton=1").fetchone()
        return None if row is None else str(row[0])

    def record_reconciliation(self, source_sha256: str) -> None:
        self.connection.execute(
            "INSERT INTO customer_reconciliation_state(singleton,source_artifact_sha256,reconciled_at) VALUES (1,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(singleton) DO UPDATE SET source_artifact_sha256=excluded.source_artifact_sha256,reconciled_at=CURRENT_TIMESTAMP",
            (source_sha256,),
        )


class CustomerExplorer:
    """Conflict-safe Core customer domain sourced only from a read-only snapshot."""

    def __init__(self, source_path: Path, data_dir: Path | None = None):
        self.source_path = Path(source_path)
        self.data_dir = Path(data_dir or os.environ.get("APC_CORE_DATA_DIR", "state"))
        descriptor = os.open(self.source_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("customer source must be a regular SQLite file")
            self._connection = sqlite3.connect(f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            self._connection.execute("PRAGMA query_only=ON")
            digest = hashlib.sha256()
            with os.fdopen(os.dup(descriptor), "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            self._source_sha256 = digest.hexdigest()
        finally:
            os.close(descriptor)
        if not _table_exists(self._connection, "MainDB__CUST"):
            raise ValueError("customer source lacks MainDB__CUST")
        customer_columns = {str(row[1]) for row in self._connection.execute('PRAGMA table_info("MainDB__CUST")')}
        if not {"Cust ID", "Name"}.issubset(customer_columns):
            raise ValueError("customer source lacks required customer columns")
        self._lock = threading.RLock()
        self._store: CustomerStore | None = None
        self._snapshot_reconciled = False

    def _local_store(self) -> CustomerStore:
        if self._store is None:
            self._store = CustomerStore(self.data_dir)
        return self._store

    def close(self) -> None:
        self._connection.close()
        if self._store is not None:
            self._store.close()

    def _snapshot(self) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
        with self._lock:
            return (_rows(self._connection, "MainDB__CUST", _CUSTOMER_COLUMNS), _rows(self._connection, "MainDB__CUST_CON", _CONFIG_COLUMNS), _rows(self._connection, "MainDB__CUST_CONSIGNEE", _CONSIGNEE_COLUMNS), _note_rows(self._connection))

    def reconciliation_status(self) -> dict[str, str]:
        """Report whether this accepted artifact was explicitly reconciled; never reconcile on a read."""
        with self._lock:
            state = "ready" if self._local_store().reconciled_source_sha256() == self._source_sha256 else "reconciliation_required"
            return {"state": state, "source_sha256": self._source_sha256}

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._snapshot_reconciled:
                if self._local_store().reconciled_source_sha256() != self._source_sha256:
                    self.backfill_from_snapshot()
                else:
                    self._snapshot_reconciled = True

    def _upsert_source_children(self, customer_id: str, configs: list[dict[str, str]], consignees: list[dict[str, str]], notes: list[dict[str, str]], counts: dict[str, int]) -> None:
        store = self._local_store(); con = store.connection
        matching_config = [row for row in configs if row["customer_id"] == customer_id]
        if len(matching_config) > 1:
            store.quarantine(customer_id, "duplicate_config"); counts["malformed"] += 1
            con.execute("UPDATE customer_export_config SET archived=1 WHERE customer_id=? AND core_created=0", (customer_id,))
        elif matching_config:
            values = matching_config[0]
            existing = con.execute("SELECT customer_id FROM customer_export_config WHERE customer_id=?", (customer_id,)).fetchone()
            if existing is None:
                con.execute("INSERT INTO customer_export_config(customer_id,core_created," + ",".join(CONFIG_FIELDS) + ") VALUES (? ,0," + ",".join("?" for _ in CONFIG_FIELDS) + ")", [customer_id, *[values[field] for field in CONFIG_FIELDS]])
            else:
                con.execute("UPDATE customer_export_config SET " + ",".join(f"{field}=?" for field in CONFIG_FIELDS) + " WHERE customer_id=? AND core_created=0 AND archived=0", [*[values[field] for field in CONFIG_FIELDS], customer_id])
        else:
            con.execute("UPDATE customer_export_config SET archived=1 WHERE customer_id=? AND core_created=0", (customer_id,))
        matching_consignees = [row for row in consignees if row["customer_id"] == customer_id]
        consignee_keys = [row["consignee"].casefold() for row in matching_consignees]
        if not all(consignee_keys):
            store.quarantine(customer_id, "blank_consignee"); counts["malformed"] += 1
            con.execute("UPDATE customer_consignees SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL", (customer_id,))
        elif len(consignee_keys) != len(set(consignee_keys)):
            store.quarantine(customer_id, "duplicate_consignee"); counts["malformed"] += 1
            con.execute("UPDATE customer_consignees SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL", (customer_id,))
        else:
            source_consignee_keys = set(consignee_keys)
            for row, key in zip(matching_consignees, consignee_keys):
                existing = con.execute("SELECT id FROM customer_consignees WHERE customer_id=? AND source_key=?", (customer_id, key)).fetchone()
                if existing is None:
                    con.execute("INSERT INTO customer_consignees(customer_id,source_key,core_created," + ",".join(CONSIGNEE_FIELDS) + ") VALUES (?, ?, 0," + ",".join("?" for _ in CONSIGNEE_FIELDS) + ")", [customer_id, key, *[row[field] for field in CONSIGNEE_FIELDS]])
                else:
                    con.execute("UPDATE customer_consignees SET " + ",".join(f"{field}=?" for field in CONSIGNEE_FIELDS) + " WHERE customer_id=? AND source_key=? AND core_created=0 AND archived=0", [*[row[field] for field in CONSIGNEE_FIELDS], customer_id, key])
            if source_consignee_keys:
                con.execute("UPDATE customer_consignees SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL AND source_key NOT IN (" + ",".join("?" for _ in source_consignee_keys) + ")", (customer_id, *source_consignee_keys))
            else:
                con.execute("UPDATE customer_consignees SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL", (customer_id,))
        matching_notes = [row for row in notes if row["customer_id"] == customer_id]
        note_values = [(row["note_type"].strip().casefold(), row["body"]) for row in matching_notes]
        note_keys = [f"{kind}:{body}" for kind, body in note_values]
        if any(kind not in {"order", "invoice"} or not body for kind, body in note_values):
            store.quarantine(customer_id, "malformed_note"); counts["malformed"] += 1
            con.execute("UPDATE customer_notes SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL", (customer_id,))
        elif len(note_keys) != len(set(note_keys)):
            store.quarantine(customer_id, "duplicate_note"); counts["malformed"] += 1
            con.execute("UPDATE customer_notes SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL", (customer_id,))
        else:
            source_note_keys = set(note_keys)
            for row, (kind, body), key in zip(matching_notes, note_values, note_keys):
                existing = con.execute("SELECT id FROM customer_notes WHERE customer_id=? AND source_key=?", (customer_id, key)).fetchone()
                if existing is None:
                    con.execute("INSERT INTO customer_notes(customer_id,note_kind,source_key,core_created,body) VALUES (?,?,?,0,?)", (customer_id, kind, key, body))
                else:
                    con.execute("UPDATE customer_notes SET note_kind=?,body=? WHERE customer_id=? AND source_key=? AND core_created=0 AND archived=0", (kind, body, customer_id, key))
            if source_note_keys:
                con.execute("UPDATE customer_notes SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL AND source_key NOT IN (" + ",".join("?" for _ in source_note_keys) + ")", (customer_id, *source_note_keys))
            else:
                con.execute("UPDATE customer_notes SET archived=1 WHERE customer_id=? AND core_created=0 AND source_key IS NOT NULL", (customer_id,))

    def backfill_from_snapshot(self) -> dict[str, int]:
        store = self._local_store(); customers, configs, consignees, notes = self._snapshot()
        counts = {"accepted": 0, "duplicate": 0, "unmatched": 0, "malformed": 0, "preserved": 0}
        ids = [row["customer_id"] for row in customers]
        known = {customer_id for customer_id in ids if customer_id}
        duplicates = {customer_id for customer_id in known if ids.count(customer_id) > 1}
        with store.connection:
            store.clear_active_quarantine()
            admitted_source_ids: set[str] = set()
            for row in customers:
                customer_id = row["customer_id"]
                if not customer_id:
                    store.quarantine("", "blank_customer_id"); counts["unmatched"] += 1; continue
                if customer_id in duplicates:
                    store.quarantine(customer_id, "duplicate_customer_id"); counts["duplicate"] += 1; continue
                existing = store.customer(customer_id)
                if existing is not None and existing["core_created"]:
                    store.quarantine(customer_id, "source_collision_core_created")
                    counts["unmatched"] += 1
                    continue
                if existing is None:
                    cols = ["customer_id", "source_customer_id", "source_artifact_path", "source_artifact_sha256", "imported_at", "core_created", "archived", *CUSTOMER_FIELDS]
                    store.connection.execute("INSERT INTO core_customers(" + ",".join(cols) + ") VALUES (?,?,?,?,CURRENT_TIMESTAMP,0,0," + ",".join("?" for _ in CUSTOMER_FIELDS) + ")", [customer_id, customer_id, str(self.source_path), self._source_sha256, *[row[field] for field in CUSTOMER_FIELDS]])
                else:
                    protected = {field for field, in store.connection.execute("SELECT field_name FROM customer_field_provenance WHERE customer_id=?", (customer_id,))}
                    updates = {field: row[field] for field in CUSTOMER_FIELDS if field not in protected and existing[field] != row[field]}
                    counts["preserved"] += sum(1 for field in CUSTOMER_FIELDS if field in protected and existing[field] != row[field])
                    if not existing["core_created"]:
                        if updates:
                            store.connection.execute("UPDATE core_customers SET " + ",".join(f"{field}=?" for field in updates) + ",updated_at=CURRENT_TIMESTAMP WHERE customer_id=?", [*updates.values(), customer_id])
                        store.connection.execute("UPDATE core_customers SET source_artifact_path=?,source_artifact_sha256=?,imported_at=CURRENT_TIMESTAMP WHERE customer_id=?", (str(self.source_path), self._source_sha256, customer_id))
                self._upsert_source_children(customer_id, configs, consignees, notes, counts)
                admitted_source_ids.add(customer_id)
                counts["accepted"] += 1
            if admitted_source_ids:
                store.connection.execute(
                    "UPDATE core_customers SET archived=1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE core_created=0 AND source_customer_id IS NOT NULL AND source_customer_id NOT IN ("
                    + ",".join("?" for _ in admitted_source_ids) + ")",
                    tuple(admitted_source_ids),
                )
            else:
                store.connection.execute(
                    "UPDATE core_customers SET archived=1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE core_created=0 AND source_customer_id IS NOT NULL"
                )
            for row in [*configs, *consignees, *notes]:
                if row["customer_id"] and row["customer_id"] not in known:
                    store.quarantine(row["customer_id"], "orphan_consignee" if "consignee" in row else "orphan_child"); counts["unmatched"] += 1
            store.record_reconciliation(self._source_sha256)
        self._snapshot_reconciled = True
        return counts

    def refresh_from_snapshot(self) -> dict[str, int]:
        return self.backfill_from_snapshot()

    def search(self, query: str = "", limit: int = 250, offset: int = 0) -> dict[str, object]:
        limit = max(1, min(int(limit), 250)); offset = max(0, int(offset)); term = (query or "").strip().casefold()
        customers = self._local_store().visible_customers()
        if term:
            customers = [row for row in customers if any(term in str(row.get(field, "")).casefold() for field in ("customer_id", "name", "address_1", "tel", "email", "price_type", "box_type"))]
        total = len(customers); next_offset = offset + limit; return {"total": total, "limit": limit, "offset": offset, "has_more": next_offset < total, "next_offset": next_offset if next_offset < total else None, "customers": customers[offset:next_offset]}

    def active_staff(self) -> list[dict[str, str]]:
        """Read-only Core staff identity for mutation attribution selection."""
        return [{"username": username, "role": role} for username, role in self._local_store().shared.active_staff()]

    def profile(self, customer_id: str, *, include_archived: bool = False) -> dict[str, object]:
        store = self._local_store(); customer = store.customer(customer_id)
        if customer is None or (customer["archived"] and not include_archived):
            raise ValueError("unknown customer")
        con = store.connection
        config = con.execute("SELECT " + ",".join(CONFIG_FIELDS) + " FROM customer_export_config WHERE customer_id=? AND archived=0", (customer_id,)).fetchone()
        consignees = [dict(zip(("id", *CONSIGNEE_FIELDS), row)) for row in con.execute("SELECT id," + ",".join(CONSIGNEE_FIELDS) + " FROM customer_consignees WHERE customer_id=? AND archived=0 ORDER BY id", (customer_id,))]
        note_rows = [(note_id, kind, body) for note_id, kind, body in con.execute("SELECT id,note_kind,body FROM customer_notes WHERE customer_id=? AND archived=0 ORDER BY id", (customer_id,))]
        return {"customer": customer, "export_config": dict(zip(CONFIG_FIELDS, config or ("",) * len(CONFIG_FIELDS))), "consignees": consignees, "order_notes": [{"id": note_id, "body": body} for note_id, kind, body in note_rows if kind == "order"], "invoice_notes": [{"id": note_id, "body": body} for note_id, kind, body in note_rows if kind == "invoice"]}

    def order_entry_note_panel(self, customer_id: str) -> dict[str, object]:
        """Read contract for a future Order Entry side panel; not an Order UI."""
        profile = self.profile(customer_id)
        return {
            "customer_id": profile["customer"]["customer_id"],
            "customer_name": profile["customer"]["name"],
            "order_notes": profile["order_notes"],
            "invoice_notes": profile["invoice_notes"],
            "consumption": "future_order_entry_side_panel",
        }

    def create(self, customer: dict[str, object], actor_username: object = None) -> dict[str, object]:
        self._ensure_loaded()
        if type(customer) is not dict or type(customer.get("customer_id")) is not str or type(customer.get("name")) is not str:
            raise ValueError("invalid customer create")
        actor = self._local_store().require_actor(actor_username); customer_id = customer["customer_id"].strip(); name = customer["name"].strip()
        if not customer_id or not name or len(customer_id) > 500 or len(name) > 500 or self._local_store().customer(customer_id) is not None:
            raise ValueError("invalid customer create")
        values = {field: str(customer.get(field, "")).strip() for field in CUSTOMER_FIELDS}
        with self._local_store().connection:
            self._local_store().connection.execute("INSERT INTO core_customers(customer_id,source_customer_id,core_created,archived," + ",".join(CUSTOMER_FIELDS) + ") VALUES (?,NULL,1,0," + ",".join("?" for _ in CUSTOMER_FIELDS) + ")", [customer_id, *[values[field] for field in CUSTOMER_FIELDS]])
            self._local_store().audit(customer_id, "created", {}, {"created": True}, actor)
        return self._local_store().customer(customer_id) or {}

    def edit(self, customer_id: str, changes: dict[str, object], actor_username: object = None) -> dict[str, object]:
        self._ensure_loaded()
        if type(customer_id) is not str or type(changes) is not dict or not changes or set(changes) - set(CUSTOMER_FIELDS):
            raise ValueError("invalid customer edit")
        actor = self._local_store().require_actor(actor_username); current = self._local_store().customer(customer_id)
        if current is None or any(type(value) is not str or len(value.strip()) > 500 for value in changes.values()):
            raise ValueError("invalid customer edit")
        clean = {field: value.strip() for field, value in changes.items()}
        with self._local_store().connection:
            self._local_store().connection.execute("UPDATE core_customers SET " + ",".join(f"{field}=?" for field in clean) + ",updated_at=CURRENT_TIMESTAMP WHERE customer_id=?", [*clean.values(), customer_id])
            self._local_store().connection.executemany("INSERT OR IGNORE INTO customer_field_provenance(customer_id,field_name) VALUES (?,?)", [(customer_id, field) for field in clean])
            self._local_store().audit(customer_id, "edit", {field: current[field] for field in clean}, clean, actor)
        return self._local_store().customer(customer_id) or {}

    def archive(self, customer_id: str, actor_username: object = None) -> dict[str, object]:
        self._ensure_loaded()
        actor = self._local_store().require_actor(actor_username); current = self._local_store().customer(customer_id)
        if current is None:
            raise ValueError("unknown customer")
        with self._local_store().connection:
            self._local_store().connection.execute("UPDATE core_customers SET archived=1,updated_at=CURRENT_TIMESTAMP WHERE customer_id=?", (customer_id,))
            self._local_store().audit(customer_id, "archived", {"archived": current["archived"]}, {"archived": True}, actor)
        return self._local_store().customer(customer_id) or {}

    def edit_export_config(self, customer_id: str, changes: dict[str, object], actor_username: object = None) -> dict[str, str]:
        self._ensure_loaded()
        if type(changes) is not dict or not changes or set(changes) - set(CONFIG_FIELDS):
            raise ValueError("invalid export config")
        actor = self._local_store().require_actor(actor_username)
        if self._local_store().customer(customer_id) is None or any(type(value) is not str or len(value.strip()) > 500 for value in changes.values()):
            raise ValueError("invalid export config")
        con = self._local_store().connection
        row = con.execute("SELECT " + ",".join(CONFIG_FIELDS) + " FROM customer_export_config WHERE customer_id=? AND archived=0", (customer_id,)).fetchone()
        before = dict(zip(CONFIG_FIELDS, row or ("",) * len(CONFIG_FIELDS)))
        after = {**before, **{field: value.strip() for field, value in changes.items()}}
        with con:
            con.execute("INSERT INTO customer_export_config(customer_id,core_created,archived," + ",".join(CONFIG_FIELDS) + ") VALUES (?,1,0," + ",".join("?" for _ in CONFIG_FIELDS) + ") ON CONFLICT(customer_id) DO UPDATE SET core_created=1,archived=0," + ",".join(f"{field}=excluded.{field}" for field in CONFIG_FIELDS), [customer_id, *[after[field] for field in CONFIG_FIELDS]])
            self._local_store().audit(customer_id, "export_config_edited", before, after, actor)
        return after

    def archive_export_config(self, customer_id: str, actor_username: object = None) -> None:
        self._ensure_loaded()
        actor = self._local_store().require_actor(actor_username); con = self._local_store().connection
        row = con.execute("SELECT " + ",".join(CONFIG_FIELDS) + " FROM customer_export_config WHERE customer_id=? AND archived=0", (customer_id,)).fetchone()
        if self._local_store().customer(customer_id) is None or row is None:
            raise ValueError("unknown export config")
        before = dict(zip(CONFIG_FIELDS, row))
        with con:
            con.execute("UPDATE customer_export_config SET archived=1 WHERE customer_id=?", (customer_id,))
            self._local_store().audit(customer_id, "export_config_archived", before, {"archived": True}, actor)

    def _child(self, table: str, customer_id: str, child_id: object) -> tuple | None:
        if type(child_id) is not int or isinstance(child_id, bool):
            raise ValueError("invalid child")
        return self._local_store().connection.execute(f"SELECT * FROM {table} WHERE id=? AND customer_id=? AND archived=0", (child_id, customer_id)).fetchone()

    def edit_consignee(self, customer_id: str, child_id: object, changes: dict[str, object], actor_username: object = None) -> dict[str, str]:
        self._ensure_loaded()
        if type(changes) is not dict or not changes or set(changes) - set(CONSIGNEE_FIELDS) or any(type(value) is not str or len(value.strip()) > 500 for value in changes.values()):
            raise ValueError("invalid consignee")
        actor = self._local_store().require_actor(actor_username); row = self._child("customer_consignees", customer_id, child_id)
        if row is None:
            raise ValueError("unknown consignee")
        before = dict(zip(("id", "customer_id", "source_key", "core_created", "archived", *CONSIGNEE_FIELDS), row)); after = {field: changes.get(field, before[field]).strip() for field in CONSIGNEE_FIELDS}
        if not after["consignee"] or not after["country"]:
            raise ValueError("invalid consignee")
        with self._local_store().connection:
            self._local_store().connection.execute("UPDATE customer_consignees SET core_created=1," + ",".join(f"{field}=?" for field in CONSIGNEE_FIELDS) + " WHERE id=?", [*[after[field] for field in CONSIGNEE_FIELDS], child_id])
            self._local_store().audit(customer_id, "consignee_edited", {field: before[field] for field in CONSIGNEE_FIELDS}, after, actor)
        return after

    def archive_consignee(self, customer_id: str, child_id: object, actor_username: object = None) -> None:
        self._ensure_loaded()
        actor = self._local_store().require_actor(actor_username); row = self._child("customer_consignees", customer_id, child_id)
        if row is None:
            raise ValueError("unknown consignee")
        with self._local_store().connection:
            self._local_store().connection.execute("UPDATE customer_consignees SET archived=1 WHERE id=?", (child_id,))
            self._local_store().audit(customer_id, "consignee_archived", {"id": child_id}, {"archived": True}, actor)

    def edit_note(self, customer_id: str, child_id: object, body: object, actor_username: object = None) -> dict[str, str]:
        self._ensure_loaded()
        if type(body) is not str or not body.strip() or len(body.strip()) > 2000:
            raise ValueError("invalid note")
        actor = self._local_store().require_actor(actor_username); row = self._child("customer_notes", customer_id, child_id)
        if row is None:
            raise ValueError("unknown note")
        before = {"body": row[-1]}; after = {"body": body.strip()}
        with self._local_store().connection:
            self._local_store().connection.execute("UPDATE customer_notes SET core_created=1,body=? WHERE id=?", (after["body"], child_id))
            self._local_store().audit(customer_id, "note_edited", before, after, actor)
        return after

    def archive_note(self, customer_id: str, child_id: object, actor_username: object = None) -> None:
        self._ensure_loaded()
        actor = self._local_store().require_actor(actor_username); row = self._child("customer_notes", customer_id, child_id)
        if row is None:
            raise ValueError("unknown note")
        with self._local_store().connection:
            self._local_store().connection.execute("UPDATE customer_notes SET archived=1 WHERE id=?", (child_id,))
            self._local_store().audit(customer_id, "note_archived", {"id": child_id}, {"archived": True}, actor)

    def mutate_child(self, customer_id: str, segments: tuple[str, ...], payload: dict[str, object], actor_username: object = None) -> object:
        if segments == ("export-config",):
            return self.edit_export_config(customer_id, payload, actor_username)
        if segments == ("export-config", "archive") and not payload:
            return self.archive_export_config(customer_id, actor_username)
        if segments == ("consignees",):
            return self.add_consignee(customer_id, payload, actor_username)
        if segments == ("notes",) and set(payload) == {"kind", "body"}:
            return self.add_note(customer_id, payload["kind"], payload["body"], actor_username)
        if len(segments) in {2, 3} and segments[0] in {"consignees", "notes"}:
            try:
                child_id = int(segments[1])
            except ValueError:
                raise ValueError("invalid child") from None
            if segments[0] == "consignees" and len(segments) == 2:
                return self.edit_consignee(customer_id, child_id, payload, actor_username)
            if segments[0] == "notes" and len(segments) == 2 and set(payload) == {"body"}:
                return self.edit_note(customer_id, child_id, payload["body"], actor_username)
            if len(segments) == 3 and segments[2] == "archive" and not payload:
                return self.archive_consignee(customer_id, child_id, actor_username) if segments[0] == "consignees" else self.archive_note(customer_id, child_id, actor_username)
        raise ValueError("invalid customer child mutation")

    def add_consignee(self, customer_id: str, consignee: dict[str, object], actor_username: object = None) -> dict[str, str]:
        self._ensure_loaded()
        actor = self._local_store().require_actor(actor_username)
        if self._local_store().customer(customer_id) is None or type(consignee) is not dict or set(consignee) - set(CONSIGNEE_FIELDS):
            raise ValueError("invalid consignee")
        values = {field: str(consignee.get(field, "")).strip() for field in CONSIGNEE_FIELDS}
        if not values["consignee"] or not values["country"]:
            raise ValueError("invalid consignee")
        with self._local_store().connection:
            self._local_store().connection.execute("INSERT INTO customer_consignees(customer_id,source_key,core_created," + ",".join(CONSIGNEE_FIELDS) + ") VALUES (?,NULL,1," + ",".join("?" for _ in CONSIGNEE_FIELDS) + ")", [customer_id, *[values[field] for field in CONSIGNEE_FIELDS]])
            self._local_store().audit(customer_id, "consignee_created", {}, values, actor)
        return values

    def add_note(self, customer_id: str, kind: object, body: object, actor_username: object = None) -> dict[str, str]:
        self._ensure_loaded()
        actor = self._local_store().require_actor(actor_username)
        if self._local_store().customer(customer_id) is None or kind not in {"order", "invoice"} or type(body) is not str or not body.strip() or len(body.strip()) > 2000:
            raise ValueError("invalid note")
        note = {"kind": kind, "body": body.strip()}
        with self._local_store().connection:
            self._local_store().connection.execute("INSERT INTO customer_notes(customer_id,note_kind,source_key,core_created,body) VALUES (?,?,NULL,1,?)", (customer_id, kind, note["body"]))
            self._local_store().audit(customer_id, "note_created", {}, note, actor)
        return note

    def activity(self, customer_id: str | None = None) -> list[dict[str, object]]:
        where, args = (" WHERE customer_id=?", (customer_id,)) if customer_id else ("", ())
        rows = self._local_store().connection.execute("SELECT customer_id,action,before_json,after_json,actor_username,created_at FROM customer_activity" + where + " ORDER BY id", args)
        return [{"customer_id": row[0], "action": row[1], "before": json.loads(row[2]), "after": json.loads(row[3]), "actor_username": row[4], "created_at": row[5]} for row in rows]

    def quarantine(self) -> list[dict[str, str]]:
        return [{"customer_id": row[0], "reason": row[1]} for row in self._local_store().connection.execute("SELECT customer_id,reason FROM customer_quarantine WHERE active=1 ORDER BY id")]
