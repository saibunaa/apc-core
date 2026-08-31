import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path


class CoreOwnedOrderPackingTests(unittest.TestCase):
    SNAPSHOT = "a" * 64
    TABLE = "MainDB__ORDER_ITEM"

    def _store(self, root: Path):
        from apc_core.core_orders import CoreOrderStore
        from apc_core.core_provenance import apply_core_provenance_migrations

        database = root / "core.sqlite"
        apply_core_provenance_migrations(database)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO core_source_snapshots(snapshot_sha256,artifact_path,imported_at) VALUES (?,?,?)",
                (self.SNAPSHOT, "/fixture/accepted.sqlite", "2026-09-01T00:00:00Z"),
            )
            for rowid, quantity in ((7, "10.00"), (8, "6")):
                connection.execute(
                    "INSERT INTO core_source_rows("
                    "snapshot_sha256,source_table,source_rowid,source_kind,document_id,line_label,item_id,quantity,evidence_json"
                    ") VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.SNAPSHOT, self.TABLE, rowid, "order_item", "ORD//2026/001", str(rowid), f"ITEM-{rowid}", quantity, "{}"),
                )
        return database, CoreOrderStore(database)

    def _create(self, store):
        return store.create_order(
            order_id="core-order-1",
            actor="WAT",
            idempotency_key="create-1",
            lines=[
                {"line_id": "line-7", "snapshot_sha256": self.SNAPSHOT, "source_table": self.TABLE, "source_rowid": 7},
                {"line_id": "line-8", "snapshot_sha256": self.SNAPSHOT, "source_table": self.TABLE, "source_rowid": 8},
            ],
        )

    def test_explicit_p2_migration_creates_only_core_owned_tables_and_constructor_is_inert(self):
        from apc_core.core_orders import CoreOrderError, CoreOrderStore
        from apc_core.core_provenance import apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "core.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE legacy_state(id INTEGER PRIMARY KEY)")
            with self.assertRaisesRegex(CoreOrderError, "migrations"):
                CoreOrderStore(database)
            self.assertEqual(2, apply_core_provenance_migrations(database))
            with sqlite3.connect(database) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"core_orders", "core_order_lines", "core_packing_plans", "core_packing_boxes", "core_packing_events"}.issubset(tables))
            self.assertFalse(any("invoice" in name.lower() or "legacy" in name.lower() and name != "legacy_state" for name in tables))

    def test_p2_migration_upgrades_a_p1_provenance_database_without_rewriting_evidence(self):
        import apc_core.core_provenance as provenance
        from apc_core.core_provenance import apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "p1.sqlite"
            with sqlite3.connect(database) as connection:
                provenance._migration_001(connection)
                connection.execute("CREATE TABLE core_schema_migrations(version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
                connection.execute("INSERT INTO core_schema_migrations(version) VALUES (1)")
                connection.execute("INSERT INTO core_source_snapshots(snapshot_sha256,artifact_path,imported_at) VALUES (?,?,?)", (self.SNAPSHOT, "/fixture/accepted.sqlite", "2026-09-01T00:00:00Z"))
            self.assertEqual(2, apply_core_provenance_migrations(database))
            with sqlite3.connect(database) as connection:
                self.assertEqual([(1,), (2,)], connection.execute("SELECT version FROM core_schema_migrations ORDER BY version").fetchall())
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM core_source_snapshots").fetchone()[0])

    def test_order_creation_is_idempotent_and_links_only_explicit_immutable_source_coordinates(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._store(Path(temporary))
            try:
                first = self._create(store)
                replay = self._create(store)
                self.assertEqual(first, replay)
                self.assertEqual("core-order-1", first["order_id"])
                self.assertEqual(0, first["version"])
                self.assertEqual(2, len(store.order_lines("core-order-1")))
                with self.assertRaisesRegex(Exception, "idempotency"):
                    store.create_order("core-order-2", "WAT", "create-1", [{"line_id": "other", "snapshot_sha256": self.SNAPSHOT, "source_table": self.TABLE, "source_rowid": 7}])
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM core_orders").fetchone()[0])
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE core_order_lines SET original_quantity='99' WHERE line_id='line-7'")

    def test_unknown_source_coordinate_or_source_quantity_that_is_not_positive_fails_closed(self):
        from apc_core.core_orders import CoreOrderError

        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._store(Path(temporary))
            with sqlite3.connect(_database) as connection:
                connection.execute(
                    "INSERT INTO core_source_rows("
                    "snapshot_sha256,source_table,source_rowid,source_kind,document_id,line_label,item_id,quantity,evidence_json"
                    ") VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.SNAPSHOT, self.TABLE, 9, "order_item", "ORD//2026/001", "9", "ITEM-9", "0", "{}"),
                )
            try:
                with self.assertRaisesRegex(CoreOrderError, "source"):
                    store.create_order("bad", "WAT", "bad-1", [{"line_id": "bad", "snapshot_sha256": self.SNAPSHOT, "source_table": self.TABLE, "source_rowid": 999}])
                with self.assertRaisesRegex(CoreOrderError, "positive"):
                    store.create_order("bad-2", "WAT", "bad-2", [{"line_id": "bad-2", "snapshot_sha256": self.SNAPSHOT, "source_table": self.TABLE, "source_rowid": 9}])
            finally:
                store.close()

    def test_box_allocation_unavailable_and_reversal_preserve_exact_decimal_conservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _database, store = self._store(Path(temporary))
            try:
                self._create(store)
                plan = store.create_packing_plan("plan-1", "core-order-1", "WAT", "plan-1")
                box = store.create_box("box-1", "plan-1", 1, "WAT", "box-1", expected_version=plan["version"])
                allocation = store.allocate("alloc-1", "plan-1", "line-7", "box-1", "4.25", "WAT", "alloc-1", expected_version=box["version"])
                unavailable = store.mark_unavailable("unavailable-1", "plan-1", "line-7", "2.75", "WAT", "unavailable-1", expected_version=allocation["version"])
                reconciliation = store.reconciliation("plan-1", "line-7")
                self.assertEqual({"original_quantity": "10", "active_allocated": "4.25", "active_unavailable": "2.75", "remaining_unallocated": "3"}, reconciliation)
                reversal = store.reverse_event("reverse-1", "plan-1", "alloc-1", "fixture correction", "WAT", "reverse-1", expected_version=unavailable["version"])
                self.assertEqual(reversal, store.reverse_event("reverse-1", "plan-1", "alloc-1", "fixture correction", "WAT", "reverse-1", expected_version=unavailable["version"]))
                self.assertEqual({"original_quantity": "10", "active_allocated": "0", "active_unavailable": "2.75", "remaining_unallocated": "7.25"}, store.reconciliation("plan-1", "line-7"))
            finally:
                store.close()

    def test_over_allocation_bad_versions_duplicate_semantic_event_and_direct_sql_mutation_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._store(Path(temporary))
            try:
                self._create(store)
                plan = store.create_packing_plan("plan-1", "core-order-1", "WAT", "plan-1")
                box = store.create_box("box-1", "plan-1", 1, "WAT", "box-1", expected_version=plan["version"])
                with self.assertRaisesRegex(Exception, "idempotency"):
                    store.create_box("box-1", "plan-1", 1, "WAT", "box-1", expected_version=box["version"])
                allocation = store.allocate("alloc-1", "plan-1", "line-7", "box-1", "6", "WAT", "alloc-1", expected_version=box["version"])
                with self.assertRaisesRegex(Exception, "idempotency"):
                    store.allocate("alloc-1", "plan-1", "line-7", "box-1", "6", "WAT", "alloc-1", expected_version=allocation["version"])
                with self.assertRaisesRegex(Exception, "version"):
                    store.mark_unavailable("unavailable-1", "plan-1", "line-7", "1", "WAT", "unavailable-1", expected_version=box["version"])
                with self.assertRaisesRegex(Exception, "available"):
                    store.mark_unavailable("unavailable-1", "plan-1", "line-7", "5", "WAT", "unavailable-1", expected_version=allocation["version"])
                with self.assertRaisesRegex(Exception, "semantic"):
                    store.allocate("alloc-2", "plan-1", "line-7", "box-1", "1", "WAT", "alloc-2", expected_version=allocation["version"])
            finally:
                store.close()
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE core_packing_events SET quantity='1' WHERE event_id='alloc-1'")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("DELETE FROM core_packing_events WHERE event_id='alloc-1'")


if __name__ == "__main__":
    unittest.main()
