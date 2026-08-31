"""P2 Core-owned Order and packing/allocation foundation.

This module is local persistence only.  It has no server wiring, source import,
legacy/MDB access, pricing, invoice, print, or delivery capability.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3


class CoreOrderError(ValueError):
    """A Core-owned order or packing command is invalid or conflicts."""


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise CoreOrderError(f"{label} is required")
    return value


def _quantity(value: object) -> Decimal:
    if type(value) is not str or not value.strip():
        raise CoreOrderError("quantity must be a positive exact decimal")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise CoreOrderError("quantity must be a positive exact decimal") from error
    if not parsed.is_finite() or parsed <= 0:
        raise CoreOrderError("quantity must be a positive exact decimal")
    return parsed


def _format(value: Decimal) -> str:
    normalized = value.normalize()
    rendered = format(normalized, "f")
    return "0" if rendered in {"-0", ""} else rendered


class CoreOrderStore:
    """Explicitly migrated P2 storage with immutable evidence edges and events."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        if not self.database_path.is_file():
            raise CoreOrderError("Core order database is missing")
        try:
            self.connection = sqlite3.connect(f"{self.database_path.resolve().as_uri()}?mode=rw", uri=True)
        except sqlite3.Error as error:
            raise CoreOrderError("Core order database cannot be opened") from error
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            versions = {row[0] for row in self.connection.execute("SELECT version FROM core_schema_migrations")}
        except sqlite3.Error as error:
            self.connection.close()
            raise CoreOrderError("Core order migrations have not been applied") from error
        if 2 not in versions:
            self.connection.close()
            raise CoreOrderError("Core order migrations have not been applied")

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _same(row: sqlite3.Row, expected: dict[str, object]) -> bool:
        return all(row[key] == value for key, value in expected.items())

    def _idempotent(self, table: str, key: str, expected: dict[str, object]) -> dict[str, object] | None:
        row = self.connection.execute(f"SELECT * FROM {table} WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if not self._same(row, expected):
            raise CoreOrderError("idempotency key conflicts with a different command")
        return dict(row)

    @staticmethod
    def _require_expected_version(value: object) -> int:
        if type(value) is not int or isinstance(value, bool) or value < 0:
            raise CoreOrderError("expected version is invalid")
        return value

    def _plan(self, plan_id: str, expected_version: object) -> sqlite3.Row:
        expected_version = self._require_expected_version(expected_version)
        row = self.connection.execute("SELECT * FROM core_packing_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise CoreOrderError("packing plan is unknown")
        if row["version"] != expected_version:
            raise CoreOrderError("expected version conflicts")
        return row

    def create_order(self, order_id: object, actor: object, idempotency_key: object, lines: object) -> dict[str, object]:
        order_id, actor, idempotency_key = _text(order_id, "order id"), _text(actor, "actor"), _text(idempotency_key, "idempotency key")
        if type(lines) is not list or not lines:
            raise CoreOrderError("at least one explicit source line is required")
        normalized: list[tuple[str, str, str, int, str]] = []
        seen_lines: set[str] = set()
        seen_sources: set[tuple[str, str, int]] = set()
        for line in lines:
            if type(line) is not dict:
                raise CoreOrderError("source line is invalid")
            line_id = _text(line.get("line_id"), "line id")
            snapshot = _text(line.get("snapshot_sha256"), "snapshot hash")
            table = _text(line.get("source_table"), "source table")
            rowid = line.get("source_rowid")
            if type(rowid) is not int or isinstance(rowid, bool):
                raise CoreOrderError("source rowid is invalid")
            if line_id in seen_lines or (snapshot, table, rowid) in seen_sources:
                raise CoreOrderError("duplicate explicit source membership")
            source = self.connection.execute(
                "SELECT quantity FROM core_source_rows WHERE snapshot_sha256=? AND source_table=? AND source_rowid=?",
                (snapshot, table, rowid),
            ).fetchone()
            if source is None:
                raise CoreOrderError("source coordinate is unknown")
            normalized.append((line_id, snapshot, table, rowid, _format(_quantity(source[0]))))
            seen_lines.add(line_id)
            seen_sources.add((snapshot, table, rowid))
        with self.connection:
            existing = self._idempotent("core_orders", idempotency_key, {"order_id": order_id, "created_by": actor})
            if existing is not None:
                existing_lines = self.order_lines(order_id)
                expected_lines = [{"line_id": item[0], "snapshot_sha256": item[1], "source_table": item[2], "source_rowid": item[3], "original_quantity": item[4]} for item in normalized]
                if existing_lines != expected_lines:
                    raise CoreOrderError("idempotency key conflicts with a different command")
                return {"order_id": order_id, "version": existing["version"]}
            self.connection.execute("INSERT INTO core_orders(order_id,created_by,idempotency_key) VALUES (?,?,?)", (order_id, actor, idempotency_key))
            self.connection.executemany(
                "INSERT INTO core_order_lines(line_id,order_id,snapshot_sha256,source_table,source_rowid,original_quantity) VALUES (?,?,?,?,?,?)",
                [(line_id, order_id, snapshot, table, rowid, quantity) for line_id, snapshot, table, rowid, quantity in normalized],
            )
        return {"order_id": order_id, "version": 0}

    def order_lines(self, order_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT line_id,snapshot_sha256,source_table,source_rowid,original_quantity FROM core_order_lines WHERE order_id=? ORDER BY line_id",
            (order_id,),
        )
        return [dict(row) for row in rows]

    def create_packing_plan(self, plan_id: object, order_id: object, actor: object, idempotency_key: object) -> dict[str, object]:
        plan_id, order_id, actor, idempotency_key = _text(plan_id, "plan id"), _text(order_id, "order id"), _text(actor, "actor"), _text(idempotency_key, "idempotency key")
        with self.connection:
            existing = self._idempotent("core_packing_plans", idempotency_key, {"plan_id": plan_id, "order_id": order_id, "created_by": actor})
            if existing is not None:
                return {"plan_id": plan_id, "version": existing["version"]}
            if self.connection.execute("SELECT 1 FROM core_orders WHERE order_id=?", (order_id,)).fetchone() is None:
                raise CoreOrderError("Core order is unknown")
            self.connection.execute("INSERT INTO core_packing_plans(plan_id,order_id,created_by,idempotency_key) VALUES (?,?,?,?)", (plan_id, order_id, actor, idempotency_key))
        return {"plan_id": plan_id, "version": 0}

    def create_box(self, box_id: object, plan_id: object, box_number: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        box_id, plan_id, actor, idempotency_key = _text(box_id, "box id"), _text(plan_id, "plan id"), _text(actor, "actor"), _text(idempotency_key, "idempotency key")
        if type(box_number) is not int or isinstance(box_number, bool) or box_number <= 0:
            raise CoreOrderError("box number is invalid")
        expected_version = self._require_expected_version(expected_version)
        with self.connection:
            existing = self._idempotent("core_packing_boxes", idempotency_key, {"box_id": box_id, "plan_id": plan_id, "box_number": box_number, "created_by": actor, "expected_version": expected_version})
            if existing is not None:
                plan = self.connection.execute("SELECT version FROM core_packing_plans WHERE plan_id=?", (plan_id,)).fetchone()
                return {"box_id": box_id, "version": plan[0]}
            self._plan(plan_id, expected_version)
            self.connection.execute("INSERT INTO core_packing_boxes(box_id,plan_id,box_number,created_by,expected_version,idempotency_key) VALUES (?,?,?,?,?,?)", (box_id, plan_id, box_number, actor, expected_version, idempotency_key))
            self.connection.execute("UPDATE core_packing_plans SET version=version+1 WHERE plan_id=?", (plan_id,))
            version = self.connection.execute("SELECT version FROM core_packing_plans WHERE plan_id=?", (plan_id,)).fetchone()[0]
        return {"box_id": box_id, "version": version}

    def _remaining(self, plan_id: str, line_id: str) -> Decimal:
        line = self.connection.execute(
            "SELECT l.original_quantity FROM core_order_lines l JOIN core_packing_plans p ON p.order_id=l.order_id WHERE p.plan_id=? AND l.line_id=?",
            (plan_id, line_id),
        ).fetchone()
        if line is None:
            raise CoreOrderError("line is not a member of packing plan")
        original = _quantity(line[0])
        rows = self.connection.execute(
            "SELECT event_id,event_kind,quantity FROM core_packing_events WHERE plan_id=? AND line_id=? AND event_kind IN ('allocation','unavailable') "
            "AND event_id NOT IN (SELECT reverses_event_id FROM core_packing_events WHERE reverses_event_id IS NOT NULL)",
            (plan_id, line_id),
        )
        used = sum((_quantity(row["quantity"]) for row in rows), Decimal("0"))
        return original - used

    def _event(self, event_id: object, plan_id: object, line_id: object, box_id: object, quantity: object, actor: object, idempotency_key: object, *, expected_version: object, event_kind: str, reason: str | None = None, reverses_event_id: str | None = None) -> dict[str, object]:
        event_id, plan_id, line_id, actor, idempotency_key = _text(event_id, "event id"), _text(plan_id, "plan id"), _text(line_id, "line id"), _text(actor, "actor"), _text(idempotency_key, "idempotency key")
        value = _quantity(quantity)
        box_value = _text(box_id, "box id") if event_kind == "allocation" else None
        expected_version = self._require_expected_version(expected_version)
        expected = {"event_id": event_id, "plan_id": plan_id, "line_id": line_id, "box_id": box_value, "event_kind": event_kind, "quantity": _format(value), "actor": actor, "reverses_event_id": reverses_event_id, "reason": reason, "expected_version": expected_version}
        with self.connection:
            existing = self._idempotent("core_packing_events", idempotency_key, expected)
            if existing is not None:
                plan = self.connection.execute("SELECT version FROM core_packing_plans WHERE plan_id=?", (plan_id,)).fetchone()
                return {"event_id": event_id, "version": plan[0]}
            self._plan(plan_id, expected_version)
            if event_kind == "allocation":
                box = self.connection.execute("SELECT 1 FROM core_packing_boxes WHERE box_id=? AND plan_id=?", (box_value, plan_id)).fetchone()
                if box is None:
                    raise CoreOrderError("box is not a member of packing plan")
                duplicate = self.connection.execute("SELECT 1 FROM core_packing_events WHERE plan_id=? AND line_id=? AND box_id=? AND event_kind='allocation' AND event_id NOT IN (SELECT reverses_event_id FROM core_packing_events WHERE reverses_event_id IS NOT NULL)", (plan_id, line_id, box_value)).fetchone()
                if duplicate is not None:
                    raise CoreOrderError("duplicate semantic allocation requires a reversal")
            if event_kind in {"allocation", "unavailable"} and value > self._remaining(plan_id, line_id):
                raise CoreOrderError("quantity exceeds remaining available")
            self.connection.execute(
                "INSERT INTO core_packing_events(event_id,plan_id,line_id,box_id,event_kind,quantity,reverses_event_id,reason,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, plan_id, line_id, box_value, event_kind, _format(value), reverses_event_id, reason, actor, expected_version, idempotency_key),
            )
            self.connection.execute("UPDATE core_packing_plans SET version=version+1 WHERE plan_id=?", (plan_id,))
            version = self.connection.execute("SELECT version FROM core_packing_plans WHERE plan_id=?", (plan_id,)).fetchone()[0]
        return {"event_id": event_id, "version": version}

    def allocate(self, event_id: object, plan_id: object, line_id: object, box_id: object, quantity: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        return self._event(event_id, plan_id, line_id, box_id, quantity, actor, idempotency_key, expected_version=expected_version, event_kind="allocation")

    def mark_unavailable(self, event_id: object, plan_id: object, line_id: object, quantity: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        return self._event(event_id, plan_id, line_id, None, quantity, actor, idempotency_key, expected_version=expected_version, event_kind="unavailable")

    def reverse_event(self, event_id: object, plan_id: object, reverses_event_id: object, reason: object, actor: object, idempotency_key: object, *, expected_version: object) -> dict[str, object]:
        reverses_event_id, reason = _text(reverses_event_id, "reversed event id"), _text(reason, "reason")
        target = self.connection.execute("SELECT * FROM core_packing_events WHERE event_id=? AND plan_id=?", (reverses_event_id, plan_id)).fetchone()
        if target is None or target["event_kind"] not in {"allocation", "unavailable"}:
            raise CoreOrderError("event cannot be reversed")
        return self._event(event_id, plan_id, target["line_id"], None, target["quantity"], actor, idempotency_key, expected_version=expected_version, event_kind="reversal", reason=reason, reverses_event_id=reverses_event_id)

    def reconciliation(self, plan_id: object, line_id: object) -> dict[str, str]:
        plan_id, line_id = _text(plan_id, "plan id"), _text(line_id, "line id")
        line = self.connection.execute(
            "SELECT l.original_quantity FROM core_order_lines l JOIN core_packing_plans p ON p.order_id=l.order_id WHERE p.plan_id=? AND l.line_id=?",
            (plan_id, line_id),
        ).fetchone()
        if line is None:
            raise CoreOrderError("line is not a member of packing plan")
        active = self.connection.execute(
            "SELECT event_kind,quantity FROM core_packing_events WHERE plan_id=? AND line_id=? AND event_kind IN ('allocation','unavailable') "
            "AND event_id NOT IN (SELECT reverses_event_id FROM core_packing_events WHERE reverses_event_id IS NOT NULL)",
            (plan_id, line_id),
        )
        allocated = Decimal("0")
        unavailable = Decimal("0")
        for event in active:
            if event["event_kind"] == "allocation":
                allocated += _quantity(event["quantity"])
            else:
                unavailable += _quantity(event["quantity"])
        original = _quantity(line[0])
        remaining = original - allocated - unavailable
        if remaining < 0:
            raise CoreOrderError("packing conservation is violated")
        return {"original_quantity": _format(original), "active_allocated": _format(allocated), "active_unavailable": _format(unavailable), "remaining_unallocated": _format(remaining)}
