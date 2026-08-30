"""Core-owned local packing-plan persistence with an append-only audit ledger.

Phase C only: it has no source reader, service, UI, or source-write capability.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock

from apc_core.packing_state import PackingLine, PackingPlan, PackingStateError


_HEX = frozenset("0123456789abcdef")
_ACTIONS = frozenset({"ALLOCATE", "UNAVAILABLE", "REVERSE"})
_STATUSES = frozenset({"OPEN", "FROZEN", "CLOSED", "VOIDED"})


def _decimal_text(value: object) -> str:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError("invalid quantity")
    return str(value)


def _reference_values(reference: object) -> tuple[str, str, str, str]:
    values = tuple(getattr(reference, name, None) for name in ("source_type", "document_id", "line_id", "source_sha256"))
    if any(type(value) is not str or not value for value in values):
        raise ValueError("invalid source reference")
    return values  # type: ignore[return-value]


def _decimal_fits(original: object, candidate: object, existing: object) -> int:
    """SQLite trigger helper: compare decimal text without float coercion."""
    try:
        prior = sum((Decimal(part) for part in str(existing or "").split("|") if part), Decimal("0"))
        amount = Decimal(str(candidate))
        limit = Decimal(str(original))
    except (InvalidOperation, ValueError):
        return 0
    return int(amount > 0 and prior + amount <= limit)


def _decimal_positive(value: object) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0
    return int(amount.is_finite() and amount > 0)


def _request(action: str, **values: object) -> str:
    return json.dumps({"action": action, **values}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class PackingStore:
    """A local SQLite boundary for Phase B packing values and audit events."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "apc_core_packing.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._write_depth = 0
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.create_function("packing_decimal_fits", 3, _decimal_fits)
        self.connection.create_function("packing_decimal_positive", 1, _decimal_positive)
        self.connection.create_function("packing_write_allowed", 0, lambda: int(self._write_depth > 0))
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._schema()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            self.connection.close()
            raise

    @contextmanager
    def _write_scope(self):
        self._write_depth += 1
        try:
            yield
        finally:
            self._write_depth -= 1

    def _schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS packing_plans (
              plan_id TEXT PRIMARY KEY,
              provenance TEXT NOT NULL CHECK(length(provenance)=64 AND provenance NOT GLOB '*[^0123456789abcdef]*'),
              status TEXT NOT NULL CHECK(status IN ('OPEN','FROZEN','CLOSED','VOIDED')),
              version INTEGER NOT NULL CHECK(version >= 0),
              created_by TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS packing_lines (
              plan_id TEXT NOT NULL REFERENCES packing_plans(plan_id),
              source_type TEXT NOT NULL, document_id TEXT NOT NULL, line_id TEXT NOT NULL,
              source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0123456789abcdef]*'),
              original_quantity TEXT NOT NULL, chapter TEXT NOT NULL,
              PRIMARY KEY(plan_id,source_type,document_id,line_id,source_sha256),
              CHECK(original_quantity <> '')
            );
            CREATE TABLE IF NOT EXISTS packing_boxes (
              plan_id TEXT NOT NULL REFERENCES packing_plans(plan_id), box_number INTEGER NOT NULL CHECK(box_number > 0),
              PRIMARY KEY(plan_id,box_number)
            );
            CREATE TABLE IF NOT EXISTS packing_mutations (
              mutation_id TEXT PRIMARY KEY,
              plan_id TEXT NOT NULL REFERENCES packing_plans(plan_id),
              source_type TEXT, document_id TEXT, line_id TEXT, source_sha256 TEXT,
              action TEXT NOT NULL CHECK(action IN ('ALLOCATE','UNAVAILABLE','REVERSE')),
              box_number INTEGER, quantity TEXT, reason TEXT,
              reverses_mutation_id TEXT REFERENCES packing_mutations(mutation_id),
              actor TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, expected_version INTEGER NOT NULL,
              CHECK((action='REVERSE' AND reverses_mutation_id IS NOT NULL AND quantity IS NULL AND box_number IS NULL)
                 OR (action IN ('ALLOCATE','UNAVAILABLE') AND reverses_mutation_id IS NULL AND quantity <> ''))
            );
            CREATE TABLE IF NOT EXISTS packing_audit (
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
              plan_id TEXT NOT NULL REFERENCES packing_plans(plan_id), action TEXT NOT NULL,
              outcome TEXT NOT NULL CHECK(outcome IN ('APPLIED','REJECTED','CONFLICT','REVERSED')),
              actor TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL,
              mutation_id TEXT REFERENCES packing_mutations(mutation_id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TRIGGER IF NOT EXISTS packing_plans_insert_guard BEFORE INSERT ON packing_plans
              WHEN NOT packing_write_allowed() BEGIN SELECT RAISE(ABORT,'packing store write scope required'); END;
            CREATE TRIGGER IF NOT EXISTS packing_plans_update_guard BEFORE UPDATE ON packing_plans
              WHEN NOT packing_write_allowed() OR NEW.provenance<>OLD.provenance OR NEW.status<>OLD.status
                OR NEW.created_by<>OLD.created_by OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.version<>OLD.version+1
              BEGIN SELECT RAISE(ABORT,'immutable packing plan'); END;
            CREATE TRIGGER IF NOT EXISTS packing_plans_no_delete BEFORE DELETE ON packing_plans
              BEGIN SELECT RAISE(ABORT,'immutable packing plan'); END;
            CREATE TRIGGER IF NOT EXISTS packing_lines_insert_guard BEFORE INSERT ON packing_lines
              WHEN NOT packing_write_allowed() OR NEW.source_sha256<>(SELECT provenance FROM packing_plans WHERE plan_id=NEW.plan_id)
                OR NOT packing_decimal_positive(NEW.original_quantity)
              BEGIN SELECT RAISE(ABORT,'invalid immutable packing line'); END;
            CREATE TRIGGER IF NOT EXISTS packing_lines_no_update BEFORE UPDATE ON packing_lines
              BEGIN SELECT RAISE(ABORT,'immutable packing line'); END;
            CREATE TRIGGER IF NOT EXISTS packing_lines_no_delete BEFORE DELETE ON packing_lines
              BEGIN SELECT RAISE(ABORT,'immutable packing line'); END;
            CREATE TRIGGER IF NOT EXISTS packing_boxes_insert_guard BEFORE INSERT ON packing_boxes
              WHEN NOT packing_write_allowed() BEGIN SELECT RAISE(ABORT,'packing store write scope required'); END;
            CREATE TRIGGER IF NOT EXISTS packing_boxes_no_update BEFORE UPDATE ON packing_boxes
              BEGIN SELECT RAISE(ABORT,'immutable packing box'); END;
            CREATE TRIGGER IF NOT EXISTS packing_boxes_no_delete BEFORE DELETE ON packing_boxes
              BEGIN SELECT RAISE(ABORT,'immutable packing box'); END;
            CREATE TRIGGER IF NOT EXISTS packing_mutations_insert_guard BEFORE INSERT ON packing_mutations
              WHEN NOT packing_write_allowed() OR NEW.expected_version<>(SELECT version FROM packing_plans WHERE plan_id=NEW.plan_id)
              BEGIN SELECT RAISE(ABORT,'invalid packing mutation version'); END;
            CREATE TRIGGER IF NOT EXISTS packing_mutations_no_update BEFORE UPDATE ON packing_mutations
              BEGIN SELECT RAISE(ABORT,'append-only packing mutation'); END;
            CREATE TRIGGER IF NOT EXISTS packing_mutations_no_delete BEFORE DELETE ON packing_mutations
              BEGIN SELECT RAISE(ABORT,'append-only packing mutation'); END;
            CREATE TRIGGER IF NOT EXISTS packing_audit_insert_guard BEFORE INSERT ON packing_audit
              WHEN NOT packing_write_allowed() BEGIN SELECT RAISE(ABORT,'packing store write scope required'); END;
            CREATE TRIGGER IF NOT EXISTS packing_audit_no_update BEFORE UPDATE ON packing_audit
              BEGIN SELECT RAISE(ABORT,'append-only packing audit'); END;
            CREATE TRIGGER IF NOT EXISTS packing_audit_no_delete BEFORE DELETE ON packing_audit
              BEGIN SELECT RAISE(ABORT,'append-only packing audit'); END;
            CREATE TRIGGER IF NOT EXISTS packing_mutations_reference_exists BEFORE INSERT ON packing_mutations
              WHEN NEW.action IN ('ALLOCATE','UNAVAILABLE') AND NOT EXISTS (
                SELECT 1 FROM packing_lines WHERE plan_id=NEW.plan_id AND source_type=NEW.source_type
                  AND document_id=NEW.document_id AND line_id=NEW.line_id AND source_sha256=NEW.source_sha256
              ) BEGIN SELECT RAISE(ABORT,'unknown packing line'); END;
            CREATE TRIGGER IF NOT EXISTS packing_mutations_box_exists BEFORE INSERT ON packing_mutations
              WHEN NEW.action='ALLOCATE' AND NOT EXISTS (
                SELECT 1 FROM packing_boxes WHERE plan_id=NEW.plan_id AND box_number=NEW.box_number
              ) BEGIN SELECT RAISE(ABORT,'unknown packing box'); END;
            CREATE TRIGGER IF NOT EXISTS packing_mutations_remaining BEFORE INSERT ON packing_mutations
              WHEN NEW.action IN ('ALLOCATE','UNAVAILABLE') AND NOT packing_decimal_fits(
                (SELECT original_quantity FROM packing_lines WHERE plan_id=NEW.plan_id AND source_type=NEW.source_type
                  AND document_id=NEW.document_id AND line_id=NEW.line_id AND source_sha256=NEW.source_sha256),
                NEW.quantity,
                (SELECT GROUP_CONCAT(m.quantity, '|') FROM packing_mutations m
                  WHERE m.plan_id=NEW.plan_id AND m.source_type=NEW.source_type AND m.document_id=NEW.document_id
                    AND m.line_id=NEW.line_id AND m.source_sha256=NEW.source_sha256 AND m.action IN ('ALLOCATE','UNAVAILABLE')
                    AND NOT EXISTS (SELECT 1 FROM packing_mutations r WHERE r.action='REVERSE' AND r.reverses_mutation_id=m.mutation_id))
              ) BEGIN SELECT RAISE(ABORT,'packing quantity exceeds remaining'); END;
            CREATE TRIGGER IF NOT EXISTS packing_mutations_reverse_valid BEFORE INSERT ON packing_mutations
              WHEN NEW.action='REVERSE' AND (NOT EXISTS (SELECT 1 FROM packing_mutations m WHERE m.mutation_id=NEW.reverses_mutation_id
                AND m.plan_id=NEW.plan_id AND m.action IN ('ALLOCATE','UNAVAILABLE'))
                OR EXISTS (SELECT 1 FROM packing_mutations r WHERE r.action='REVERSE' AND r.reverses_mutation_id=NEW.reverses_mutation_id))
              BEGIN SELECT RAISE(ABORT,'invalid packing reversal'); END;
            """
        )

    @staticmethod
    def _text(value: object, label: str) -> str:
        if type(value) is not str or not value:
            raise ValueError(f"invalid {label}")
        return value

    def _audit(self, plan_id: str, action: str, outcome: str, actor: str, key: str, request_json: str, mutation_id: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO packing_audit(plan_id,action,outcome,actor,idempotency_key,request_json,mutation_id) VALUES (?,?,?,?,?,?,?)",
            (plan_id, action, outcome, actor, key, request_json, mutation_id),
        )

    def _replay(self, key: str, request_json: str) -> str | None:
        row = self.connection.execute("SELECT plan_id,request_json FROM packing_audit WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row[1] != request_json:
            raise ValueError("idempotency key mismatch")
        return str(row[0])

    def _plan_row(self, plan_id: object) -> tuple[str, str, int]:
        identifier = self._text(plan_id, "plan id")
        row = self.connection.execute("SELECT provenance,status,version FROM packing_plans WHERE plan_id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("unknown packing plan")
        return str(row[0]), str(row[1]), int(row[2])

    def load_plan(self, plan_id: object) -> PackingPlan:
        identifier = self._text(plan_id, "plan id")
        provenance, status, version = self._plan_row(identifier)
        rows = self.connection.execute(
            "SELECT source_type,document_id,line_id,source_sha256,original_quantity,chapter FROM packing_lines WHERE plan_id=? ORDER BY rowid", (identifier,)
        ).fetchall()
        from apc_core.order_invoice_workspace import SourceLineReference

        lines = tuple(PackingLine(SourceLineReference(*row[:4]), Decimal(row[4]), row[5]) for row in rows)
        plan = PackingPlan.open(identifier, provenance, lines)
        boxes = self.connection.execute("SELECT box_number FROM packing_boxes WHERE plan_id=? ORDER BY box_number", (identifier,)).fetchall()
        for (number,) in boxes:
            plan, _ = plan.create_box(number, expected_version=plan.version)
        mutations = self.connection.execute(
            "SELECT mutation_id,source_type,document_id,line_id,source_sha256,action,box_number,quantity,reason FROM packing_mutations "
            "WHERE plan_id=? AND action IN ('ALLOCATE','UNAVAILABLE') AND NOT EXISTS "
            "(SELECT 1 FROM packing_mutations r WHERE r.action='REVERSE' AND r.reverses_mutation_id=packing_mutations.mutation_id) ORDER BY rowid",
            (identifier,),
        ).fetchall()
        for _, source_type, document_id, line_id, source_sha256, action, box_number, quantity, reason in mutations:
            reference = SourceLineReference(source_type, document_id, line_id, source_sha256)
            if action == "ALLOCATE":
                plan = plan.allocate(reference, box_number, Decimal(quantity), expected_version=plan.version)
            else:
                plan = plan.mark_unavailable(reference, Decimal(quantity), reason, expected_version=plan.version)
        from apc_core.packing_state import PlanStatus
        # Reversal events can advance the optimistic-concurrency version without
        # changing the active Phase B value state. Restore that persisted version
        # directly rather than fabricating a domain mutation.
        return PackingPlan(
            plan.plan_id,
            plan.provenance,
            plan.lines,
            PlanStatus(status),
            version,
            plan.boxes,
            plan.allocations,
            plan.unavailable,
        )

    def _apply(self, plan_id: str, expected_version: object) -> tuple[str, int]:
        provenance, status, version = self._plan_row(plan_id)
        if type(expected_version) is not int or expected_version != version or status != "OPEN":
            raise ValueError("stale or non-open packing plan")
        return provenance, version

    def create_plan(self, plan: object, *, actor: object, idempotency_key: object) -> PackingPlan:
        if type(plan) is not PackingPlan:
            raise ValueError("invalid packing plan")
        staff = self._text(actor, "actor")
        key = self._text(idempotency_key, "idempotency key")
        request_json = _request(
            "CREATE_PLAN",
            plan_id=plan.plan_id,
            provenance=plan.provenance,
            status=plan.status.value,
            version=plan.version,
            lines=[
                [*_reference_values(line.reference), str(line.quantity), line.chapter]
                for line in plan.lines
            ],
        )
        with self._lock:
            replay = self._replay(key, request_json)
            if replay is not None:
                return self.load_plan(replay)
            try:
                with self._write_scope():
                    self.connection.execute("BEGIN IMMEDIATE")
                    self.connection.execute("INSERT INTO packing_plans(plan_id,provenance,status,version,created_by,idempotency_key) VALUES (?,?,?,?,?,?)", (plan.plan_id, plan.provenance, plan.status.value, plan.version, staff, key))
                    for line in plan.lines:
                        source_type, document_id, line_id, source_sha256 = _reference_values(line.reference)
                        self.connection.execute("INSERT INTO packing_lines(plan_id,source_type,document_id,line_id,source_sha256,original_quantity,chapter) VALUES (?,?,?,?,?,?,?)", (plan.plan_id, source_type, document_id, line_id, source_sha256, str(line.quantity), line.chapter))
                    self._audit(plan.plan_id, "CREATE_PLAN", "APPLIED", staff, key, request_json)
                    self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.load_plan(plan.plan_id)

    def create_box(self, plan_id: object, number: object, *, actor: object, idempotency_key: object, expected_version: object) -> PackingPlan:
        identifier, staff, key = self._text(plan_id, "plan id"), self._text(actor, "actor"), self._text(idempotency_key, "idempotency key")
        if type(number) is not int or number <= 0:
            raise ValueError("invalid box")
        request_json = _request("CREATE_BOX", plan_id=identifier, number=number, expected_version=expected_version)
        with self._lock:
            replay = self._replay(key, request_json)
            if replay is not None:
                return self.load_plan(replay)
            try:
                with self._write_scope():
                    self.connection.execute("BEGIN IMMEDIATE")
                    _, version = self._apply(identifier, expected_version)
                    self.connection.execute("INSERT INTO packing_boxes(plan_id,box_number) VALUES (?,?)", (identifier, number))
                    self.connection.execute("UPDATE packing_plans SET version=? WHERE plan_id=?", (version + 1, identifier))
                    self._audit(identifier, "CREATE_BOX", "APPLIED", staff, key, request_json)
                    self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.load_plan(identifier)

    def allocate(self, plan_id: object, reference: object, box_number: object, quantity: object, *, actor: object, idempotency_key: object, expected_version: object) -> PackingPlan:
        return self._record("ALLOCATE", plan_id, reference, box_number, quantity, None, actor, idempotency_key, expected_version)

    def mark_unavailable(self, plan_id: object, reference: object, quantity: object, reason: object, *, actor: object, idempotency_key: object, expected_version: object) -> PackingPlan:
        return self._record("UNAVAILABLE", plan_id, reference, None, quantity, reason, actor, idempotency_key, expected_version)

    def _record(self, action: str, plan_id: object, reference: object, box_number: object, quantity: object, reason: object, actor: object, idempotency_key: object, expected_version: object) -> PackingPlan:
        identifier, staff, key = self._text(plan_id, "plan id"), self._text(actor, "actor"), self._text(idempotency_key, "idempotency key")
        source_type, document_id, line_id, source_sha256 = _reference_values(reference)
        amount = _decimal_text(quantity)
        if action == "ALLOCATE":
            if type(box_number) is not int or box_number <= 0:
                raise ValueError("invalid box")
        elif type(reason) is not str or not reason:
            raise ValueError("invalid unavailable reason")
        request_json = _request(action, plan_id=identifier, reference=[source_type, document_id, line_id, source_sha256], box_number=box_number, quantity=amount, reason=reason, expected_version=expected_version)
        with self._lock:
            replay = self._replay(key, request_json)
            if replay is not None:
                return self.load_plan(replay)
            try:
                with self._write_scope():
                    self.connection.execute("BEGIN IMMEDIATE")
                    _, version = self._apply(identifier, expected_version)
                    current = self.load_plan(identifier)
                    if action == "ALLOCATE":
                        current.allocate(reference, box_number, Decimal(amount), expected_version=version)
                    else:
                        current.mark_unavailable(reference, Decimal(amount), reason, expected_version=version)
                    mutation_id = str(uuid.uuid4())
                    self.connection.execute("INSERT INTO packing_mutations(mutation_id,plan_id,source_type,document_id,line_id,source_sha256,action,box_number,quantity,reason,actor,idempotency_key,expected_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (mutation_id, identifier, source_type, document_id, line_id, source_sha256, action, box_number, amount, reason, staff, key, version))
                    self.connection.execute("UPDATE packing_plans SET version=? WHERE plan_id=?", (version + 1, identifier))
                    self._audit(identifier, action, "APPLIED", staff, key, request_json, mutation_id)
                    self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.load_plan(identifier)

    def reverse(self, plan_id: object, original_idempotency_key: object, *, actor: object, idempotency_key: object, expected_version: object, reason: object) -> PackingPlan:
        identifier, original_key, staff, key = self._text(plan_id, "plan id"), self._text(original_idempotency_key, "original idempotency key"), self._text(actor, "actor"), self._text(idempotency_key, "idempotency key")
        if type(reason) is not str or not reason:
            raise ValueError("invalid reversal reason")
        request_json = _request("REVERSE", plan_id=identifier, original_idempotency_key=original_key, reason=reason, expected_version=expected_version)
        with self._lock:
            replay = self._replay(key, request_json)
            if replay is not None:
                return self.load_plan(replay)
            try:
                with self._write_scope():
                    self.connection.execute("BEGIN IMMEDIATE")
                    _, version = self._apply(identifier, expected_version)
                    row = self.connection.execute("SELECT mutation_id FROM packing_mutations WHERE plan_id=? AND idempotency_key=? AND action IN ('ALLOCATE','UNAVAILABLE')", (identifier, original_key)).fetchone()
                    if row is None:
                        raise ValueError("unknown original packing mutation")
                    mutation_id = str(uuid.uuid4())
                    self.connection.execute("INSERT INTO packing_mutations(mutation_id,plan_id,action,reverses_mutation_id,reason,actor,idempotency_key,expected_version) VALUES (?,?,?,?,?,?,?,?)", (mutation_id, identifier, "REVERSE", row[0], reason, staff, key, version))
                    self.connection.execute("UPDATE packing_plans SET version=? WHERE plan_id=?", (version + 1, identifier))
                    self._audit(identifier, "REVERSE", "REVERSED", staff, key, request_json, mutation_id)
                    self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return self.load_plan(identifier)

    def count_audit_events(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM packing_audit").fetchone()[0])

    def close(self) -> None:
        self.connection.close()
