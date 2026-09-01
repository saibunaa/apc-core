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


if __name__ == "__main__":
    unittest.main()
