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
_INVOICE_SCHEMA_VERSION = 5


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


def _migration_003(connection: sqlite3.Connection) -> None:
    """P3a Core-owned no-money invoice lifecycle; never part of runtime startup."""
    connection.execute(
        "CREATE TABLE core_invoices ("
        "invoice_id TEXT PRIMARY KEY NOT NULL, created_by TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "status TEXT NOT NULL CHECK(status IN ('draft','review','approved','cancelled')), "
        "version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0), idempotency_key TEXT NOT NULL UNIQUE)"
    )
    connection.execute(
        "CREATE TABLE core_invoice_lines ("
        "invoice_line_id TEXT PRIMARY KEY NOT NULL, invoice_id TEXT NOT NULL REFERENCES core_invoices(invoice_id) DEFERRABLE INITIALLY DEFERRED, "
        "order_line_id TEXT NOT NULL UNIQUE REFERENCES core_order_lines(line_id), "
        "UNIQUE(invoice_id,order_line_id))"
    )
    connection.execute(
        "CREATE TABLE core_invoice_events ("
        "event_id TEXT PRIMARY KEY NOT NULL, invoice_id TEXT NOT NULL REFERENCES core_invoices(invoice_id) DEFERRABLE INITIALLY DEFERRED, "
        "conflict_id TEXT REFERENCES core_invoice_conflicts(conflict_id), "
        "event_kind TEXT NOT NULL CHECK(event_kind IN ('creation','review_submission','approval','cancellation','evidence_conflict','conflict_resolution')), "
        "actor TEXT NOT NULL, expected_version INTEGER NOT NULL CHECK(expected_version >= 0), "
        "idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE core_invoice_conflicts ("
        "conflict_id TEXT PRIMARY KEY NOT NULL, invoice_id TEXT NOT NULL REFERENCES core_invoices(invoice_id), "
        "source_snapshot_sha256 TEXT NOT NULL REFERENCES core_source_snapshots(snapshot_sha256), "
        "imported_snapshot_sha256 TEXT NOT NULL REFERENCES core_source_snapshots(snapshot_sha256), "
        "comparison_rule TEXT NOT NULL CHECK(comparison_rule='core_imported_at_strictly_later'), "
        "source_imported_at TEXT NOT NULL, imported_snapshot_imported_at TEXT NOT NULL, "
        "UNIQUE(invoice_id,source_snapshot_sha256,imported_snapshot_sha256), "
        "CHECK(source_snapshot_sha256 <> imported_snapshot_sha256))"
    )
    for table in ("core_invoice_lines", "core_invoice_events", "core_invoice_conflicts"):
        connection.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END"
        )
        connection.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )
    connection.execute(
        "CREATE TRIGGER core_invoice_lines_fk_guard BEFORE INSERT ON core_invoice_lines "
        "WHEN NOT EXISTS (SELECT 1 FROM core_order_lines WHERE line_id=NEW.order_line_id) "
        "BEGIN SELECT RAISE(ABORT, 'core invoice line foreign key is invalid'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoice_events_fk_guard BEFORE INSERT ON core_invoice_events "
        "WHEN (NOT EXISTS (SELECT 1 FROM core_invoices WHERE invoice_id=NEW.invoice_id) AND NOT ("
        "NEW.event_kind='creation' AND NEW.conflict_id IS NULL AND NEW.expected_version=0)) "
        "OR (NEW.event_kind IN ('evidence_conflict','conflict_resolution') AND (NEW.conflict_id IS NULL OR NOT EXISTS ("
        "SELECT 1 FROM core_invoice_conflicts WHERE conflict_id=NEW.conflict_id AND invoice_id=NEW.invoice_id))) "
        "OR (NEW.event_kind NOT IN ('evidence_conflict','conflict_resolution') AND NEW.conflict_id IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT, 'core invoice event foreign key is invalid'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoice_creation_event_insert_guard BEFORE INSERT ON core_invoice_events "
        "WHEN NEW.event_kind='creation' AND EXISTS (SELECT 1 FROM core_invoices WHERE invoice_id=NEW.invoice_id) "
        "BEGIN SELECT RAISE(ABORT, 'core invoice creation event must precede invoice creation'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoices_creation_guard BEFORE INSERT ON core_invoices "
        "WHEN NEW.status <> 'draft' OR NEW.version <> 1 OR NOT EXISTS ("
        "SELECT 1 FROM core_invoice_events WHERE invoice_id=NEW.invoice_id AND event_kind='creation' "
        "AND conflict_id IS NULL AND actor=NEW.created_by AND expected_version=0 AND idempotency_key=NEW.idempotency_key"
        ") OR NOT EXISTS (SELECT 1 FROM core_invoice_lines WHERE invoice_id=NEW.invoice_id) "
        "BEGIN SELECT RAISE(ABORT, 'core invoice creation requires matching audited event and membership'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoice_conflicts_fk_guard BEFORE INSERT ON core_invoice_conflicts "
        "WHEN NOT EXISTS (SELECT 1 FROM core_invoices WHERE invoice_id=NEW.invoice_id) "
        "OR NOT EXISTS (SELECT 1 FROM core_source_snapshots WHERE snapshot_sha256=NEW.source_snapshot_sha256) "
        "OR NOT EXISTS (SELECT 1 FROM core_source_snapshots WHERE snapshot_sha256=NEW.imported_snapshot_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'core invoice conflict foreign key is invalid'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoice_conflicts_timestamp_guard BEFORE INSERT ON core_invoice_conflicts "
        "WHEN NEW.source_imported_at <> (SELECT imported_at FROM core_source_snapshots WHERE snapshot_sha256=NEW.source_snapshot_sha256) "
        "OR NEW.imported_snapshot_imported_at <> (SELECT imported_at FROM core_source_snapshots WHERE snapshot_sha256=NEW.imported_snapshot_sha256) "
        "OR julianday(NEW.source_imported_at) IS NULL OR julianday(NEW.imported_snapshot_imported_at) IS NULL "
        "OR strftime('%Y-%m-%d', NEW.source_imported_at) <> substr(NEW.source_imported_at,1,10) "
        "OR strftime('%Y-%m-%d', NEW.imported_snapshot_imported_at) <> substr(NEW.imported_snapshot_imported_at,1,10) "
        "OR NOT (NEW.source_imported_at GLOB '????-??-??T??:??:??Z' OR NEW.source_imported_at GLOB '????-??-??T??:??:??+??:??' OR NEW.source_imported_at GLOB '????-??-??T??:??:??-??:??') "
        "OR NOT (NEW.imported_snapshot_imported_at GLOB '????-??-??T??:??:??Z' OR NEW.imported_snapshot_imported_at GLOB '????-??-??T??:??:??+??:??' OR NEW.imported_snapshot_imported_at GLOB '????-??-??T??:??:??-??:??') "
        "OR julianday(NEW.imported_snapshot_imported_at) <= julianday(NEW.source_imported_at) "
        "BEGIN SELECT RAISE(ABORT, 'core invoice conflict timestamps are not strictly later timezone-aware evidence'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoices_no_delete BEFORE DELETE ON core_invoices "
        "BEGIN SELECT RAISE(ABORT, 'core invoices are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoices_lifecycle_guard BEFORE UPDATE OF status,version ON core_invoices "
        "WHEN NEW.version <> OLD.version + 1 OR NOT EXISTS ("
        "SELECT 1 FROM core_invoice_events WHERE invoice_id=OLD.invoice_id AND expected_version=OLD.version AND ("
        "(OLD.status='draft' AND NEW.status='review' AND event_kind='review_submission') OR "
        "(OLD.status='review' AND NEW.status='approved' AND event_kind='approval') OR "
        "(OLD.status <> 'cancelled' AND NEW.status='cancelled' AND event_kind='cancellation') OR "
        "(OLD.status=NEW.status AND event_kind IN ('evidence_conflict','conflict_resolution'))"
        ")) BEGIN SELECT RAISE(ABORT, 'core invoice lifecycle requires a matching audited event'); END"
    )
    connection.execute(
        "CREATE TRIGGER core_invoices_identity_immutable BEFORE UPDATE OF invoice_id,created_by,created_at,idempotency_key ON core_invoices "
        "BEGIN SELECT RAISE(ABORT, 'core invoice identity is immutable'); END"
    )


def _migration_004(connection: sqlite3.Connection) -> None:
    """P4 local workflow extension; P3 remains membership/provenance once referenced."""
    connection.execute("CREATE TABLE core_invoice_documents (invoice_id TEXT PRIMARY KEY NOT NULL, base_invoice_id TEXT NOT NULL REFERENCES core_invoices(invoice_id), customer_code TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('temporary','real','cancelled')), permanent_number TEXT UNIQUE, created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, version INTEGER NOT NULL CHECK(version >= 0), idempotency_key TEXT NOT NULL UNIQUE, CHECK((state='temporary' AND permanent_number IS NULL) OR (state='real' AND length(trim(permanent_number))>0) OR state='cancelled'))")
    connection.execute("CREATE TABLE core_invoice_document_lines (document_line_id TEXT PRIMARY KEY NOT NULL, invoice_id TEXT NOT NULL REFERENCES core_invoice_documents(invoice_id) DEFERRABLE INITIALLY DEFERRED, core_invoice_line_id TEXT NOT NULL REFERENCES core_invoice_lines(invoice_line_id), UNIQUE(invoice_id,core_invoice_line_id))")
    connection.execute("CREATE TABLE core_invoice_price_events (event_id TEXT PRIMARY KEY NOT NULL, invoice_id TEXT NOT NULL REFERENCES core_invoice_documents(invoice_id) DEFERRABLE INITIALLY DEFERRED, document_line_id TEXT NOT NULL REFERENCES core_invoice_document_lines(document_line_id), event_kind TEXT NOT NULL CHECK(event_kind IN ('customer_code_price','temporary_override')), unit_price TEXT, customer_code TEXT NOT NULL, actor TEXT NOT NULL, expected_version INTEGER NOT NULL CHECK(expected_version >= 0), price_sequence INTEGER NOT NULL DEFAULT 1 CHECK(price_sequence > 0), idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(document_line_id,price_sequence), CHECK(unit_price IS NULL OR (typeof(unit_price)='text' AND unit_price GLOB '[0-9]*' AND unit_price NOT GLOB '*[^0-9.]*' AND unit_price NOT GLOB '.*' AND unit_price NOT GLOB '*.' AND unit_price NOT GLOB '*.*.*' AND CAST(unit_price AS REAL)>0)))")
    connection.execute("CREATE TABLE core_invoice_document_events (event_id TEXT PRIMARY KEY NOT NULL, invoice_id TEXT NOT NULL REFERENCES core_invoice_documents(invoice_id) DEFERRABLE INITIALLY DEFERRED, event_kind TEXT NOT NULL CHECK(event_kind IN ('creation','real_confirmation','cancellation')), actor TEXT NOT NULL, expected_version INTEGER NOT NULL CHECK(expected_version >= 0), idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    connection.execute("CREATE TABLE core_invoice_corrections (correction_invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES core_invoice_documents(invoice_id) DEFERRABLE INITIALLY DEFERRED, original_invoice_id TEXT NOT NULL REFERENCES core_invoice_documents(invoice_id), created_by TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, idempotency_key TEXT NOT NULL UNIQUE, CHECK(correction_invoice_id<>original_invoice_id))")
    connection.execute("CREATE UNIQUE INDEX core_invoice_documents_one_active_base ON core_invoice_documents(base_invoice_id) WHERE state<>'cancelled'")
    connection.execute("CREATE UNIQUE INDEX core_invoice_corrections_one_child ON core_invoice_corrections(original_invoice_id)")
    connection.execute("CREATE INDEX core_invoice_documents_state_created ON core_invoice_documents(state,created_at)")
    connection.execute("CREATE INDEX core_invoice_documents_customer_created ON core_invoice_documents(customer_code,created_at)")
    for table in ('core_invoice_document_lines','core_invoice_price_events','core_invoice_document_events','core_invoice_corrections'):
        connection.execute(f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END")
        connection.execute(f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")
    connection.execute("CREATE TRIGGER core_invoice_documents_no_delete BEFORE DELETE ON core_invoice_documents BEGIN SELECT RAISE(ABORT, 'invoice documents are append-only'); END")
    connection.execute("CREATE TRIGGER core_invoice_documents_identity_immutable BEFORE UPDATE OF invoice_id,base_invoice_id,customer_code,created_by,created_at,idempotency_key ON core_invoice_documents BEGIN SELECT RAISE(ABORT, 'invoice document identity is immutable'); END")
    connection.execute("CREATE TRIGGER core_invoice_documents_permanent_number_guard BEFORE UPDATE OF permanent_number ON core_invoice_documents WHEN NOT (OLD.state='temporary' AND NEW.state='real' AND OLD.permanent_number IS NULL AND NEW.permanent_number IS NOT NULL AND length(trim(NEW.permanent_number))>0) BEGIN SELECT RAISE(ABORT, 'invoice permanent number is immutable'); END")
    connection.execute("CREATE TRIGGER core_invoice_document_lines_guard BEFORE INSERT ON core_invoice_document_lines WHEN NOT EXISTS (SELECT 1 FROM core_invoice_lines WHERE invoice_line_id=NEW.core_invoice_line_id) BEGIN SELECT RAISE(ABORT, 'invoice document line membership is invalid'); END")
    connection.execute("CREATE TRIGGER core_invoice_price_events_guard BEFORE INSERT ON core_invoice_price_events WHEN NOT EXISTS (SELECT 1 FROM core_invoice_document_lines WHERE document_line_id=NEW.document_line_id AND invoice_id=NEW.invoice_id) OR (NEW.event_kind='customer_code_price' AND (NEW.expected_version<>0 OR NEW.price_sequence<>1 OR EXISTS (SELECT 1 FROM core_invoice_documents WHERE invoice_id=NEW.invoice_id))) OR (NEW.event_kind='temporary_override' AND (NEW.unit_price IS NULL OR NEW.price_sequence<>NEW.expected_version+1 OR EXISTS (SELECT 1 FROM core_invoice_price_events WHERE invoice_id=NEW.invoice_id AND event_kind='temporary_override' AND expected_version=NEW.expected_version) OR NOT EXISTS (SELECT 1 FROM core_invoice_documents WHERE invoice_id=NEW.invoice_id AND state='temporary' AND customer_code=NEW.customer_code AND version=NEW.expected_version))) BEGIN SELECT RAISE(ABORT, 'invoice price event is invalid'); END")
    connection.execute("CREATE TRIGGER core_invoice_document_events_guard BEFORE INSERT ON core_invoice_document_events WHEN (NEW.event_kind='creation' AND EXISTS (SELECT 1 FROM core_invoice_documents WHERE invoice_id=NEW.invoice_id)) OR (NEW.event_kind<>'creation' AND NOT EXISTS (SELECT 1 FROM core_invoice_documents WHERE invoice_id=NEW.invoice_id)) BEGIN SELECT RAISE(ABORT, 'invoice document event is invalid'); END")
    connection.execute("CREATE TRIGGER core_invoice_documents_creation_guard BEFORE INSERT ON core_invoice_documents WHEN NEW.state<>'temporary' OR NEW.permanent_number IS NOT NULL OR NEW.version<>1 OR NOT EXISTS (SELECT 1 FROM core_invoice_document_events WHERE invoice_id=NEW.invoice_id AND event_kind='creation' AND actor=NEW.created_by AND expected_version=0 AND idempotency_key=NEW.idempotency_key) OR (EXISTS (SELECT 1 FROM core_invoice_documents cancelled WHERE cancelled.base_invoice_id=NEW.base_invoice_id AND cancelled.state='cancelled') AND NOT EXISTS (SELECT 1 FROM core_invoice_corrections correction JOIN core_invoice_documents original ON original.invoice_id=correction.original_invoice_id WHERE correction.correction_invoice_id=NEW.invoice_id AND original.base_invoice_id=NEW.base_invoice_id AND original.state='cancelled')) OR EXISTS (SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id EXCEPT SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id=NEW.base_invoice_id) OR EXISTS (SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id=NEW.base_invoice_id EXCEPT SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id) OR EXISTS (SELECT document_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id EXCEPT SELECT document_line_id FROM core_invoice_price_events WHERE invoice_id=NEW.invoice_id AND event_kind='customer_code_price' AND expected_version=0 AND price_sequence=1 AND customer_code=NEW.customer_code) OR EXISTS (SELECT document_line_id FROM core_invoice_price_events WHERE invoice_id=NEW.invoice_id AND event_kind='customer_code_price' EXCEPT SELECT document_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id) BEGIN SELECT RAISE(ABORT, 'invoice document creation requires matching audited event, correction link, complete base membership, and initial prices'); END")
    connection.execute("CREATE TRIGGER core_invoice_documents_lifecycle_guard BEFORE UPDATE OF state,version ON core_invoice_documents WHEN NEW.version<>OLD.version+1 OR NOT (EXISTS (SELECT 1 FROM core_invoice_document_events WHERE invoice_id=OLD.invoice_id AND expected_version=OLD.version AND ((OLD.state='temporary' AND NEW.state='real' AND event_kind='real_confirmation') OR (OLD.state IN ('temporary','real') AND NEW.state='cancelled' AND event_kind='cancellation'))) OR (OLD.state='temporary' AND NEW.state='temporary' AND EXISTS (SELECT 1 FROM core_invoice_price_events WHERE invoice_id=OLD.invoice_id AND expected_version=OLD.version AND event_kind='temporary_override' AND price_sequence=OLD.version+1))) OR (NEW.state='real' AND (NEW.permanent_number IS NULL OR length(trim(NEW.permanent_number))=0 OR EXISTS (SELECT 1 FROM core_invoice_document_lines l WHERE l.invoice_id=OLD.invoice_id AND NOT EXISTS (SELECT 1 FROM core_invoice_price_events p WHERE p.document_line_id=l.document_line_id AND p.unit_price IS NOT NULL AND CAST(p.unit_price AS REAL)>0 AND p.price_sequence=(SELECT MAX(p2.price_sequence) FROM core_invoice_price_events p2 WHERE p2.document_line_id=l.document_line_id))))) BEGIN SELECT RAISE(ABORT, 'invoice document lifecycle requires matching event and positive prices'); END")
    connection.execute("CREATE TRIGGER core_invoice_corrections_guard BEFORE INSERT ON core_invoice_corrections WHEN NOT EXISTS (SELECT 1 FROM core_invoice_documents WHERE invoice_id=NEW.original_invoice_id AND state='cancelled') OR (EXISTS (SELECT 1 FROM core_invoice_documents WHERE invoice_id=NEW.correction_invoice_id) AND (NOT EXISTS (SELECT 1 FROM core_invoice_documents WHERE invoice_id=NEW.correction_invoice_id AND state='temporary') OR EXISTS (SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.correction_invoice_id EXCEPT SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.original_invoice_id) OR EXISTS (SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.original_invoice_id EXCEPT SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.correction_invoice_id))) BEGIN SELECT RAISE(ABORT, 'invoice correction linkage is invalid'); END")
    connection.execute("CREATE TRIGGER core_invoice_events_p4_lifecycle_guard BEFORE INSERT ON core_invoice_events WHEN NEW.event_kind IN ('review_submission','approval','cancellation') AND EXISTS (SELECT 1 FROM core_invoice_documents WHERE base_invoice_id=NEW.invoice_id) BEGIN SELECT RAISE(ABORT, 'P4 document owns invoice lifecycle'); END")
    connection.execute("CREATE TRIGGER core_invoices_p4_lifecycle_guard BEFORE UPDATE OF status,version ON core_invoices WHEN NEW.status<>OLD.status AND EXISTS (SELECT 1 FROM core_invoice_documents WHERE base_invoice_id=OLD.invoice_id) BEGIN SELECT RAISE(ABORT, 'P4 document owns invoice lifecycle'); END")


def _migration_005(connection: sqlite3.Connection) -> None:
    """P5 durable temporary references and immutable invoice shipment context."""
    connection.execute("CREATE TABLE core_invoice_reference_counters (customer_code TEXT NOT NULL, reference_year INTEGER NOT NULL CHECK(reference_year BETWEEN 2000 AND 2099), next_sequence INTEGER NOT NULL CHECK(next_sequence BETWEEN 1 AND 1000), PRIMARY KEY(customer_code,reference_year))")
    connection.execute("INSERT INTO core_invoice_reference_counters(customer_code,reference_year,next_sequence) VALUES ('__P5_LEDGER__',2000,1)")
    connection.execute("CREATE TABLE core_invoice_reference_allocations (invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES core_invoice_documents(invoice_id) DEFERRABLE INITIALLY DEFERRED, allocation_event_id TEXT NOT NULL UNIQUE REFERENCES core_invoice_document_events(event_id) DEFERRABLE INITIALLY DEFERRED, customer_code TEXT NOT NULL, reference_year INTEGER NOT NULL CHECK(reference_year BETWEEN 2000 AND 2099), reference_sequence INTEGER NOT NULL CHECK(reference_sequence BETWEEN 1 AND 999), temporary_reference TEXT NOT NULL UNIQUE, UNIQUE(customer_code,reference_year,reference_sequence), CHECK(temporary_reference=customer_code || '-T' || printf('%02d',reference_year % 100) || '-' || printf('%03d',reference_sequence)))")
    connection.execute("CREATE TABLE core_invoice_document_context (invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES core_invoice_documents(invoice_id) DEFERRABLE INITIALLY DEFERRED, customer_code TEXT NOT NULL, reference_year INTEGER NOT NULL CHECK(reference_year BETWEEN 2000 AND 2099), reference_sequence INTEGER NOT NULL CHECK(reference_sequence BETWEEN 1 AND 999), temporary_reference TEXT NOT NULL UNIQUE, consignee TEXT NOT NULL CHECK(length(trim(consignee))>0), delivery_reference TEXT NOT NULL CHECK(length(trim(delivery_reference))>0), UNIQUE(customer_code,reference_year,reference_sequence), CHECK(temporary_reference=customer_code || '-T' || printf('%02d',reference_year % 100) || '-' || printf('%03d',reference_sequence)))")
    connection.execute("CREATE INDEX core_invoice_document_context_reference ON core_invoice_document_context(temporary_reference)")
    connection.execute("CREATE INDEX core_invoice_document_context_consignee ON core_invoice_document_context(consignee)")
    connection.execute("CREATE INDEX core_invoice_document_context_delivery ON core_invoice_document_context(delivery_reference)")
    connection.execute("CREATE TRIGGER core_invoice_document_context_no_reinsert BEFORE INSERT ON core_invoice_document_context WHEN EXISTS (SELECT 1 FROM core_invoice_document_context WHERE invoice_id=NEW.invoice_id) BEGIN SELECT RAISE(ABORT, 'invoice document context is immutable'); END")
    connection.execute("CREATE TRIGGER core_invoice_document_context_no_update BEFORE UPDATE ON core_invoice_document_context BEGIN SELECT RAISE(ABORT, 'invoice document context is immutable'); END")
    connection.execute("CREATE TRIGGER core_invoice_document_context_no_delete BEFORE DELETE ON core_invoice_document_context BEGIN SELECT RAISE(ABORT, 'invoice document context is append-only'); END")
    connection.execute("CREATE TRIGGER core_invoice_reference_allocations_no_reinsert BEFORE INSERT ON core_invoice_reference_allocations WHEN EXISTS (SELECT 1 FROM core_invoice_reference_allocations WHERE invoice_id=NEW.invoice_id) BEGIN SELECT RAISE(ABORT, 'invoice reference allocations are immutable'); END")
    connection.execute("CREATE TRIGGER core_invoice_reference_allocations_no_update BEFORE UPDATE ON core_invoice_reference_allocations BEGIN SELECT RAISE(ABORT, 'invoice reference allocations are immutable'); END")
    connection.execute("CREATE TRIGGER core_invoice_reference_allocations_no_delete BEFORE DELETE ON core_invoice_reference_allocations BEGIN SELECT RAISE(ABORT, 'invoice reference allocations are append-only'); END")
    connection.execute("CREATE TRIGGER core_invoice_reference_allocations_guard BEFORE INSERT ON core_invoice_reference_allocations WHEN NOT EXISTS (SELECT 1 FROM core_invoice_document_events WHERE event_id=NEW.allocation_event_id AND invoice_id=NEW.invoice_id AND event_kind='creation') OR NEW.reference_sequence<>(SELECT COALESCE(MAX(reference_sequence),0)+1 FROM core_invoice_reference_allocations WHERE customer_code=NEW.customer_code AND reference_year=NEW.reference_year) BEGIN SELECT RAISE(ABORT, 'invoice reference allocation sequence is not the next available number'); END")
    connection.execute("CREATE TRIGGER core_invoice_document_context_allocation_guard BEFORE INSERT ON core_invoice_document_context WHEN NOT EXISTS (SELECT 1 FROM core_invoice_reference_allocations WHERE invoice_id=NEW.invoice_id AND customer_code=NEW.customer_code AND reference_year=NEW.reference_year AND reference_sequence=NEW.reference_sequence AND temporary_reference=NEW.temporary_reference) BEGIN SELECT RAISE(ABORT, 'invoice document context requires an official reference allocation'); END")
    connection.execute("CREATE TRIGGER core_invoice_reference_counters_no_insert BEFORE INSERT ON core_invoice_reference_counters BEGIN SELECT RAISE(ABORT, 'invoice reference counters are ledger-owned'); END")
    connection.execute("CREATE TRIGGER core_invoice_reference_counters_no_update BEFORE UPDATE ON core_invoice_reference_counters BEGIN SELECT RAISE(ABORT, 'invoice reference counters are immutable'); END")
    connection.execute("CREATE TRIGGER core_invoice_reference_counters_no_delete BEFORE DELETE ON core_invoice_reference_counters BEGIN SELECT RAISE(ABORT, 'invoice reference counters are append-only'); END")
    connection.execute("DROP TRIGGER core_invoice_documents_creation_guard")
    connection.execute("CREATE TRIGGER core_invoice_documents_creation_guard BEFORE INSERT ON core_invoice_documents WHEN NEW.state<>'temporary' OR NEW.permanent_number IS NOT NULL OR NEW.version<>1 OR NOT EXISTS (SELECT 1 FROM core_invoice_document_events WHERE invoice_id=NEW.invoice_id AND event_kind='creation' AND actor=NEW.created_by AND expected_version=0 AND idempotency_key=NEW.idempotency_key) OR NOT EXISTS (SELECT 1 FROM core_invoice_document_context WHERE invoice_id=NEW.invoice_id AND customer_code=NEW.customer_code) OR NOT EXISTS (SELECT 1 FROM core_invoice_reference_allocations allocation JOIN core_invoice_document_events creation_event ON creation_event.event_id=allocation.allocation_event_id WHERE allocation.invoice_id=NEW.invoice_id AND allocation.customer_code=NEW.customer_code AND creation_event.invoice_id=NEW.invoice_id AND creation_event.event_kind='creation' AND creation_event.actor=NEW.created_by AND creation_event.expected_version=0 AND creation_event.idempotency_key=NEW.idempotency_key) OR (EXISTS (SELECT 1 FROM core_invoice_documents cancelled WHERE cancelled.base_invoice_id=NEW.base_invoice_id AND cancelled.state='cancelled') AND NOT EXISTS (SELECT 1 FROM core_invoice_corrections correction JOIN core_invoice_documents original ON original.invoice_id=correction.original_invoice_id WHERE correction.correction_invoice_id=NEW.invoice_id AND original.base_invoice_id=NEW.base_invoice_id AND original.state='cancelled')) OR EXISTS (SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id EXCEPT SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id=NEW.base_invoice_id) OR EXISTS (SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id=NEW.base_invoice_id EXCEPT SELECT core_invoice_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id) OR EXISTS (SELECT document_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id EXCEPT SELECT document_line_id FROM core_invoice_price_events WHERE invoice_id=NEW.invoice_id AND event_kind='customer_code_price' AND expected_version=0 AND price_sequence=1 AND customer_code=NEW.customer_code) OR EXISTS (SELECT document_line_id FROM core_invoice_price_events WHERE invoice_id=NEW.invoice_id AND event_kind='customer_code_price' EXCEPT SELECT document_line_id FROM core_invoice_document_lines WHERE invoice_id=NEW.invoice_id) BEGIN SELECT RAISE(ABORT, 'invoice document creation requires matching audited event, official reference allocation, correction link, complete base membership, and initial prices'); END")


def apply_core_provenance_migrations(database_path: Path) -> int:
    """Apply the explicit, versioned P1/P2 foundation migrations to a Core DB."""
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


def apply_core_invoice_migrations(database_path: Path) -> int:
    """Apply explicit migration 003 to an already-versioned local Core DB."""
    apply_core_provenance_migrations(database_path)
    database_path = Path(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        applied = {row[0] for row in connection.execute("SELECT version FROM core_schema_migrations")}
        if 3 not in applied:
            _migration_003(connection)
            connection.execute("INSERT INTO core_schema_migrations(version) VALUES (?)", (3,))
    return 3


def apply_core_invoice_workflow_migrations(database_path: Path) -> int:
    """Explicitly apply P4 workflow migration 004 after the preserved P3a path."""
    apply_core_invoice_migrations(database_path)
    database_path = Path(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        applied = {row[0] for row in connection.execute("SELECT version FROM core_schema_migrations")}
        if 4 not in applied:
            _migration_004(connection)
            connection.execute("INSERT INTO core_schema_migrations(version) VALUES (4)")
        if 5 not in applied:
            if connection.execute("SELECT 1 FROM core_invoice_documents LIMIT 1").fetchone() is not None:
                raise CoreProvenanceError("P5 migration requires reset: populated P4 invoice documents have no P5 shipment context")
            _migration_005(connection)
            connection.execute("INSERT INTO core_schema_migrations(version) VALUES (5)")
    return _INVOICE_SCHEMA_VERSION


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
