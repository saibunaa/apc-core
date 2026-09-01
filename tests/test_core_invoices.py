import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path


class CoreOwnedInvoiceLifecycleTests(unittest.TestCase):
    SNAPSHOT_A = "a" * 64
    SNAPSHOT_B = "b" * 64
    TABLE = "MainDB__ORDER_ITEM"

    def _store(self, root: Path):
        from apc_core.core_invoices import CoreInvoiceStore
        from apc_core.core_orders import CoreOrderStore
        from apc_core.core_provenance import apply_core_invoice_migrations

        database = root / "core.sqlite"
        apply_core_invoice_migrations(database)
        with sqlite3.connect(database) as connection:
            for snapshot, imported_at in (
                (self.SNAPSHOT_A, "2026-09-01T00:00:00Z"),
                (self.SNAPSHOT_B, "2026-09-01T00:01:00Z"),
            ):
                connection.execute(
                    "INSERT INTO core_source_snapshots(snapshot_sha256,artifact_path,imported_at) VALUES (?,?,?)",
                    (snapshot, f"/fixture/{snapshot}.sqlite", imported_at),
                )
            for rowid in (7, 8):
                connection.execute(
                    "INSERT INTO core_source_rows("
                    "snapshot_sha256,source_table,source_rowid,source_kind,document_id,line_label,item_id,quantity,evidence_json"
                    ") VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.SNAPSHOT_A, self.TABLE, rowid, "order_item", "ORD//2026/001", str(rowid), f"ITEM-{rowid}", "2", "{}"),
                )
            connection.execute(
                "INSERT INTO core_source_rows("
                "snapshot_sha256,source_table,source_rowid,source_kind,document_id,line_label,item_id,quantity,evidence_json"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (self.SNAPSHOT_B, self.TABLE, 7, "order_item", "OTHER", "7", "OTHER-7", "2", "{}"),
            )
        order_store = CoreOrderStore(database)
        try:
            order_store.create_order(
                "order-1", "WAT", "order-create-1",
                [
                    {"line_id": "line-7", "snapshot_sha256": self.SNAPSHOT_A, "source_table": self.TABLE, "source_rowid": 7},
                    {"line_id": "line-8", "snapshot_sha256": self.SNAPSHOT_A, "source_table": self.TABLE, "source_rowid": 8},
                ],
            )
        finally:
            order_store.close()
        return database, CoreInvoiceStore(database)

    def _create(self, store, *, invoice_id="invoice-1", key="invoice-create-1", line_ids=None):
        return store.create_invoice(
            invoice_id, "WAT", key, ["line-7", "line-8"] if line_ids is None else line_ids, expected_version=0
        )

    def test_constructor_is_inert_and_rejects_missing_or_p2_only_database(self):
        from apc_core.core_invoices import CoreInvoiceError, CoreInvoiceStore
        from apc_core.core_provenance import apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.sqlite"
            with self.assertRaisesRegex(CoreInvoiceError, "missing"):
                CoreInvoiceStore(missing)
            self.assertFalse(missing.exists())

            database = root / "p2.sqlite"
            apply_core_provenance_migrations(database)
            with self.assertRaisesRegex(CoreInvoiceError, "migrations"):
                CoreInvoiceStore(database)
            with sqlite3.connect(database) as connection:
                self.assertFalse(any("invoice" in row[0] for row in connection.execute("SELECT name FROM sqlite_master")))

    def test_migration_003_upgrades_p2_without_rewriting_p1_or_p2_evidence(self):
        from apc_core.core_orders import CoreOrderStore
        from apc_core.core_provenance import apply_core_invoice_migrations, apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "p2.sqlite"
            apply_core_provenance_migrations(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO core_source_snapshots(snapshot_sha256,artifact_path,imported_at) VALUES (?,?,?)",
                    (self.SNAPSHOT_A, "/fixture/a.sqlite", "2026-09-01T00:00:00Z"),
                )
                connection.execute(
                    "INSERT INTO core_source_rows("
                    "snapshot_sha256,source_table,source_rowid,source_kind,document_id,line_label,item_id,quantity,evidence_json"
                    ") VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.SNAPSHOT_A, self.TABLE, 7, "order_item", "ORD", "7", "ITEM-7", "2", "{}"),
                )
            order_store = CoreOrderStore(database)
            try:
                order_store.create_order(
                    "order-1", "WAT", "order-create-1",
                    [{"line_id": "line-7", "snapshot_sha256": self.SNAPSHOT_A, "source_table": self.TABLE, "source_rowid": 7}],
                )
            finally:
                order_store.close()
            with sqlite3.connect(database) as connection:
                before = connection.execute(
                    "SELECT snapshot_sha256,source_table,source_rowid,evidence_json FROM core_source_rows"
                ).fetchall()
                order_before = connection.execute("SELECT order_id,idempotency_key FROM core_orders").fetchall()
            self.assertEqual(3, apply_core_invoice_migrations(database))
            self.assertEqual(3, apply_core_invoice_migrations(database))
            with sqlite3.connect(database) as connection:
                self.assertEqual([(1,), (2,), (3,)], connection.execute("SELECT version FROM core_schema_migrations ORDER BY version").fetchall())
                self.assertEqual(before, connection.execute("SELECT snapshot_sha256,source_table,source_rowid,evidence_json FROM core_source_rows").fetchall())
                self.assertEqual(order_before, connection.execute("SELECT order_id,idempotency_key FROM core_orders").fetchall())
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"core_invoices", "core_invoice_lines", "core_invoice_events", "core_invoice_conflicts"}.issubset(tables))

    def test_invoice_membership_is_explicit_whole_order_lines_only_and_idempotent(self):
        from apc_core.core_invoices import CoreInvoiceError

        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._store(Path(temporary))
            try:
                first = self._create(store)
                self.assertEqual(first, self._create(store))
                self.assertEqual({"invoice_id": "invoice-1", "version": 1, "status": "draft"}, first)
                self.assertEqual(["line-7", "line-8"], [row["order_line_id"] for row in store.invoice_lines("invoice-1")])
                with self.assertRaisesRegex(CoreInvoiceError, "membership"):
                    self._create(store, invoice_id="invoice-2", key="duplicate-lines", line_ids=["line-7", "line-7"])
                with self.assertRaisesRegex(CoreInvoiceError, "unknown"):
                    self._create(store, invoice_id="invoice-2", key="unknown-line", line_ids=["missing"])
                with self.assertRaisesRegex(CoreInvoiceError, "selected"):
                    self._create(store, invoice_id="invoice-2", key="reused-line", line_ids=["line-7"])
                with self.assertRaisesRegex(CoreInvoiceError, "idempotency"):
                    self._create(store, invoice_id="invoice-2", key="invoice-create-1", line_ids=["line-8"])
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM core_invoice_lines").fetchone()[0])

    def test_creation_event_persists_the_command_idempotency_key_and_invoice_identity_conflict_is_closed(self):
        from apc_core.core_invoices import CoreInvoiceError

        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._store(Path(temporary))
            try:
                self._create(store)
                with self.assertRaisesRegex(CoreInvoiceError, "invoice identity conflicts"):
                    self._create(store, invoice_id="invoice-1", key="different-create-key", line_ids=["line-7"])
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                creation = connection.execute(
                    "SELECT idempotency_key,expected_version,actor FROM core_invoice_events WHERE event_kind='creation'"
                ).fetchone()
                self.assertEqual(("invoice-create-1", 0, "WAT"), creation)
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM core_invoices").fetchone()[0])
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM core_invoice_lines").fetchone()[0])

    def test_lifecycle_requires_expected_versions_and_changed_version_replays_fail_closed(self):
        from apc_core.core_invoices import CoreInvoiceError

        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._store(Path(temporary))
            try:
                self._create(store)
                review = store.submit_for_review("review-event", "invoice-1", "YIM", "review-key", expected_version=1)
                self.assertEqual({"invoice_id": "invoice-1", "version": 2, "status": "review"}, review)
                self.assertEqual(review, store.submit_for_review("review-event", "invoice-1", "YIM", "review-key", expected_version=1))
                with self.assertRaisesRegex(CoreInvoiceError, "idempotency"):
                    store.submit_for_review("review-event", "invoice-1", "YIM", "review-key", expected_version=2)
                with self.assertRaisesRegex(CoreInvoiceError, "version"):
                    store.approve("approval-event", "invoice-1", "SAI", "approval-key", expected_version=1)
                approved = store.approve("approval-event", "invoice-1", "SAI", "approval-key", expected_version=2)
                self.assertEqual({"invoice_id": "invoice-1", "version": 3, "status": "approved"}, approved)
                with self.assertRaisesRegex(CoreInvoiceError, "idempotency"):
                    store.approve("approval-event", "invoice-1", "SAI", "approval-key", expected_version=3)
                cancelled = store.cancel("cancel-event", "invoice-1", "SAI", "cancel-key", expected_version=3)
                self.assertEqual({"invoice_id": "invoice-1", "version": 4, "status": "cancelled"}, cancelled)
                with self.assertRaisesRegex(CoreInvoiceError, "status"):
                    store.submit_for_review("late-review", "invoice-1", "YIM", "late-key", expected_version=4)
            finally:
                store.close()

    def test_direct_sql_rejects_invalid_status_fks_and_invoice_line_event_mutation_or_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._store(Path(temporary))
            try:
                self._create(store)
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE core_invoices SET status='issued' WHERE invoice_id='invoice-1'")
                connection.create_function("core_invoice_lifecycle_authorized", 0, lambda: 1)
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE core_invoices SET status='review',version=1 WHERE invoice_id='invoice-1'")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("INSERT INTO core_invoice_lines(invoice_line_id,invoice_id,order_line_id) VALUES ('bad','missing','line-7')")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE core_invoice_lines SET order_line_id='line-8' WHERE invoice_id='invoice-1'")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("DELETE FROM core_invoice_lines WHERE invoice_id='invoice-1'")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE core_invoice_events SET actor='other' WHERE event_kind='creation'")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("DELETE FROM core_invoice_events WHERE event_kind='creation'")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) "
                        "VALUES ('un-evidenced-conflict','invoice-1','evidence_conflict','YIM',1,'un-evidenced-conflict-key')"
                    )

    def test_direct_sql_rejects_invoice_creation_without_matching_creation_event_or_membership(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._store(Path(temporary))
            store.close()
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoices(invoice_id,created_by,status,version,idempotency_key) "
                        "VALUES ('un-audited','WAT','draft',1,'un-audited-key')"
                    )
                connection.execute(
                    "INSERT INTO core_invoice_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) "
                    "VALUES ('no-lines:creation','no-lines','creation','WAT',0,'no-lines-key')"
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoices(invoice_id,created_by,status,version,idempotency_key) "
                        "VALUES ('no-lines','WAT','draft',1,'no-lines-key')"
                    )

    def test_direct_sql_rejects_invalid_snapshot_conflict_timestamp_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._store(Path(temporary))
            try:
                self._create(store)
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                for conflict_id, source_at, imported_at in (
                    ('bad-conflict-malformed', 'not-a-timestamp', '2026-09-01T00:01:00Z'),
                    ('bad-conflict-naive', '2026-09-01T00:00:00', '2026-09-01T00:01:00Z'),
                    ('bad-conflict-equal', '2026-09-01T00:01:00Z', '2026-09-01T00:01:00Z'),
                    ('bad-conflict-reverse', '2026-09-01T00:02:00Z', '2026-09-01T00:01:00Z'),
                ):
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(
                            "INSERT INTO core_invoice_conflicts("
                            "conflict_id,invoice_id,source_snapshot_sha256,imported_snapshot_sha256,comparison_rule,"
                            "source_imported_at,imported_snapshot_imported_at"
                            ") VALUES (?,?,?,?,?,?,?)",
                            (
                                conflict_id, 'invoice-1', self.SNAPSHOT_A, self.SNAPSHOT_B,
                                'core_imported_at_strictly_later', source_at, imported_at,
                            ),
                        )

    def test_distinct_imported_snapshot_records_conflict_without_remapping_membership(self):
        from apc_core.core_invoices import CoreInvoiceError

        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._store(Path(temporary))
            try:
                self._create(store)
                with self.assertRaisesRegex(CoreInvoiceError, "freshness"):
                    store.record_evidence_conflict(
                        "conflict-1", "invoice-1", self.SNAPSHOT_A, self.SNAPSHOT_B, "YIM", "conflict-key", expected_version=1
                    )
                self.assertEqual(1, store.connection.execute("SELECT version FROM core_invoices WHERE invoice_id='invoice-1'").fetchone()[0])
                self.assertEqual(0, store.connection.execute("SELECT COUNT(*) FROM core_invoice_conflicts").fetchone()[0])
                self.assertEqual(0, store.connection.execute("SELECT COUNT(*) FROM core_invoice_events WHERE event_kind='evidence_conflict'").fetchone()[0])
                conflict = store.record_evidence_conflict(
                    "conflict-1", "invoice-1", self.SNAPSHOT_A, self.SNAPSHOT_B, "YIM", "conflict-key", expected_version=1,
                    comparison_rule="core_imported_at_strictly_later",
                )
                self.assertEqual({"invoice_id": "invoice-1", "version": 2, "status": "draft", "conflict_id": "conflict-1"}, conflict)
                self.assertEqual(conflict, store.record_evidence_conflict(
                    "conflict-1", "invoice-1", self.SNAPSHOT_A, self.SNAPSHOT_B, "YIM", "conflict-key", expected_version=1,
                    comparison_rule="core_imported_at_strictly_later",
                ))
                self.assertEqual(["line-7", "line-8"], [row["order_line_id"] for row in store.invoice_lines("invoice-1")])
                with self.assertRaisesRegex(CoreInvoiceError, "distinct"):
                    store.record_evidence_conflict("conflict-2", "invoice-1", self.SNAPSHOT_A, self.SNAPSHOT_A, "YIM", "same-key", expected_version=2)
                with self.assertRaisesRegex(CoreInvoiceError, "membership"):
                    store.record_evidence_conflict("conflict-3", "invoice-1", self.SNAPSHOT_B, self.SNAPSHOT_A, "YIM", "wrong-source-key", expected_version=2)
                resolution = store.resolve_conflict("resolution-event", "conflict-1", "SAI", "resolution-key", expected_version=2)
                self.assertEqual({"invoice_id": "invoice-1", "version": 3, "status": "draft", "conflict_id": "conflict-1"}, resolution)
            finally:
                store.close()

    def test_migration_emits_only_core_namespace_no_money_or_legacy_fields(self):
        import apc_core.core_provenance as provenance

        migration_source = inspect.getsource(provenance._migration_003).lower()
        for forbidden in ("legacy", "awb", "price", "tax", "currency", "discount", "total", "number", "accounting", "print"):
            self.assertNotIn(forbidden, migration_source)
        self.assertIn("core_invoices", migration_source)
        self.assertIn("core_invoice_lines", migration_source)


class CoreInvoiceWorkflowTests(unittest.TestCase):
    SNAPSHOT = "c" * 64
    TABLE = "MainDB__ORDER_ITEM"

    def _workflow(self, root: Path):
        from apc_core.core_invoices import CoreInvoiceStore, CoreInvoiceWorkflowStore
        from apc_core.core_orders import CoreOrderStore
        from apc_core.core_provenance import apply_core_invoice_workflow_migrations

        database = root / "workflow.sqlite"
        self.assertEqual(5, apply_core_invoice_workflow_migrations(database))
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO core_source_snapshots(snapshot_sha256,artifact_path,imported_at) VALUES (?,?,?)", (self.SNAPSHOT, "/fixture/c.sqlite", "2026-09-01T00:00:00Z"))
            for rowid in (1, 2):
                connection.execute("INSERT INTO core_source_rows(snapshot_sha256,source_table,source_rowid,source_kind,document_id,line_label,item_id,quantity,evidence_json) VALUES (?,?,?,?,?,?,?,?,?)", (self.SNAPSHOT, self.TABLE, rowid, "order_item", "ORD", str(rowid), f"ITEM-{rowid}", "1", "{}"))
        orders = CoreOrderStore(database)
        try:
            orders.create_order("order-p4", "creator", "order-p4-key", [{"line_id": "p4-line-1", "snapshot_sha256": self.SNAPSHOT, "source_table": self.TABLE, "source_rowid": 1}, {"line_id": "p4-line-2", "snapshot_sha256": self.SNAPSHOT, "source_table": self.TABLE, "source_rowid": 2}])
        finally:
            orders.close()
        legacy = CoreInvoiceStore(database)
        try:
            legacy.create_invoice("base-p4", "creator", "base-p4-key", ["p4-line-1", "p4-line-2"], expected_version=0)
        finally:
            legacy.close()
        return database, CoreInvoiceWorkflowStore(database)

    def test_migration_004_is_explicit_idempotent_and_p4_store_rejects_v3(self):
        from apc_core.core_invoices import CoreInvoiceError, CoreInvoiceWorkflowStore
        from apc_core.core_provenance import apply_core_invoice_migrations, apply_core_invoice_workflow_migrations
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "v3.sqlite"
            self.assertEqual(3, apply_core_invoice_migrations(database))
            with self.assertRaisesRegex(CoreInvoiceError, "migrations"):
                CoreInvoiceWorkflowStore(database)
            self.assertEqual(5, apply_core_invoice_workflow_migrations(database))
            self.assertEqual(5, apply_core_invoice_workflow_migrations(database))
            with sqlite3.connect(database) as connection:
                self.assertEqual([(1,), (2,), (3,), (4,), (5,)], connection.execute("SELECT version FROM core_schema_migrations ORDER BY version").fetchall())
                self.assertTrue({"core_invoice_documents", "core_invoice_document_lines", "core_invoice_price_events", "core_invoice_document_events", "core_invoice_corrections"}.issubset({row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}))

    def test_temporary_prices_real_cancellation_and_correction_are_append_only(self):
        from apc_core.core_invoices import CoreInvoiceError
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                created = store.create_temporary_invoice("doc-1", "base-p4", "any nonblank actor", "CUST-1", {"p4-line-1": "10.00", "p4-line-2": None}, "doc-1-create", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                self.assertEqual({"invoice_id": "doc-1", "state": "temporary", "version": 1, "permanent_number": None, "temporary_reference": "CUST-1-T26-001", "consignee": "Fixture consignee", "delivery_reference": "Fixture delivery"}, created)
                self.assertEqual(["doc-1"], [row["invoice_id"] for row in store.search_invoices(customer_code="CUST-1", state="temporary")])
                with self.assertRaisesRegex(CoreInvoiceError, "positive price"):
                    store.confirm_real("confirm-bad", "doc-1", "PERM-1", "actor", "confirm-bad-key", expected_version=1)
                changed = store.override_temporary_price("override-1", "doc-1", "doc-1:line:2", "12.50", "actor", "override-key", expected_version=1)
                self.assertEqual(2, changed["version"])
                real = store.confirm_real("confirm-1", "doc-1", "opaque-001", "actor", "confirm-key", expected_version=2)
                self.assertEqual({"invoice_id": "doc-1", "state": "real", "version": 3, "permanent_number": "opaque-001", "temporary_reference": "CUST-1-T26-001", "consignee": "Fixture consignee", "delivery_reference": "Fixture delivery"}, real)
                cancelled = store.cancel("cancel-1", "doc-1", "actor", "cancel-key", expected_version=3)
                self.assertEqual("cancelled", cancelled["state"])
                correction = store.create_correction_temporary("doc-2", "doc-1", "other", "CUST-1", {"p4-line-1": "11", "p4-line-2": "13"}, "doc-2-create", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                self.assertEqual("temporary", correction["state"])
                original, corrected = store.get_invoice("doc-1"), store.get_invoice("doc-2")
                self.assertEqual("cancelled", original["state"])
                self.assertEqual("doc-1", corrected["correction_of"])
                self.assertEqual(original["core_invoice_line_ids"], corrected["core_invoice_line_ids"])
                self.assertEqual(3, len(original["price_events"]))
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                for statement in ("UPDATE core_invoice_document_lines SET core_invoice_line_id='x' WHERE invoice_id='doc-1'", "DELETE FROM core_invoice_price_events WHERE invoice_id='doc-1'", "UPDATE core_invoice_document_events SET actor='x' WHERE invoice_id='doc-1'", "DELETE FROM core_invoice_corrections WHERE correction_invoice_id='doc-2'", "UPDATE core_invoice_document_context SET consignee='changed' WHERE invoice_id='doc-1'", "UPDATE core_invoice_documents SET permanent_number='changed' WHERE invoice_id='doc-1'", "UPDATE core_invoice_documents SET state='real',version=9 WHERE invoice_id='doc-2'"):
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(statement)

    def test_idempotency_and_stale_versions_fail_closed(self):
        from apc_core.core_invoices import CoreInvoiceError
        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._workflow(Path(temporary))
            try:
                first = store.create_temporary_invoice("doc-key", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "create-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                self.assertEqual(first, store.create_temporary_invoice("doc-key", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "create-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026))
                with self.assertRaisesRegex(CoreInvoiceError, "idempotency"):
                    store.create_temporary_invoice("doc-key", "base-p4", "actor", "CUST", {"p4-line-1": "9", "p4-line-2": "2"}, "create-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                store.override_temporary_price("change-key", "doc-key", "doc-key:line:1", "3", "actor", "change-command", expected_version=1)
                with self.assertRaisesRegex(CoreInvoiceError, "version"):
                    store.override_temporary_price("stale", "doc-key", "doc-key:line:2", "4", "actor", "stale-command", expected_version=1)
            finally:
                store.close()

    def test_cancelled_base_rejects_normal_successor_and_requires_correction_link(self):
        from apc_core.core_invoices import CoreInvoiceError

        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice("doc-original", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "original-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                store.cancel("cancel-original", "doc-original", "actor", "cancel-original-key", expected_version=1)
                with self.assertRaisesRegex(CoreInvoiceError, "correction"):
                    store.create_temporary_invoice("doc-unlinked", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "unlinked-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                successor = store.create_correction_temporary("doc-correction", "doc-original", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "correction-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                self.assertEqual("temporary", successor["state"])
                self.assertEqual("doc-original", store.get_invoice("doc-correction")["correction_of"])
            finally:
                store.close()

    @staticmethod
    def _direct_document(connection, invoice_id, base_invoice_id, customer_code, key, original_invoice_id=None, reference_sequence=None):
        connection.execute(
            "INSERT INTO core_invoice_document_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) "
            "VALUES (?,?, 'creation','actor',0,?)",
            (f"{invoice_id}:creation", invoice_id, key),
        )
        if original_invoice_id is not None:
            connection.execute(
                "INSERT INTO core_invoice_corrections(correction_invoice_id,original_invoice_id,created_by,idempotency_key) VALUES (?,?, 'actor',?)",
                (invoice_id, original_invoice_id, f"{key}:correction"),
            )
        lines = connection.execute(
            "SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id=? ORDER BY invoice_line_id", (base_invoice_id,)
        ).fetchall()
        for position, (core_line_id,) in enumerate(lines, 1):
            document_line_id = f"{invoice_id}:line:{position}"
            connection.execute(
                "INSERT INTO core_invoice_document_lines(document_line_id,invoice_id,core_invoice_line_id) VALUES (?,?,?)",
                (document_line_id, invoice_id, core_line_id),
            )
            connection.execute(
                "INSERT INTO core_invoice_price_events("
                "event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,idempotency_key"
                ") VALUES (?,?,?,'customer_code_price','1',?,'actor',0,?)",
                (f"{invoice_id}:price:{position}", invoice_id, document_line_id, customer_code, f"{key}:price:{position}"),
            )
        if reference_sequence is None:
            reference_sequence = connection.execute(
                "SELECT COALESCE(MAX(reference_sequence),0)+1 FROM core_invoice_reference_allocations "
                "WHERE customer_code=? AND reference_year=2026",
                (customer_code,),
            ).fetchone()[0]
        connection.execute(
            "INSERT INTO core_invoice_reference_allocations(invoice_id,allocation_event_id,customer_code,reference_year,reference_sequence,temporary_reference) "
            "VALUES (?,?,?,?,?,?)",
            (invoice_id, f"{invoice_id}:creation", customer_code, 2026, reference_sequence, f"{customer_code}-T26-{reference_sequence:03d}"),
        )
        connection.execute("INSERT INTO core_invoice_document_context(invoice_id,customer_code,reference_year,reference_sequence,temporary_reference,consignee,delivery_reference) VALUES (?,?,?,?,?,?,?)", (invoice_id, customer_code, 2026, reference_sequence, f"{customer_code}-T26-{reference_sequence:03d}", "Direct consignee", "Direct delivery"))
        connection.execute(
            "INSERT INTO core_invoice_documents(invoice_id,base_invoice_id,customer_code,state,created_by,version,idempotency_key) "
            "VALUES (?,?,?,'temporary','actor',1,?)",
            (invoice_id, base_invoice_id, customer_code, key),
        )

    def test_direct_sql_rejects_unaudited_document_and_invalid_correction_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice("doc-1", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "key-1", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("INSERT INTO core_invoice_documents(invoice_id,base_invoice_id,customer_code,state,created_by,version,idempotency_key) VALUES ('bad','base-p4','CUST','temporary','actor',1,'bad-key')")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("INSERT INTO core_invoice_corrections(correction_invoice_id,original_invoice_id,created_by,idempotency_key) VALUES ('doc-1','doc-1','actor','same-link')")

    def test_direct_sql_rejects_fully_audited_unlinked_successor_after_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice("doc-original", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "original-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                store.cancel("cancel-original", "doc-original", "actor", "cancel-original-key", expected_version=1)
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError), connection:
                    self._direct_document(connection, "doc-unlinked", "base-p4", "CUST", "unlinked-key")

    def test_direct_sql_rejects_competing_temporary_overrides_with_same_document_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice("doc-price", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "price-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO core_invoice_price_events("
                    "event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,price_sequence,idempotency_key"
                    ") VALUES ('override-first','doc-price','doc-price:line:1','temporary_override','3','CUST','actor',1,2,'override-first-key')"
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_price_events("
                        "event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,price_sequence,idempotency_key"
                        ") VALUES ('override-competing','doc-price','doc-price:line:2','temporary_override','4','CUST','actor',1,2,'override-competing-key')"
                    )

    def test_direct_sql_enforces_p4_price_document_and_lifecycle_invariants(self):
        from apc_core.core_invoices import CoreInvoiceError, CoreInvoiceStore, CoreInvoiceWorkflowStore

        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice("doc-1", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "key-1", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                store.cancel("cancel-1", "doc-1", "actor", "cancel-key", expected_version=1)
                store.create_correction_temporary("doc-2", "doc-1", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "key-2", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                store.cancel("cancel-2", "doc-2", "actor", "cancel-key-2", expected_version=1)
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                # One active document is allowed; a second fully audited active document is not.
                self._direct_document(connection, "doc-active", "base-p4", "CUST", "active-key", "doc-2")
                with self.assertRaises(sqlite3.DatabaseError):
                    self._direct_document(connection, "doc-active-2", "base-p4", "CUST", "active-key-2", "doc-2")
            active = CoreInvoiceWorkflowStore(database)
            try:
                active.cancel("cancel-active", "doc-active", "actor", "cancel-active-key", expected_version=1)
            finally:
                active.close()
            with sqlite3.connect(database) as connection:
                # A second correction child cannot be linked to the same cancelled original.
                self._direct_document(connection, "doc-3", "base-p4", "CUST", "key-3", "doc-active")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_corrections(correction_invoice_id,original_invoice_id,created_by,idempotency_key) "
                        "VALUES ('doc-3','doc-1','actor','second-correction-key')"
                    )

        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice("doc-price", "base-p4", "actor", "CUST", {"p4-line-1": "1", "p4-line-2": "2"}, "price-key", expected_version=0, consignee="Fixture consignee", delivery_reference="Fixture delivery", reference_year=2026)
                line_id = "doc-price:line:1"
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                for event_id, price, customer_code, expected_version in (
                    ("wrong-customer", "2", "OTHER", 1),
                    ("stale-version", "2", "CUST", 0),
                    ("malformed-double-dot", "1..2", "CUST", 1),
                    ("malformed-minus", "1-", "CUST", 1),
                    ("malformed-many-dot", "0.1.2", "CUST", 1),
                ):
                    with self.subTest(event_id=event_id), self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(
                            "INSERT INTO core_invoice_price_events("
                            "event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,idempotency_key"
                            ") VALUES (?,?,?,'temporary_override',?,?, 'actor',?,?)",
                            (event_id, "doc-price", line_id, price, customer_code, expected_version, f"{event_id}-key"),
                        )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) "
                        "VALUES ('p3-after-p4','base-p4','review_submission','actor',1,'p3-after-p4-key')"
                    )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_price_events("
                        "event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,price_sequence,idempotency_key"
                        ") VALUES ('injected-initial','doc-price',?,'customer_code_price','9','CUST','actor',0,2,'injected-initial-key')",
                        (line_id,),
                    )
            real = CoreInvoiceWorkflowStore(database)
            try:
                real.confirm_real("real-price", "doc-price", "PERM-price", "actor", "real-price-key", expected_version=1)
            finally:
                real.close()
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_price_events("
                        "event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,price_sequence,idempotency_key"
                        ") VALUES ('real-override','doc-price',?,'temporary_override','3','CUST','actor',2,3,'real-override-key')",
                        (line_id,),
                    )
            legacy = CoreInvoiceStore(database)
            try:
                with self.assertRaisesRegex(CoreInvoiceError, "P4"):
                    legacy.submit_for_review("p3-after-p4-store", "base-p4", "actor", "p3-after-p4-store-key", expected_version=1)
            finally:
                legacy.close()


class CoreInvoiceWorkflowP5Tests(CoreInvoiceWorkflowTests):
    def test_p5_database_rejects_first_direct_allocation_that_skips_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            store.close()
            with sqlite3.connect(database) as connection:
                # This builds every creation artifact before the allocation insert; only
                # the first allocation's non-sequential number must reject it.
                with self.assertRaisesRegex(sqlite3.DatabaseError, "sequence"):
                    self._direct_document(connection, "p5-skip-777", "base-p4", "ACME", "p5-skip-777-key", reference_sequence=777)
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM core_invoice_reference_allocations WHERE customer_code='ACME' AND reference_year=2026").fetchone()[0])

                # A direct audit fixture may allocate only the actual next number.
                self._direct_document(connection, "p5-direct-001", "base-p4", "ACME", "p5-direct-001-key", reference_sequence=1)
                with self.assertRaisesRegex(sqlite3.DatabaseError, "sequence"):
                    self._direct_document(connection, "p5-skip-003", "base-p4", "ACME", "p5-skip-003-key", reference_sequence=3)
                self.assertEqual(
                    [("p5-direct-001", 1)],
                    connection.execute("SELECT invoice_id,reference_sequence FROM core_invoice_reference_allocations WHERE customer_code='ACME' AND reference_year=2026").fetchall(),
                )

    def test_p5_direct_sql_rejects_allocation_bound_to_different_document_creation_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            store.close()
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO core_invoice_document_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) "
                    "VALUES ('p5-binding:allocation','p5-binding','creation','actor',0,'alloc-event-key')"
                )
                connection.execute(
                    "INSERT INTO core_invoice_document_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) "
                    "VALUES ('p5-binding:document','p5-binding','creation','actor',0,'document-key')"
                )
                for position, (core_line_id,) in enumerate(
                    connection.execute("SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id='base-p4' ORDER BY invoice_line_id"), 1
                ):
                    line_id = f"p5-binding:line:{position}"
                    connection.execute(
                        "INSERT INTO core_invoice_document_lines(document_line_id,invoice_id,core_invoice_line_id) VALUES (?,?,?)",
                        (line_id, "p5-binding", core_line_id),
                    )
                    connection.execute(
                        "INSERT INTO core_invoice_price_events(event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,idempotency_key) "
                        "VALUES (?,?,?,'customer_code_price','1','ACME','actor',0,?)",
                        (f"p5-binding:price:{position}", "p5-binding", line_id, f"p5-binding:price-key:{position}"),
                    )
                connection.execute(
                    "INSERT INTO core_invoice_reference_allocations(invoice_id,allocation_event_id,customer_code,reference_year,reference_sequence,temporary_reference) "
                    "VALUES ('p5-binding','p5-binding:allocation','ACME',2026,1,'ACME-T26-001')"
                )
                connection.execute(
                    "INSERT INTO core_invoice_document_context(invoice_id,customer_code,reference_year,reference_sequence,temporary_reference,consignee,delivery_reference) "
                    "VALUES ('p5-binding','ACME',2026,1,'ACME-T26-001','Direct consignee','Direct delivery')"
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_documents(invoice_id,base_invoice_id,customer_code,state,created_by,version,idempotency_key) "
                        "VALUES ('p5-binding','base-p4','ACME','temporary','actor',1,'document-key')"
                    )

    def test_p5_database_rejects_direct_context_without_official_allocation_and_counter_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice(
                    "p5-doc-1", "base-p4", "actor", "ACME", {"p4-line-1": "1", "p4-line-2": "2"}, "p5-create-1",
                    expected_version=0, consignee="Fixture consignee", delivery_reference="PO-001", reference_year=2026,
                )
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT INTO core_invoice_document_context(invoice_id,customer_code,reference_year,reference_sequence,temporary_reference,consignee,delivery_reference) "
                        "VALUES ('p5-unallocated','ACME',2026,2,'ACME-T26-002','Direct consignee','Direct delivery')"
                    )
                for statement in (
                    "INSERT INTO core_invoice_reference_counters(customer_code,reference_year,next_sequence) VALUES ('ACME',2026,999)",
                    "UPDATE core_invoice_reference_counters SET next_sequence=999 WHERE customer_code='__P5_LEDGER__' AND reference_year=2000",
                    "DELETE FROM core_invoice_reference_counters WHERE customer_code='__P5_LEDGER__' AND reference_year=2000",
                ):
                    with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(statement)
                self.assertEqual(
                    [("p5-doc-1", "ACME", 2026, 1)],
                    connection.execute(
                        "SELECT invoice_id,customer_code,reference_year,reference_sequence "
                        "FROM core_invoice_reference_allocations"
                    ).fetchall(),
                )

    def test_p5_direct_sql_replace_cannot_reallocate_existing_invoice(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice(
                    "p5-doc-replace-allocation", "base-p4", "actor", "ACME",
                    {"p4-line-1": "1", "p4-line-2": "2"}, "p5-replace-allocation-key",
                    expected_version=0, consignee="Original consignee", delivery_reference="Original delivery",
                    reference_year=2026,
                )
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                original_allocation = connection.execute(
                    "SELECT allocation_event_id,customer_code,reference_year,reference_sequence,temporary_reference "
                    "FROM core_invoice_reference_allocations WHERE invoice_id='p5-doc-replace-allocation'"
                ).fetchone()
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT OR REPLACE INTO core_invoice_reference_allocations("
                        "invoice_id,allocation_event_id,customer_code,reference_year,reference_sequence,temporary_reference"
                        ") VALUES ('p5-doc-replace-allocation','p5-doc-replace-allocation:creation',"
                        "'OTHER',2026,1,'OTHER-T26-001')"
                    )
                self.assertEqual(
                    original_allocation,
                    connection.execute(
                        "SELECT allocation_event_id,customer_code,reference_year,reference_sequence,temporary_reference "
                        "FROM core_invoice_reference_allocations WHERE invoice_id='p5-doc-replace-allocation'"
                    ).fetchone(),
                )

    def test_p5_direct_sql_replace_cannot_rewrite_existing_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                store.create_temporary_invoice(
                    "p5-doc-replace-context", "base-p4", "actor", "ACME",
                    {"p4-line-1": "1", "p4-line-2": "2"}, "p5-replace-context-key",
                    expected_version=0, consignee="Original consignee", delivery_reference="Original delivery",
                    reference_year=2026,
                )
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                original_context = connection.execute(
                    "SELECT customer_code,reference_year,reference_sequence,temporary_reference,consignee,delivery_reference "
                    "FROM core_invoice_document_context WHERE invoice_id='p5-doc-replace-context'"
                ).fetchone()
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "INSERT OR REPLACE INTO core_invoice_document_context("
                        "invoice_id,customer_code,reference_year,reference_sequence,temporary_reference,consignee,delivery_reference"
                        ") VALUES ('p5-doc-replace-context','ACME',2026,1,'ACME-T26-001',"
                        "'Replacement consignee','Replacement delivery')"
                    )
                self.assertEqual(
                    original_context,
                    connection.execute(
                        "SELECT customer_code,reference_year,reference_sequence,temporary_reference,consignee,delivery_reference "
                        "FROM core_invoice_document_context WHERE invoice_id='p5-doc-replace-context'"
                    ).fetchone(),
                )

    def test_p5_upgrade_rejects_populated_p4_without_fabricating_context_or_partial_v5(self):
        from apc_core.core_invoices import CoreInvoiceError, CoreInvoiceWorkflowStore
        import apc_core.core_provenance as provenance

        with tempfile.TemporaryDirectory() as temporary:
            database, p3_store = CoreOwnedInvoiceLifecycleTests()._store(Path(temporary))
            try:
                CoreOwnedInvoiceLifecycleTests()._create(p3_store)
            finally:
                p3_store.close()
            with sqlite3.connect(database) as connection:
                connection.execute("BEGIN")
                provenance._migration_004(connection)
                connection.execute("INSERT INTO core_schema_migrations(version) VALUES (4)")
                connection.execute(
                    "INSERT INTO core_invoice_document_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) "
                    "VALUES ('legacy-p4-doc:creation','legacy-p4-doc','creation','actor',0,'legacy-p4-key')"
                )
                for position, (core_line_id,) in enumerate(
                    connection.execute("SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id='invoice-1' ORDER BY invoice_line_id"), 1
                ):
                    line_id = f"legacy-p4-doc:line:{position}"
                    connection.execute(
                        "INSERT INTO core_invoice_document_lines(document_line_id,invoice_id,core_invoice_line_id) VALUES (?,?,?)",
                        (line_id, "legacy-p4-doc", core_line_id),
                    )
                    connection.execute(
                        "INSERT INTO core_invoice_price_events(event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,idempotency_key) "
                        "VALUES (?,?,?,'customer_code_price','1','ACME','actor',0,?)",
                        (f"legacy-p4-doc:price:{position}", "legacy-p4-doc", line_id, f"legacy-p4-price:{position}"),
                    )
                connection.execute(
                    "INSERT INTO core_invoice_documents(invoice_id,base_invoice_id,customer_code,state,created_by,version,idempotency_key) "
                    "VALUES ('legacy-p4-doc','invoice-1','ACME','temporary','actor',1,'legacy-p4-key')"
                )
            with self.assertRaisesRegex(provenance.CoreProvenanceError, "P5.*reset"):
                provenance.apply_core_invoice_workflow_migrations(database)
            with sqlite3.connect(database) as connection:
                self.assertEqual([(1,), (2,), (3,), (4,)], connection.execute("SELECT version FROM core_schema_migrations ORDER BY version").fetchall())
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM core_invoice_documents").fetchone()[0])
                self.assertFalse(any(row[0] == "core_invoice_document_context" for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")))
            with self.assertRaisesRegex(CoreInvoiceError, "migrations"):
                CoreInvoiceWorkflowStore(database)
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO core_schema_migrations(version) VALUES (5)")
            with self.assertRaisesRegex(CoreInvoiceError, "migrations"):
                CoreInvoiceWorkflowStore(database)

    def test_p5_sequence_stops_after_999_without_counter_corruption(self):
        from apc_core.core_invoices import CoreInvoiceError

        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                previous = None
                for sequence in range(1, 999):
                    invoice_id = f"p5-boundary-{sequence}"
                    if previous is None:
                        created = store.create_temporary_invoice(
                            invoice_id, "base-p4", "actor", "LIMIT", {"p4-line-1": "1", "p4-line-2": "2"}, f"boundary-key-{sequence}",
                            expected_version=0, consignee="Fixture consignee", delivery_reference="PO-BOUNDARY", reference_year=2026,
                        )
                    else:
                        created = store.create_correction_temporary(
                            invoice_id, previous, "actor", "LIMIT", {"p4-line-1": "1", "p4-line-2": "2"}, f"boundary-key-{sequence}",
                            expected_version=0, consignee="Fixture consignee", delivery_reference="PO-BOUNDARY", reference_year=2026,
                        )
                    self.assertEqual(f"LIMIT-T26-{sequence:03d}", created["temporary_reference"])
                    store.cancel(f"boundary-cancel-{sequence}", invoice_id, "actor", f"boundary-cancel-key-{sequence}", expected_version=1)
                    previous = invoice_id
                last = store.create_correction_temporary(
                    "p5-boundary-999", previous, "actor", "LIMIT", {"p4-line-1": "1", "p4-line-2": "2"}, "boundary-key-999",
                    expected_version=0, consignee="Fixture consignee", delivery_reference="PO-BOUNDARY", reference_year=2026,
                )
                self.assertEqual("LIMIT-T26-999", last["temporary_reference"])
                store.cancel("boundary-cancel-999", "p5-boundary-999", "actor", "boundary-cancel-key-999", expected_version=1)
                with self.assertRaisesRegex(CoreInvoiceError, "Temporary reference sequence is full for this customer and year"):
                    store.create_correction_temporary(
                        "p5-boundary-1000", "p5-boundary-999", "actor", "LIMIT", {"p4-line-1": "1", "p4-line-2": "2"}, "boundary-key-1000",
                        expected_version=0, consignee="Fixture consignee", delivery_reference="PO-BOUNDARY", reference_year=2026,
                    )
                self.assertEqual(
                    999,
                    store.connection.execute(
                        "SELECT COUNT(*) FROM core_invoice_reference_allocations WHERE customer_code='LIMIT' AND reference_year=2026"
                    ).fetchone()[0],
                )
            finally:
                store.close()

    def test_p5_allocates_customer_year_references_and_preserves_them_after_real_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._workflow(Path(temporary))
            try:
                created = store.create_temporary_invoice(
                    "p5-doc-1", "base-p4", "actor", "acme", {"p4-line-1": "1", "p4-line-2": "2"}, "p5-create-1",
                    expected_version=0, consignee="Bangkok Warehouse", delivery_reference="PO-100", reference_year=2026,
                )
                self.assertEqual("ACME-T26-001", created["temporary_reference"])
                real = store.confirm_real("p5-real-1", "p5-doc-1", "REAL-100", "actor", "p5-real-key", expected_version=1)
                self.assertEqual("ACME-T26-001", real["temporary_reference"])
                self.assertEqual("ACME-T26-001", store.get_invoice("p5-doc-1")["temporary_reference"])
            finally:
                store.close()

    def test_p5_uses_independent_customer_year_counters_and_persists_distinct_shipment_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._workflow(Path(temporary))
            try:
                first = store.create_temporary_invoice(
                    "p5-doc-1", "base-p4", "actor", "ACME", {"p4-line-1": "1", "p4-line-2": "2"}, "p5-create-1",
                    expected_version=0, consignee="First consignee", delivery_reference="PO-001", reference_year=2026,
                )
                store.cancel("p5-cancel-1", "p5-doc-1", "actor", "p5-cancel-key-1", expected_version=1)
                second = store.create_correction_temporary(
                    "p5-doc-2", "p5-doc-1", "actor", "ACME", {"p4-line-1": "1", "p4-line-2": "2"}, "p5-create-2",
                    expected_version=0, consignee="Second consignee", delivery_reference="PO-002", reference_year=2026,
                )
                store.cancel("p5-cancel-2", "p5-doc-2", "actor", "p5-cancel-key-2", expected_version=1)
                third = store.create_correction_temporary(
                    "p5-doc-3", "p5-doc-2", "actor", "BETA", {"p4-line-1": "1", "p4-line-2": "2"}, "p5-create-3",
                    expected_version=0, consignee="Third consignee", delivery_reference="PO-003", reference_year=2026,
                )
                self.assertEqual("ACME-T26-001", first["temporary_reference"])
                self.assertEqual("ACME-T26-002", second["temporary_reference"])
                self.assertEqual("BETA-T26-001", third["temporary_reference"])
                self.assertEqual(
                    {"consignee": "Second consignee", "delivery_reference": "PO-002"},
                    {key: store.get_invoice("p5-doc-2")[key] for key in ("consignee", "delivery_reference")},
                )
                self.assertEqual(["p5-doc-2"], [row["invoice_id"] for row in store.search_invoices(consignee="Second consignee", delivery_reference="PO-002")])
            finally:
                store.close()

    def test_p5_rejects_invalid_context_customer_code_and_reference_year_and_direct_sql_missing_context(self):
        from apc_core.core_invoices import CoreInvoiceError
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._workflow(Path(temporary))
            try:
                for customer_code, consignee, delivery_reference, reference_year in (
                    ("bad customer", "Consignee", "PO", 2026),
                    ("ACME", " ", "PO", 2026),
                    ("ACME", "Consignee", "", 2026),
                    ("ACME", "Consignee", "PO", 26),
                ):
                    with self.subTest(customer_code=customer_code, consignee=consignee, delivery_reference=delivery_reference, reference_year=reference_year), self.assertRaises(CoreInvoiceError):
                        store.create_temporary_invoice(
                            "bad-doc", "base-p4", "actor", customer_code, {"p4-line-1": "1", "p4-line-2": "2"}, "bad-key",
                            expected_version=0, consignee=consignee, delivery_reference=delivery_reference, reference_year=reference_year,
                        )
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO core_invoice_document_events(event_id,invoice_id,event_kind,actor,expected_version,idempotency_key) VALUES ('p5-direct:creation','p5-direct','creation','actor',0,'p5-direct-key')")
                for position, (core_line_id,) in enumerate(connection.execute("SELECT invoice_line_id FROM core_invoice_lines WHERE invoice_id='base-p4' ORDER BY invoice_line_id"), 1):
                    line_id = f"p5-direct:line:{position}"
                    connection.execute("INSERT INTO core_invoice_document_lines(document_line_id,invoice_id,core_invoice_line_id) VALUES (?,?,?)", (line_id, "p5-direct", core_line_id))
                    connection.execute("INSERT INTO core_invoice_price_events(event_id,invoice_id,document_line_id,event_kind,unit_price,customer_code,actor,expected_version,idempotency_key) VALUES (?,?,?,'customer_code_price','1','ACME','actor',0,?)", (f"p5-direct:price:{position}", "p5-direct", line_id, f"p5-direct-price:{position}"))
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("INSERT INTO core_invoice_documents(invoice_id,base_invoice_id,customer_code,state,created_by,version,idempotency_key) VALUES ('p5-direct','base-p4','ACME','temporary','actor',1,'p5-direct-key')")


if __name__ == "__main__":
    unittest.main()
