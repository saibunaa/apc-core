"""P3a Core-owned no-money invoice aggregate and lifecycle persistence.

This module is local persistence only. It has no server wiring, legacy/MDB/NAS
access, pricing, numbering, tax, currency, AWB, printing, or accounting paths.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3


_FRESHNESS_RULE = "core_imported_at_strictly_later"


class CoreInvoiceError(ValueError):
    """A Core-owned invoice lifecycle command is invalid or conflicts."""


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise CoreInvoiceError(f"{label} is required")
    return value


def _version(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise CoreInvoiceError("expected version is invalid")
    return value


def _strictly_later_snapshot_evidence(comparison_rule: object, source_imported_at: object, imported_snapshot_imported_at: object) -> tuple[str, str]:
    if comparison_rule != _FRESHNESS_RULE:
        raise CoreInvoiceError("evidence freshness is unknown")
    if type(source_imported_at) is not str or type(imported_snapshot_imported_at) is not str:
        raise CoreInvoiceError("evidence freshness is unknown")
    try:
        source_time = datetime.fromisoformat(source_imported_at.replace("Z", "+00:00"))
        imported_time = datetime.fromisoformat(imported_snapshot_imported_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise CoreInvoiceError("evidence freshness is unknown") from error
    if source_time.tzinfo is None or imported_time.tzinfo is None or imported_time <= source_time:
        raise CoreInvoiceError("evidence freshness is unknown")
    return source_imported_at, imported_snapshot_imported_at


class CoreInvoiceStore:
    """Explicitly migrated P3a storage with immutable order-line membership and audit events."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        if not self.database_path.is_file():
            raise CoreInvoiceError("Core invoice database is missing")
        try:
            self.connection = sqlite3.connect(f"{self.database_path.resolve().as_uri()}?mode=rw", uri=True)
        except sqlite3.Error as error:
            raise CoreInvoiceError("Core invoice database cannot be opened") from error
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            versions = {row[0] for row in self.connection.execute("SELECT version FROM core_schema_migrations")}
        except sqlite3.Error as error:
            self.connection.close()
            raise CoreInvoiceError("Core invoice migrations have not been applied") from error
        if 3 not in versions:
            self.connection.close()
            raise CoreInvoiceError("Core invoice migrations have not been applied")

    def close(self) -> None:
        self.connection.close()

    def _advance(self, invoice_id: str, *, status: str | None = None) -> None:
        if status is None:
            self.connection.execute("UPDATE core_invoices SET version=version+1 WHERE invoice_id=?", (invoice_id,))
        else:
            self.connection.execute("UPDATE core_invoices SET status=?,version=version+1 WHERE invoice_id=?", (status, invoice_id))

    @staticmethod
    def _same(row: sqlite3.Row, expected: dict[str, object]) -> bool:
        return all(row[key] == value for key, value in expected.items())

    def _event_replay(self, key: str, expected: dict[str, object]) -> sqlite3.Row | None:
        row = self.connection.execute("SELECT * FROM core_invoice_events WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if not self._same(row, expected):
            raise CoreInvoiceError("idempotency key conflicts with a different command")
        return row

    def _invoice(self, invoice_id: str, expected_version: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if row is None:
            raise CoreInvoiceError("invoice is unknown")
        if row["version"] != expected_version:
            raise CoreInvoiceError("expected version conflicts")
        return row

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, object]:
        return {"invoice_id": row["invoice_id"], "version": row["version"], "status": row["status"]}

    def invoice_lines(self, invoice_id: object) -> list[dict[str, object]]:
        invoice_id = _text(invoice_id, "invoice id")
        rows = self.connection.execute(
            "SELECT invoice_line_id,order_line_id FROM core_invoice_lines WHERE invoice_id=? ORDER BY order_line_id",
            (invoice_id,),
        )
        return [dict(row) for row in rows]

    def create_invoice(self, invoice_id: object, actor: object, idempotency_key: object, order_line_ids: object, *, expected_version: object) -> dict[str, object]:
        invoice_id = _text(invoice_id, "invoice id")
        actor = _text(actor, "actor")
        idempotency_key = _text(idempotency_key, "idempotency key")
        expected_version = _version(expected_version)
        if expected_version != 0:
            raise CoreInvoiceError("expected version conflicts")
        if type(order_line_ids) is not list or not order_line_ids:
            raise CoreInvoiceError("at least one explicit order-line membership is required")
        line_ids = [_text(item, "order line id") for item in order_line_ids]
        if len(set(line_ids)) != len(line_ids):
            raise CoreInvoiceError("duplicate explicit order-line membership")
        with self.connection:
            existing = self.connection.execute("SELECT * FROM core_invoices WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                if not self._same(existing, {"invoice_id": invoice_id, "created_by": actor, "version": 1, "status": "draft"}):
                    raise CoreInvoiceError("idempotency key conflicts with a different command")
                if [row["order_line_id"] for row in self.invoice_lines(invoice_id)] != sorted(line_ids):
                    raise CoreInvoiceError("idempotency key conflicts with a different command")
                return self._receipt(existing)
            if self.connection.execute("SELECT 1 FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone() is not None:
                raise CoreInvoiceError("invoice identity conflicts with an existing invoice")
            known = {
                row[0]
                for row in self.connection.execute(
                    "SELECT line_id FROM core_order_lines WHERE line_id IN (" + ",".join("?" for _ in line_ids) + ")",
                    line_ids,
                )
            }
            if known != set(line_ids):
                raise CoreInvoiceError("selected order line is unknown")
            selected = self.connection.execute(
                "SELECT order_line_id FROM core_invoice_lines WHERE order_line_id IN (" + ",".join("?" for _ in line_ids) + ")",
                line_ids,
            ).fetchone()
            if selected is not None:
                raise CoreInvoiceError("selected order line is already a member of another invoice")
            self.connection.executemany(
                "INSERT INTO core_invoice_lines(invoice_line_id,invoice_id,order_line_id) VALUES (?,?,?)",
                [(f"{invoice_id}:line:{index}", invoice_id, line_id) for index, line_id in enumerate(line_ids, start=1)],
            )
            self.connection.execute(
                "INSERT INTO core_invoice_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?)",
                (f"{invoice_id}:creation", invoice_id, "creation", actor, expected_version, idempotency_key),
            )
            self.connection.execute(
                "INSERT INTO core_invoices(invoice_id,created_by,status,version,idempotency_key) VALUES (?,?,?,1,?)",
                (invoice_id, actor, "draft", idempotency_key),
            )
            return {"invoice_id": invoice_id, "version": 1, "status": "draft"}

    def _transition(self, event_id: object, invoice_id: object, actor: object, idempotency_key: object, *, expected_version: object, event_kind: str, from_status: str, to_status: str) -> dict[str, object]:
        event_id = _text(event_id, "event id")
        invoice_id = _text(invoice_id, "invoice id")
        actor = _text(actor, "actor")
        idempotency_key = _text(idempotency_key, "idempotency key")
        expected_version = _version(expected_version)
        expected = {"event_id": event_id, "invoice_id": invoice_id, "event_kind": event_kind, "actor": actor, "expected_version": expected_version}
        with self.connection:
            replay = self._event_replay(idempotency_key, expected)
            if replay is not None:
                row = self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
                return self._receipt(row)
            invoice = self._invoice(invoice_id, expected_version)
            if invoice["status"] != from_status:
                raise CoreInvoiceError("invoice status does not allow this lifecycle action")
            self.connection.execute(
                "INSERT INTO core_invoice_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?)",
                (event_id, invoice_id, event_kind, actor, expected_version, idempotency_key),
            )
            self._advance(invoice_id, status=to_status)
            return self._receipt(self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone())

    def submit_for_review(self, event_id: object, invoice_id: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        return self._transition(event_id, invoice_id, actor, idempotency_key, expected_version=expected_version, event_kind="review_submission", from_status="draft", to_status="review")

    def approve(self, event_id: object, invoice_id: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        return self._transition(event_id, invoice_id, actor, idempotency_key, expected_version=expected_version, event_kind="approval", from_status="review", to_status="approved")

    def cancel(self, event_id: object, invoice_id: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        event_id = _text(event_id, "event id")
        invoice_id = _text(invoice_id, "invoice id")
        actor = _text(actor, "actor")
        idempotency_key = _text(idempotency_key, "idempotency key")
        expected_version = _version(expected_version)
        expected = {"event_id": event_id, "invoice_id": invoice_id, "event_kind": "cancellation", "actor": actor, "expected_version": expected_version}
        with self.connection:
            replay = self._event_replay(idempotency_key, expected)
            if replay is not None:
                return self._receipt(self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone())
            invoice = self._invoice(invoice_id, expected_version)
            if invoice["status"] == "cancelled":
                raise CoreInvoiceError("invoice status does not allow this lifecycle action")
            self.connection.execute(
                "INSERT INTO core_invoice_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?)",
                (event_id, invoice_id, "cancellation", actor, expected_version, idempotency_key),
            )
            self._advance(invoice_id, status="cancelled")
            return self._receipt(self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone())

    def record_evidence_conflict(self, conflict_id: object, invoice_id: object, source_snapshot_sha256: object, imported_snapshot_sha256: object, actor: object, idempotency_key: object, *, expected_version: object, comparison_rule: object = None) -> dict[str, object]:
        conflict_id = _text(conflict_id, "conflict id")
        invoice_id = _text(invoice_id, "invoice id")
        source_snapshot_sha256 = _text(source_snapshot_sha256, "source snapshot identity")
        imported_snapshot_sha256 = _text(imported_snapshot_sha256, "imported snapshot identity")
        actor = _text(actor, "actor")
        idempotency_key = _text(idempotency_key, "idempotency key")
        expected_version = _version(expected_version)
        if source_snapshot_sha256 == imported_snapshot_sha256:
            raise CoreInvoiceError("imported snapshot identity must be distinct")
        expected = {"event_id": f"{conflict_id}:event", "invoice_id": invoice_id, "conflict_id": conflict_id, "event_kind": "evidence_conflict", "actor": actor, "expected_version": expected_version}
        with self.connection:
            replay = self._event_replay(idempotency_key, expected)
            if replay is not None:
                conflict = self.connection.execute(
                    "SELECT source_snapshot_sha256,imported_snapshot_sha256,comparison_rule FROM core_invoice_conflicts WHERE conflict_id=?",
                    (conflict_id,),
                ).fetchone()
                if conflict is None or tuple(conflict) != (source_snapshot_sha256, imported_snapshot_sha256, comparison_rule):
                    raise CoreInvoiceError("idempotency key conflicts with a different command")
                row = self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
                return {**self._receipt(row), "conflict_id": conflict_id}
            self._invoice(invoice_id, expected_version)
            belongs = self.connection.execute(
                "SELECT 1 FROM core_invoice_lines i JOIN core_order_lines o ON o.line_id=i.order_line_id "
                "WHERE i.invoice_id=? AND o.snapshot_sha256=?",
                (invoice_id, source_snapshot_sha256),
            ).fetchone()
            if belongs is None:
                raise CoreInvoiceError("source snapshot is not explicit invoice membership")
            snapshots = {
                row["snapshot_sha256"]: row["imported_at"]
                for row in self.connection.execute(
                    "SELECT snapshot_sha256,imported_at FROM core_source_snapshots WHERE snapshot_sha256 IN (?,?)",
                    (source_snapshot_sha256, imported_snapshot_sha256),
                )
            }
            if imported_snapshot_sha256 not in snapshots:
                raise CoreInvoiceError("imported snapshot identity is unknown")
            source_imported_at, imported_snapshot_imported_at = _strictly_later_snapshot_evidence(
                comparison_rule, snapshots.get(source_snapshot_sha256), snapshots[imported_snapshot_sha256]
            )
            self.connection.execute(
                "INSERT INTO core_invoice_conflicts("
                "conflict_id,invoice_id,source_snapshot_sha256,imported_snapshot_sha256,comparison_rule,source_imported_at,imported_snapshot_imported_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    conflict_id, invoice_id, source_snapshot_sha256, imported_snapshot_sha256, comparison_rule,
                    source_imported_at, imported_snapshot_imported_at,
                ),
            )
            self.connection.execute(
                "INSERT INTO core_invoice_events(event_id,invoice_id,conflict_id,event_kind,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?,?)",
                (f"{conflict_id}:event", invoice_id, conflict_id, "evidence_conflict", actor, expected_version, idempotency_key),
            )
            self._advance(invoice_id)
            row = self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
            return {**self._receipt(row), "conflict_id": conflict_id}

    def resolve_conflict(self, event_id: object, conflict_id: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        event_id = _text(event_id, "event id")
        conflict_id = _text(conflict_id, "conflict id")
        actor = _text(actor, "actor")
        idempotency_key = _text(idempotency_key, "idempotency key")
        expected_version = _version(expected_version)
        with self.connection:
            conflict = self.connection.execute("SELECT * FROM core_invoice_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
            if conflict is None:
                raise CoreInvoiceError("conflict is unknown")
            invoice_id = conflict["invoice_id"]
            expected = {"event_id": event_id, "invoice_id": invoice_id, "conflict_id": conflict_id, "event_kind": "conflict_resolution", "actor": actor, "expected_version": expected_version}
            replay = self._event_replay(idempotency_key, expected)
            if replay is not None:
                row = self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
                return {**self._receipt(row), "conflict_id": conflict_id}
            self._invoice(invoice_id, expected_version)
            if self.connection.execute(
                "SELECT 1 FROM core_invoice_events WHERE conflict_id=? AND event_kind='conflict_resolution'", (conflict_id,)
            ).fetchone() is not None:
                raise CoreInvoiceError("conflict is already resolved")
            self.connection.execute(
                "INSERT INTO core_invoice_events(event_id,invoice_id,conflict_id,event_kind,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?,?)",
                (event_id, invoice_id, conflict_id, "conflict_resolution", actor, expected_version, idempotency_key),
            )
            self._advance(invoice_id)
            row = self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
            return {**self._receipt(row), "conflict_id": conflict_id}
