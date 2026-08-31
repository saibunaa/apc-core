"""Core-owned immutable provenance for accepted legacy snapshot rows.

This module is deliberately not wired into server startup. Migrations are an
explicit operator/deployment action; importing reads a stable private copy of a
pinned SQLite artifact and writes exclusively to the caller-selected Core DB.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


ORDER_ITEM_TABLE = "MainDB__ORDER_ITEM"
ORDER_ITEM_REQUIRED_COLUMNS = {"Order No", "Line No", "Item ID", "Qty", "Description TH", "SubCust"}
_SCHEMA_VERSION = 2


class CoreProvenanceError(ValueError):
    """A Core provenance migration or immutable source import is unsafe."""


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    snapshot_sha256: str
    source_table: str
    row_count: int
    replayed: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _migration_001(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE core_source_snapshots ("
        "snapshot_sha256 TEXT PRIMARY KEY NOT NULL "
        "CHECK(length(snapshot_sha256)=64 AND snapshot_sha256 NOT GLOB '*[^0123456789abcdef]*'), "
        "artifact_path TEXT NOT NULL, imported_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE core_source_rows ("
        "snapshot_sha256 TEXT NOT NULL REFERENCES core_source_snapshots(snapshot_sha256), "
        "source_table TEXT NOT NULL, source_rowid INTEGER NOT NULL, "
        "source_kind TEXT NOT NULL CHECK(source_kind='order_item'), "
        "document_id TEXT, line_label TEXT, item_id TEXT, quantity TEXT, "
        "evidence_json TEXT NOT NULL, "
        "PRIMARY KEY(snapshot_sha256, source_table, source_rowid))"
    )
    connection.execute(
        "CREATE TRIGGER core_source_snapshots_no_update BEFORE UPDATE ON core_source_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'source snapshots are immutable'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_source_snapshots_no_delete BEFORE DELETE ON core_source_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'source snapshots are immutable'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_source_rows_no_update BEFORE UPDATE ON core_source_rows "
        "BEGIN SELECT RAISE(ABORT, 'source rows are immutable'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_source_rows_no_delete BEFORE DELETE ON core_source_rows "
        "BEGIN SELECT RAISE(ABORT, 'source rows are immutable'); END"
    )


def _migration_002(connection: sqlite3.Connection) -> None:
    """P2 Core-owned orders and packing state; never part of runtime startup."""
    connection.execute(
        "CREATE TABLE core_orders ("
        "order_id TEXT PRIMARY KEY NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0), idempotency_key TEXT NOT NULL UNIQUE)"
    )
    connection.execute(
        "CREATE TABLE core_order_lines ("
        "line_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL REFERENCES core_orders(order_id), "
        "snapshot_sha256 TEXT NOT NULL, source_table TEXT NOT NULL, source_rowid INTEGER NOT NULL, "
        "original_quantity TEXT NOT NULL CHECK(CAST(original_quantity AS REAL) > 0), "
        "FOREIGN KEY(snapshot_sha256,source_table,source_rowid) "
        "REFERENCES core_source_rows(snapshot_sha256,source_table,source_rowid), "
        "UNIQUE(order_id,snapshot_sha256,source_table,source_rowid))"
    )
    connection.execute(
        "CREATE TABLE core_packing_plans ("
        "plan_id TEXT PRIMARY KEY NOT NULL, order_id TEXT NOT NULL REFERENCES core_orders(order_id), "
        "created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0), idempotency_key TEXT NOT NULL UNIQUE)"
    )
    connection.execute(
        "CREATE TABLE core_packing_boxes ("
        "box_id TEXT PRIMARY KEY NOT NULL, plan_id TEXT NOT NULL REFERENCES core_packing_plans(plan_id), "
        "box_number INTEGER NOT NULL CHECK(box_number > 0), created_by TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expected_version INTEGER NOT NULL CHECK(expected_version >= 0), "
        "idempotency_key TEXT NOT NULL UNIQUE, UNIQUE(plan_id,box_number))"
    )
    connection.execute(
        "CREATE TABLE core_packing_events ("
        "event_id TEXT PRIMARY KEY NOT NULL, plan_id TEXT NOT NULL REFERENCES core_packing_plans(plan_id), "
        "line_id TEXT NOT NULL REFERENCES core_order_lines(line_id), box_id TEXT REFERENCES core_packing_boxes(box_id), "
        "event_kind TEXT NOT NULL CHECK(event_kind IN ('allocation','unavailable','reversal')), "
        "quantity TEXT NOT NULL CHECK(CAST(quantity AS REAL) > 0), reverses_event_id TEXT UNIQUE REFERENCES core_packing_events(event_id), "
        "reason TEXT, actor TEXT NOT NULL, expected_version INTEGER NOT NULL CHECK(expected_version >= 0), "
        "idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "CHECK((event_kind='allocation' AND box_id IS NOT NULL AND reverses_event_id IS NULL AND reason IS NULL) OR "
        "(event_kind='unavailable' AND box_id IS NULL AND reverses_event_id IS NULL AND reason IS NULL) OR "
        "(event_kind='reversal' AND box_id IS NULL AND reverses_event_id IS NOT NULL AND reason IS NOT NULL)))"
    )
    for table in ("core_order_lines", "core_packing_boxes", "core_packing_events"):
        connection.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END"
        )
        connection.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )
    connection.execute(
        "CREATE TRIGGER core_orders_no_delete BEFORE DELETE ON core_orders "
        "BEGIN SELECT RAISE(ABORT, 'core orders are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_orders_identity_immutable BEFORE UPDATE OF order_id,created_by,created_at,idempotency_key ON core_orders "
        "BEGIN SELECT RAISE(ABORT, 'core order identity is immutable'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_packing_plans_no_delete BEFORE DELETE ON core_packing_plans "
        "BEGIN SELECT RAISE(ABORT, 'packing plans are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_packing_plans_identity_immutable BEFORE UPDATE OF plan_id,order_id,created_by,created_at,idempotency_key ON core_packing_plans "
        "BEGIN SELECT RAISE(ABORT, 'packing plan identity is immutable'); END"
    )


def apply_core_provenance_migrations(database_path: Path) -> int:
    """Apply the explicit, versioned P1 foundation migration to a Core DB."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS core_schema_migrations ("
            "version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {row[0] for row in connection.execute("SELECT version FROM core_schema_migrations")}
        if 1 not in applied:
            _migration_001(connection)
            connection.execute("INSERT INTO core_schema_migrations(version) VALUES (?)", (1,))
        if 2 not in applied:
            _migration_002(connection)
            connection.execute("INSERT INTO core_schema_migrations(version) VALUES (?)", (2,))
    return _SCHEMA_VERSION


class CoreProvenanceStore:
    """Explicitly migrated Core persistence; never writes the source artifact."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        if not self.database_path.is_file():
            raise CoreProvenanceError("Core provenance database is missing")
        try:
            self.connection = sqlite3.connect(f"{self.database_path.resolve().as_uri()}?mode=rw", uri=True)
        except sqlite3.Error as error:
            raise CoreProvenanceError("Core provenance database cannot be opened") from error
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.connection.close()

    def _require_migration(self) -> None:
        try:
            versions = {row[0] for row in self.connection.execute("SELECT version FROM core_schema_migrations")}
        except sqlite3.Error as error:
            raise CoreProvenanceError("Core provenance migrations have not been applied") from error
        if _SCHEMA_VERSION not in versions:
            raise CoreProvenanceError("Core provenance migrations have not been applied")

    @staticmethod
    def _reject_nonempty_wal(snapshot_path: Path) -> None:
        wal_path = snapshot_path.with_name(snapshot_path.name + "-wal")
        try:
            if wal_path.exists() and wal_path.stat().st_size > 0:
                raise CoreProvenanceError("accepted snapshot has a non-empty WAL")
        except OSError as error:
            raise CoreProvenanceError("accepted snapshot WAL cannot be inspected") from error

    def _copy_stable_source(self, snapshot_path: Path, snapshot_sha256: str) -> Path:
        if not _valid_sha256(snapshot_sha256):
            raise CoreProvenanceError("snapshot hash is invalid")
        self._reject_nonempty_wal(snapshot_path)
        try:
            descriptor = os.open(snapshot_path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise CoreProvenanceError("accepted snapshot is missing") from error
        temporary: Path | None = None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CoreProvenanceError("accepted snapshot is not a regular file")
            if _sha256_descriptor(descriptor) != snapshot_sha256:
                raise CoreProvenanceError("accepted snapshot hash does not match")
            os.lseek(descriptor, 0, os.SEEK_SET)
            target_descriptor, target_name = tempfile.mkstemp(prefix=".core-provenance-source-", suffix=".sqlite", dir=self.database_path.parent)
            temporary = Path(target_name)
            try:
                with os.fdopen(target_descriptor, "wb") as target, os.fdopen(os.dup(descriptor), "rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            if _sha256(temporary) != snapshot_sha256:
                temporary.unlink(missing_ok=True)
                raise CoreProvenanceError("stable source copy hash does not match")
            temporary.chmod(0o400)
            self._reject_nonempty_wal(snapshot_path)
            return temporary
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise CoreProvenanceError("accepted snapshot cannot be copied safely") from error
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_validated_stable_snapshot(stable_path: Path) -> sqlite3.Connection:
        try:
            source = sqlite3.connect(_readonly_uri(stable_path), uri=True)
            source.row_factory = sqlite3.Row
            integrity = source.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise CoreProvenanceError("accepted snapshot integrity check failed")
            columns = {row[1] for row in source.execute(f'PRAGMA table_info("{ORDER_ITEM_TABLE}")')}
            if not ORDER_ITEM_REQUIRED_COLUMNS.issubset(columns):
                raise CoreProvenanceError("accepted snapshot order-item schema is incomplete")
            return source
        except sqlite3.Error as error:
            raise CoreProvenanceError("accepted snapshot cannot be read") from error

    def import_order_item_snapshot(self, snapshot_path: Path, *, snapshot_sha256: str, imported_at: str) -> ImportReceipt:
        """Persist immutable source evidence keyed by snapshot SHA, table, and rowid."""
        snapshot_path = Path(snapshot_path)
        if type(imported_at) is not str or not imported_at:
            raise CoreProvenanceError("import timestamp is required")
        self._require_migration()
        stable_path = self._copy_stable_source(snapshot_path, snapshot_sha256)
        try:
            source = self._open_validated_stable_snapshot(stable_path)
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                existing = self.connection.execute(
                    "SELECT COUNT(*) FROM core_source_rows WHERE snapshot_sha256=? AND source_table=?",
                    (snapshot_sha256, ORDER_ITEM_TABLE),
                ).fetchone()[0]
                if existing:
                    self.connection.commit()
                    return ImportReceipt(snapshot_sha256, ORDER_ITEM_TABLE, existing, True)
                self.connection.execute(
                    "INSERT INTO core_source_snapshots(snapshot_sha256, artifact_path, imported_at) VALUES (?, ?, ?)",
                    (snapshot_sha256, str(snapshot_path.resolve()), imported_at),
                )
                count = 0
                for row in source.execute(
                    'SELECT rowid, "Order No", "Line No", "Item ID", "Qty", "Description TH", "SubCust" '
                    f'FROM "{ORDER_ITEM_TABLE}" ORDER BY rowid'
                ):
                    evidence = json.dumps(
                        {"description_th": row[5], "subcust": row[6]}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    self.connection.execute(
                        "INSERT INTO core_source_rows("
                        "snapshot_sha256, source_table, source_rowid, source_kind, document_id, line_label, item_id, quantity, evidence_json"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (snapshot_sha256, ORDER_ITEM_TABLE, row[0], "order_item", row[1], row[2], row[3], row[4], evidence),
                    )
                    count += 1
                if count < 1:
                    raise CoreProvenanceError("accepted snapshot order-item table is empty")
                self.connection.commit()
                return ImportReceipt(snapshot_sha256, ORDER_ITEM_TABLE, count, False)
            except Exception:
                self.connection.rollback()
                raise
            finally:
                source.close()
        finally:
            stable_path.unlink(missing_ok=True)

    def source_rows(self, snapshot_sha256: str) -> list[dict[str, object]]:
        self._require_migration()
        rows = self.connection.execute(
            "SELECT snapshot_sha256, source_table, source_rowid, source_kind, document_id, line_label, item_id, quantity, evidence_json "
            "FROM core_source_rows WHERE snapshot_sha256=? ORDER BY source_table, source_rowid",
            (snapshot_sha256,),
        )
        return [dict(row) for row in rows]
