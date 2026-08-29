"""Core-owned, local-only invoice draft persistence.

This module deliberately has no snapshot reader or legacy-table integration.  It
persists caller-supplied accepted-snapshot digests as immutable draft provenance.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from functools import wraps
from pathlib import Path
from threading import RLock


def _locked(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


_DRAFT_STATUSES = frozenset({"draft", "review", "conflicted"})
_DECISIONS = {"submit_for_review": ("draft", "review"), "return_to_draft": ("review", "draft")}
_HEX = frozenset("0123456789abcdef")
_LINE_FIELDS = ("order_id", "order_line_no", "item_id", "quantity")


class InvoiceDraftStore:
    """A narrow local store for unissued invoice drafts and append-only audit."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "apc_core.sqlite"
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS invoice_drafts ("
            "draft_id TEXT NOT NULL PRIMARY KEY, accepted_snapshot_sha256 TEXT NOT NULL "
            "CHECK(length(accepted_snapshot_sha256)=64 AND accepted_snapshot_sha256 NOT GLOB '*[^0123456789abcdef]*'), "
            "created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "status TEXT NOT NULL CHECK(status IN ('draft','review','conflicted')), "
            "idempotency_key TEXT NOT NULL UNIQUE, submission_json TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS invoice_draft_lines ("
            "draft_id TEXT NOT NULL REFERENCES invoice_drafts(draft_id), line_no INTEGER NOT NULL, item_id TEXT NOT NULL, quantity TEXT NOT NULL, "
            "PRIMARY KEY(draft_id,line_no))"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS invoice_line_allocations ("
            "draft_id TEXT NOT NULL REFERENCES invoice_drafts(draft_id), order_id TEXT NOT NULL, order_line_no TEXT NOT NULL, line_no INTEGER NOT NULL, "
            "FOREIGN KEY(draft_id,line_no) REFERENCES invoice_draft_lines(draft_id,line_no), "
            "UNIQUE(draft_id,order_id,order_line_no), UNIQUE(draft_id,line_no))"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS invoice_draft_conflicts ("
            "conflict_id TEXT NOT NULL PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES invoice_drafts(draft_id), reason TEXT NOT NULL, "
            "created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "resolution TEXT, resolved_by TEXT, resolved_at TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS invoice_draft_audit ("
            "audit_id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT NOT NULL REFERENCES invoice_drafts(draft_id), action TEXT NOT NULL, "
            "actor TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self.connection.execute(
            "CREATE TRIGGER IF NOT EXISTS invoice_drafts_snapshot_immutable "
            "BEFORE UPDATE OF accepted_snapshot_sha256 ON invoice_drafts "
            "BEGIN SELECT RAISE(ABORT,'immutable accepted snapshot'); END"
        )
        self.connection.execute(
            "CREATE TRIGGER IF NOT EXISTS invoice_draft_audit_no_update "
            "BEFORE UPDATE ON invoice_draft_audit BEGIN SELECT RAISE(ABORT,'append-only audit'); END"
        )
        self.connection.execute(
            "CREATE TRIGGER IF NOT EXISTS invoice_draft_audit_no_delete "
            "BEFORE DELETE ON invoice_draft_audit BEGIN SELECT RAISE(ABORT,'append-only audit'); END"
        )
        self._validate_schema()
        self.connection.commit()

    def _validate_schema(self) -> None:
        required = {
            "invoice_drafts": (
                ("accepted_snapshot_sha256", "created_by", "created_at", "status", "idempotency_key", "submission_json"),
                ("check(length(accepted_snapshot_sha256)=64andaccepted_snapshot_sha256notglob'*[^0123456789abcdef]*')", "draft_idtextnotnullprimarykey", "check(statusin('draft','review','conflicted'))", "idempotency_keytextnotnullunique"),
            ),
            "invoice_draft_lines": (("draft_id", "line_no", "item_id", "quantity"), ("referencesinvoice_drafts(draft_id)", "primarykey(draft_id,line_no)")),
            "invoice_line_allocations": (("draft_id", "order_id", "order_line_no", "line_no"), ("referencesinvoice_drafts(draft_id)", "foreignkey(draft_id,line_no)referencesinvoice_draft_lines(draft_id,line_no)", "unique(draft_id,order_id,order_line_no)", "unique(draft_id,line_no)")),
            "invoice_draft_conflicts": (("conflict_id", "draft_id", "reason", "created_by", "created_at", "resolution", "resolved_by", "resolved_at"), ("conflict_idtextnotnullprimarykey", "referencesinvoice_drafts(draft_id)",)),
            "invoice_draft_audit": (("audit_id", "draft_id", "action", "actor", "details_json", "created_at"), ("audit_idintegerprimarykeyautoincrement", "referencesinvoice_drafts(draft_id)")),
        }
        for table, (columns, markers) in required.items():
            actual = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            sql_row = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            sql = "" if sql_row is None or sql_row[0] is None else "".join(sql_row[0].lower().split())
            if not set(columns).issubset(actual) or any(marker not in sql for marker in markers):
                raise ValueError("incompatible invoice draft schema")
        triggers = {
            "invoice_drafts_snapshot_immutable": "beforeupdateofaccepted_snapshot_sha256oninvoice_draftsbeginselectraise(abort,'immutableacceptedsnapshot');end",
            "invoice_draft_audit_no_update": "beforeupdateoninvoice_draft_auditbeginselectraise(abort,'append-onlyaudit');end",
            "invoice_draft_audit_no_delete": "beforedeleteoninvoice_draft_auditbeginselectraise(abort,'append-onlyaudit');end",
        }
        for name, marker in triggers.items():
            row = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            sql = "" if row is None or row[0] is None else "".join(row[0].lower().split())
            if marker not in sql:
                raise ValueError("incompatible invoice draft schema")

    @staticmethod
    def _text(value: object, label: str) -> str:
        if type(value) is not str or not value:
            raise ValueError(f"invalid {label}")
        return value

    @classmethod
    def _snapshot_sha256(cls, value: object) -> str:
        if type(value) is not str or len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError("invalid accepted snapshot")
        return value

    @classmethod
    def _lines(cls, lines: object) -> list[dict[str, str]]:
        if type(lines) is not list or not lines:
            raise ValueError("invalid lines")
        clean: list[dict[str, str]] = []
        allocation_keys: set[tuple[str, str]] = set()
        for line in lines:
            if type(line) is not dict or tuple(line) != _LINE_FIELDS:
                raise ValueError("invalid line")
            value = {field: cls._text(line[field], "line") for field in _LINE_FIELDS}
            allocation = (value["order_id"], value["order_line_no"])
            if allocation in allocation_keys:
                raise ValueError("duplicate allocation")
            allocation_keys.add(allocation)
            clean.append(value)
        return clean

    @staticmethod
    def _submission(snapshot: str, actor: str, lines: list[dict[str, str]]) -> str:
        return json.dumps(
            {"accepted_snapshot_sha256": snapshot, "created_by": actor, "lines": lines},
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def _audit(self, draft_id: str, action: str, actor: str, details: dict[str, str]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO invoice_draft_audit(draft_id,action,actor,details_json) VALUES (?,?,?,?)",
            (draft_id, action, actor, json.dumps(details, sort_keys=True, separators=(",", ":"))),
        )
        return int(cursor.lastrowid)

    def _draft(self, draft_id: str) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT draft_id,accepted_snapshot_sha256,created_by,created_at,status FROM invoice_drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if row is None:
            return None
        lines = self.connection.execute(
            "SELECT l.line_no,a.order_id,a.order_line_no,l.item_id,l.quantity "
            "FROM invoice_draft_lines l JOIN invoice_line_allocations a "
            "ON a.draft_id=l.draft_id AND a.line_no=l.line_no WHERE l.draft_id=? ORDER BY l.line_no",
            (draft_id,),
        ).fetchall()
        return {
            "draft_id": row[0], "accepted_snapshot_sha256": row[1], "created_by": row[2],
            "created_at": row[3], "status": row[4],
            "lines": [
                {"line_no": line[0], "order_id": line[1], "order_line_no": line[2], "item_id": line[3], "quantity": line[4]}
                for line in lines
            ],
        }

    @_locked
    def create_draft(self, accepted_snapshot_sha256: object, actor: object, idempotency_key: object, lines: object) -> dict[str, object]:
        snapshot = self._snapshot_sha256(accepted_snapshot_sha256)
        creator = self._text(actor, "actor")
        key = self._text(idempotency_key, "idempotency key")
        clean_lines = self._lines(lines)
        submission = self._submission(snapshot, creator, clean_lines)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT draft_id,submission_json FROM invoice_drafts WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing[1] != submission:
                    raise ValueError("idempotency key mismatch")
                self.connection.commit()
                return self._draft(existing[0]) or {}
            draft_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO invoice_drafts(draft_id,accepted_snapshot_sha256,created_by,status,idempotency_key,submission_json) "
                "VALUES (?,?,?,'draft',?,?)", (draft_id, snapshot, creator, key, submission),
            )
            for line_no, line in enumerate(clean_lines, 1):
                self.connection.execute(
                    "INSERT INTO invoice_draft_lines(draft_id,line_no,item_id,quantity) VALUES (?,?,?,?)",
                    (draft_id, line_no, line["item_id"], line["quantity"]),
                )
                self.connection.execute(
                    "INSERT INTO invoice_line_allocations(draft_id,order_id,order_line_no,line_no) VALUES (?,?,?,?)",
                    (draft_id, line["order_id"], line["order_line_no"], line_no),
                )
            self._audit(draft_id, "created", creator, {"status": "draft"})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self._draft(draft_id) or {}

    @_locked
    def _transition(self, draft_id: object, from_status: str, to_status: str, action: str, actor: object) -> dict[str, object]:
        identifier = self._text(draft_id, "draft id")
        staff = self._text(actor, "actor")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            updated = self.connection.execute(
                "UPDATE invoice_drafts SET status=? WHERE draft_id=? AND status=?", (to_status, identifier, from_status)
            )
            if updated.rowcount != 1:
                raise ValueError("unknown status transition")
            audit_id = self._audit(identifier, action, staff, {"from": from_status, "to": to_status})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"draft_id": identifier, "status": to_status, "audit_id": audit_id}

    def record_staff_decision(self, draft_id: object, decision: object, actor: object) -> dict[str, object]:
        if type(decision) is not str or decision not in _DECISIONS:
            raise ValueError("unknown status transition")
        before, after = _DECISIONS[decision]
        return self._transition(draft_id, before, after, decision, actor)

    @_locked
    def record_conflict(self, draft_id: object, reason: object, actor: object) -> dict[str, object]:
        identifier = self._text(draft_id, "draft id")
        conflict_reason = self._text(reason, "conflict")
        staff = self._text(actor, "actor")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            conflict_id = str(uuid.uuid4())
            updated = self.connection.execute(
                "UPDATE invoice_drafts SET status='conflicted' WHERE draft_id=? AND status IN ('draft','review')", (identifier,)
            )
            if updated.rowcount != 1:
                raise ValueError("unknown status transition")
            self.connection.execute(
                "INSERT INTO invoice_draft_conflicts(conflict_id,draft_id,reason,created_by) VALUES (?,?,?,?)",
                (conflict_id, identifier, conflict_reason, staff),
            )
            audit_id = self._audit(identifier, "conflict_recorded", staff, {"reason": conflict_reason})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"conflict_id": conflict_id, "draft_id": identifier, "status": "conflicted", "audit_id": audit_id}

    @_locked
    def resolve_conflict(self, conflict_id: object, resolution: object, actor: object) -> dict[str, object]:
        identifier = self._text(conflict_id, "conflict id")
        if resolution != "return_to_draft":
            raise ValueError("unknown status transition")
        staff = self._text(actor, "actor")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT draft_id FROM invoice_draft_conflicts WHERE conflict_id=? AND resolution IS NULL", (identifier,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown status transition")
            resolved = self.connection.execute(
                "UPDATE invoice_draft_conflicts SET resolution=?,resolved_by=?,resolved_at=CURRENT_TIMESTAMP "
                "WHERE conflict_id=? AND resolution IS NULL", (resolution, staff, identifier)
            )
            restored = self.connection.execute(
                "UPDATE invoice_drafts SET status='draft' WHERE draft_id=? AND status='conflicted'", (row[0],)
            )
            if resolved.rowcount != 1 or restored.rowcount != 1:
                raise ValueError("unknown status transition")
            audit_id = self._audit(row[0], "conflict_resolved", staff, {"resolution": resolution})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"conflict_id": identifier, "draft_id": row[0], "status": "draft", "audit_id": audit_id}

    def audit_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM invoice_draft_audit").fetchone()[0])

    def close(self) -> None:
        self.connection.close()
