"""P3a Core-owned no-money invoice aggregate and lifecycle persistence.

This module is local persistence only. It has no server wiring, legacy/MDB/NAS
access, pricing, numbering, tax, currency, AWB, printing, or accounting paths.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
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
        self._p4_workflow = 4 in versions

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

    def _p3_lifecycle_available(self, invoice_id: str) -> None:
        if self._p4_workflow and self.connection.execute(
            "SELECT 1 FROM core_invoice_documents WHERE base_invoice_id=?", (invoice_id,)
        ).fetchone() is not None:
            raise CoreInvoiceError("P4 document owns invoice lifecycle")

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
            self._p3_lifecycle_available(invoice_id)
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
            self._p3_lifecycle_available(invoice_id)
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
            if self.connection.execute("SELECT 1 FROM core_invoice_events WHERE conflict_id=? AND event_kind='conflict_resolution'", (conflict_id,)).fetchone() is not None:
                raise CoreInvoiceError("conflict is already resolved")
            self.connection.execute("INSERT INTO core_invoice_events(event_id,invoice_id,conflict_id,event_kind,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?,?)", (event_id, invoice_id, conflict_id, "conflict_resolution", actor, expected_version, idempotency_key))
            self._advance(invoice_id)
            row = self.connection.execute("SELECT * FROM core_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
            return {**self._receipt(row), "conflict_id": conflict_id}


class CoreInvoiceWorkflowStore:
    """P4 local workflow persistence; no source or runtime integration."""
    def __init__(self, database_path: Path):
        if not Path(database_path).is_file(): raise CoreInvoiceError("Core invoice workflow database is missing")
        self.connection = sqlite3.connect(f"{Path(database_path).resolve().as_uri()}?mode=rw", uri=True)
        self.connection.row_factory = sqlite3.Row; self.connection.execute("PRAGMA foreign_keys=ON")
        try: versions = {r[0] for r in self.connection.execute("SELECT version FROM core_schema_migrations")}
        except sqlite3.Error as e: self.connection.close(); raise CoreInvoiceError("Core invoice workflow migrations have not been applied") from e
        if 5 not in versions: self.connection.close(); raise CoreInvoiceError("Core invoice workflow migrations have not been applied")
        try:
            required = {"core_invoice_reference_counters", "core_invoice_reference_allocations", "core_invoice_document_context"}
            tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            incomplete = self.connection.execute("SELECT 1 FROM core_invoice_documents d WHERE NOT EXISTS (SELECT 1 FROM core_invoice_document_context c WHERE c.invoice_id=d.invoice_id) OR NOT EXISTS (SELECT 1 FROM core_invoice_reference_allocations a WHERE a.invoice_id=d.invoice_id) LIMIT 1").fetchone()
        except sqlite3.Error as e:
            self.connection.close(); raise CoreInvoiceError("Core invoice workflow migrations have not been applied") from e
        if not required.issubset(tables) or incomplete is not None:
            self.connection.close(); raise CoreInvoiceError("Core invoice workflow migrations have not been applied")
    def close(self): self.connection.close()
    def _receipt(self,r):
        context=self.connection.execute("SELECT temporary_reference,consignee,delivery_reference FROM core_invoice_document_context WHERE invoice_id=?",(r["invoice_id"],)).fetchone()
        return {"invoice_id":r["invoice_id"],"state":r["state"],"version":r["version"],"permanent_number":r["permanent_number"],"temporary_reference":context["temporary_reference"],"consignee":context["consignee"],"delivery_reference":context["delivery_reference"]}
    @staticmethod
    def _price(v, nullable=False):
        if v is None and nullable: return None
        if type(v) is not str or not v.strip(): raise CoreInvoiceError("positive price is required")
        try: d=Decimal(v)
        except (InvalidOperation,ValueError) as e: raise CoreInvoiceError("positive price is required") from e
        if not d.is_finite() or d<=0: raise CoreInvoiceError("positive price is required")
        return format(d,"f")
    @staticmethod
    def _customer_code(v):
        value=_text(v,"customer code").strip().upper()
        if re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*",value) is None: raise CoreInvoiceError("customer code is invalid")
        return value
    @staticmethod
    def _reference_year(v):
        if type(v) is not int or isinstance(v,bool) or not 2000<=v<=2099: raise CoreInvoiceError("reference year is invalid")
        return v
    def _doc(self,i,v):
        r=self.connection.execute("SELECT * FROM core_invoice_documents WHERE invoice_id=?",(i,)).fetchone()
        if r is None: raise CoreInvoiceError("invoice document is unknown")
        if r["version"]!=v: raise CoreInvoiceError("expected version conflicts")
        return r
    def _create(self,i,b,a,c,p,k,expected_version,consignee,delivery_reference,reference_year,original=None):
        i,b,a,k=(_text(i,"invoice id"),_text(b,"base invoice id"),_text(a,"actor"),_text(k,"idempotency key")); c=self._customer_code(c); consignee=_text(consignee,"consignee"); delivery_reference=_text(delivery_reference,"delivery reference"); reference_year=self._reference_year(reference_year); v=_version(expected_version)
        if v: raise CoreInvoiceError("expected version conflicts")
        if type(p) is not dict: raise CoreInvoiceError("prices must be explicit")
        p={_text(x,"order line id"):self._price(y,True) for x,y in p.items()}
        with self.connection:
            r=self.connection.execute("SELECT * FROM core_invoice_documents WHERE idempotency_key=?",(k,)).fetchone()
            if r:
                if tuple(r[x] for x in ("invoice_id","base_invoice_id","created_by","customer_code","state")) != (i,b,a,c,"temporary"): raise CoreInvoiceError("idempotency key conflicts with a different command")
                actual={x["order_line_id"]:x["unit_price"] for x in self.connection.execute("SELECT l.order_line_id,p.unit_price FROM core_invoice_document_lines d JOIN core_invoice_lines l ON l.invoice_line_id=d.core_invoice_line_id JOIN core_invoice_price_events p ON p.document_line_id=d.document_line_id WHERE d.invoice_id=? AND p.event_kind='customer_code_price'",(i,))}
                if actual != p: raise CoreInvoiceError("idempotency key conflicts with a different command")
                context=self.connection.execute("SELECT customer_code,reference_year,consignee,delivery_reference FROM core_invoice_document_context WHERE invoice_id=?",(i,)).fetchone()
                if context is None or tuple(context)!=(c,reference_year,consignee,delivery_reference): raise CoreInvoiceError("idempotency key conflicts with a different command")
                return self._receipt(r)
            if original is None and self.connection.execute("SELECT 1 FROM core_invoice_documents WHERE base_invoice_id=? AND state='cancelled'", (b,)).fetchone(): raise CoreInvoiceError("cancelled base requires correction")
            lines=self.connection.execute("SELECT invoice_line_id,order_line_id FROM core_invoice_lines WHERE invoice_id=? ORDER BY invoice_line_id",(b,)).fetchall()
            if not lines: raise CoreInvoiceError("base invoice is unknown")
            if set(p)!={x["order_line_id"] for x in lines}: raise CoreInvoiceError("prices must cover explicit base membership")
            self.connection.execute("INSERT INTO core_invoice_document_events VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",(f"{i}:creation",i,"creation",a,0,k))
            if original: self.connection.execute("INSERT INTO core_invoice_corrections(correction_invoice_id,original_invoice_id,created_by,idempotency_key) VALUES (?,?,?,?)",(i,original,a,f"{k}:correction"))
            members=[(f"{i}:line:{n}",i,x["invoice_line_id"]) for n,x in enumerate(lines,1)]
            self.connection.executemany("INSERT INTO core_invoice_document_lines VALUES (?,?,?)",members)
            self.connection.executemany("INSERT INTO core_invoice_price_events(event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,price_sequence,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",[(f"{i}:price:{n}",i,m[0],"customer_code_price",p[x["order_line_id"]],c,a,0,1,f"{k}:price:{n}") for n,(m,x) in enumerate(zip(members,lines),1)])
            sequence=self.connection.execute("SELECT COALESCE(MAX(reference_sequence),0)+1 FROM core_invoice_reference_allocations WHERE customer_code=? AND reference_year=?",(c,reference_year)).fetchone()[0]
            if sequence > 999: raise CoreInvoiceError("Temporary reference sequence is full for this customer and year")
            temporary_reference=f"{c}-T{reference_year % 100:02d}-{sequence:03d}"
            self.connection.execute("INSERT INTO core_invoice_reference_allocations(invoice_id,allocation_event_id,customer_code,reference_year,reference_sequence,temporary_reference) VALUES (?,?,?,?,?,?)",(i,f"{i}:creation",c,reference_year,sequence,temporary_reference))
            self.connection.execute("INSERT INTO core_invoice_document_context(invoice_id,customer_code,reference_year,reference_sequence,temporary_reference,consignee,delivery_reference) VALUES (?,?,?,?,?,?,?)",(i,c,reference_year,sequence,temporary_reference,consignee,delivery_reference))
            self.connection.execute("INSERT INTO core_invoice_documents(invoice_id,base_invoice_id,customer_code,state,created_by,version,idempotency_key) VALUES (?,?,?,?,?,?,?)",(i,b,c,"temporary",a,1,k))
            return self._receipt(self._doc(i,1))
    def create_temporary_invoice(self,i,b,a,c,p,k,*,expected_version,consignee,delivery_reference,reference_year): return self._create(i,b,a,c,p,k,expected_version,consignee,delivery_reference,reference_year)
    def override_temporary_price(self,e,i,l,p,a,k,*,expected_version):
        e,i,l,a,k=(_text(e,"event id"),_text(i,"invoice id"),_text(l,"document line id"),_text(a,"actor"),_text(k,"idempotency key")); v=_version(expected_version); p=self._price(p)
        with self.connection:
            old=self.connection.execute("SELECT * FROM core_invoice_price_events WHERE idempotency_key=?",(k,)).fetchone()
            if old:
                if tuple(old[x] for x in ("event_id","invoice_id","document_line_id","event_kind","unit_price","actor","expected_version")) != (e,i,l,"temporary_override",p,a,v): raise CoreInvoiceError("idempotency key conflicts with a different command")
                return self._receipt(self._doc(i,old["expected_version"]+1))
            d=self._doc(i,v)
            if d["state"]!="temporary": raise CoreInvoiceError("invoice document state does not allow price override")
            self.connection.execute("INSERT INTO core_invoice_price_events(event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,price_sequence,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",(e,i,l,"temporary_override",p,d["customer_code"],a,v,v+1,k)); self.connection.execute("UPDATE core_invoice_documents SET version=version+1 WHERE invoice_id=?",(i,)); return self._receipt(self._doc(i,v+1))
    def _transition(self,e,i,a,k,v,kind,state,num=None):
        e,i,a,k=(_text(e,"event id"),_text(i,"invoice id"),_text(a,"actor"),_text(k,"idempotency key")); v=_version(v)
        with self.connection:
            old=self.connection.execute("SELECT * FROM core_invoice_document_events WHERE idempotency_key=?",(k,)).fetchone()
            if old:
                if tuple(old[x] for x in ("event_id","invoice_id","event_kind","actor","expected_version")) != (e,i,kind,a,v): raise CoreInvoiceError("idempotency key conflicts with a different command")
                return self._receipt(self._doc(i,v+1))
            d=self._doc(i,v)
            if (kind=="real_confirmation" and d["state"]!="temporary") or (kind=="cancellation" and d["state"] not in ("temporary","real")): raise CoreInvoiceError("invoice document state does not allow this lifecycle action")
            if kind == "real_confirmation" and self.connection.execute("SELECT 1 FROM core_invoice_document_lines l WHERE l.invoice_id=? AND NOT EXISTS (SELECT 1 FROM core_invoice_price_events p WHERE p.document_line_id=l.document_line_id AND p.unit_price IS NOT NULL AND CAST(p.unit_price AS REAL)>0 AND p.price_sequence=(SELECT MAX(p2.price_sequence) FROM core_invoice_price_events p2 WHERE p2.document_line_id=l.document_line_id))",(i,)).fetchone(): raise CoreInvoiceError("positive price is required")
            self.connection.execute("INSERT INTO core_invoice_document_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) VALUES (?,?,?,?,?,?)",(e,i,kind,a,v,k))
            if num is None: self.connection.execute("UPDATE core_invoice_documents SET state=?,version=version+1 WHERE invoice_id=?",(state,i))
            else: self.connection.execute("UPDATE core_invoice_documents SET state=?,permanent_number=?,version=version+1 WHERE invoice_id=?",(state,num,i))
            return self._receipt(self._doc(i,v+1))
    def confirm_real(self,e,i,n,a,k,*,expected_version): return self._transition(e,i,a,k,expected_version,"real_confirmation","real",_text(n,"permanent number"))
    def cancel(self,e,i,a,k,*,expected_version): return self._transition(e,i,a,k,expected_version,"cancellation","cancelled")
    def create_correction_temporary(self,i,o,a,c,p,k,*,expected_version,consignee,delivery_reference,reference_year):
        o=_text(o,"original invoice id"); r=self.connection.execute("SELECT base_invoice_id,state FROM core_invoice_documents WHERE invoice_id=?",(o,)).fetchone()
        if r is None or r["state"]!="cancelled": raise CoreInvoiceError("original invoice must be cancelled")
        return self._create(i,r["base_invoice_id"],a,c,p,k,expected_version,consignee,delivery_reference,reference_year,o)
    def get_invoice(self,i):
        r=self._doc(_text(i,"invoice id"),self.connection.execute("SELECT version FROM core_invoice_documents WHERE invoice_id=?",(i,)).fetchone()[0]); out=self._receipt(r)
        out["core_invoice_line_ids"]=[x[0] for x in self.connection.execute("SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=? ORDER BY document_line_id",(i,))]; out["price_events"]=[dict(x) for x in self.connection.execute("SELECT * FROM core_invoice_price_events WHERE invoice_id=? ORDER BY document_line_id,price_sequence",(i,))]; x=self.connection.execute("SELECT original_invoice_id FROM core_invoice_corrections WHERE correction_invoice_id=?",(i,)).fetchone(); out["correction_of"]=x[0] if x else None; return out
    def search_invoices(self,*,customer_code=None,state=None,temporary_reference=None,consignee=None,delivery_reference=None):
        q="SELECT d.* FROM core_invoice_documents d JOIN core_invoice_document_context c ON c.invoice_id=d.invoice_id"; vals=[]; clauses=[]
        if customer_code is not None: clauses.append("d.customer_code=?"); vals.append(self._customer_code(customer_code))
        if state is not None: clauses.append("d.state=?"); vals.append(_text(state,"state"))
        if temporary_reference is not None: clauses.append("c.temporary_reference=?"); vals.append(_text(temporary_reference,"temporary reference"))
        if consignee is not None: clauses.append("c.consignee=?"); vals.append(_text(consignee,"consignee"))
        if delivery_reference is not None: clauses.append("c.delivery_reference=?"); vals.append(_text(delivery_reference,"delivery reference"))
        if clauses: q+=" WHERE "+" AND ".join(clauses)
        return [self._receipt(x) for x in self.connection.execute(q+" ORDER BY d.created_at,d.invoice_id",vals)]
