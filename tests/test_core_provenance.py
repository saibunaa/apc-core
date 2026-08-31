import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class CoreProvenanceFoundationTests(unittest.TestCase):
    def _snapshot(
        self, root: Path, *, wal_bytes: bytes | None = None, include_subcust: bool = True, description: str = "ปลา"
    ) -> Path:
        snapshot = root / "accepted.sqlite"
        root.mkdir(parents=True, exist_ok=True)
        columns = '"Order No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Qty" TEXT, "Description TH" TEXT'
        if include_subcust:
            columns += ', "SubCust" TEXT'
        with sqlite3.connect(snapshot) as connection:
            connection.execute(f'CREATE TABLE "MainDB__ORDER_ITEM" ({columns})')
            values = [
                ("ORD-1", "001", "ITEM-1", "2", description, "A"),
                ("ORD-1", "001", "ITEM-1", "2", description, "A"),
                ("ORD-1", "001", "ITEM-1", "2", description, "A"),
                ("ORD-1", "001", "ITEM-1", "2", description, "A"),
            ]
            if not include_subcust:
                values = [value[:-1] for value in values]
            placeholders = ", ".join("?" for _ in values[0])
            connection.executemany(f'INSERT INTO "MainDB__ORDER_ITEM" VALUES ({placeholders})', values)
        if wal_bytes is not None:
            snapshot.with_name(snapshot.name + "-wal").write_bytes(wal_bytes)
        return snapshot

    def test_explicit_migration_is_idempotent_and_constructor_does_not_create_schema(self):
        from apc_core.core_provenance import CoreProvenanceStore, apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "core.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE legacy_state (id INTEGER PRIMARY KEY)")
            with sqlite3.connect(database) as connection:
                before = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            store = CoreProvenanceStore(database)
            store.close()
            with sqlite3.connect(database) as connection:
                self.assertEqual(before, connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())

            self.assertEqual(2, apply_core_provenance_migrations(database))
            self.assertEqual(2, apply_core_provenance_migrations(database))
            with sqlite3.connect(database) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                versions = connection.execute("SELECT version FROM core_schema_migrations").fetchall()
            self.assertTrue({"legacy_state", "core_schema_migrations", "core_source_snapshots", "core_source_rows"}.issubset(tables))
            self.assertEqual([(1,), (2,)], versions)

    def test_store_constructor_rejects_missing_database_without_creating_it(self):
        from apc_core.core_provenance import CoreProvenanceError, CoreProvenanceStore

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing.sqlite"
            with self.assertRaisesRegex(CoreProvenanceError, "database is missing"):
                CoreProvenanceStore(database)
            self.assertFalse(database.exists())

    def test_import_uses_snapshot_table_rowid_when_visible_source_fields_collide(self):
        from apc_core.core_provenance import CoreProvenanceStore, apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root)
            database = root / "core.sqlite"
            source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            apply_core_provenance_migrations(database)
            store = CoreProvenanceStore(database)
            try:
                before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
                first = store.import_order_item_snapshot(snapshot, snapshot_sha256=source_hash, imported_at="2026-09-01T00:00:00Z")
                second = store.import_order_item_snapshot(snapshot, snapshot_sha256=source_hash, imported_at="2026-09-01T00:00:00Z")
                self.assertEqual(before, hashlib.sha256(snapshot.read_bytes()).hexdigest())
                self.assertEqual(4, first.row_count)
                self.assertFalse(first.replayed)
                self.assertTrue(second.replayed)
                self.assertEqual(4, second.row_count)
                rows = store.source_rows(source_hash)
            finally:
                store.close()

            self.assertEqual([1, 2, 3, 4], [row["source_rowid"] for row in rows])
            self.assertEqual(4, len({(row["snapshot_sha256"], row["source_table"], row["source_rowid"]) for row in rows}))
            self.assertTrue(all(row["document_id"] == "ORD-1" and row["line_label"] == "001" for row in rows))
            self.assertTrue(all(row["source_kind"] == "order_item" for row in rows))
            with sqlite3.connect(database) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE core_source_rows SET line_label='changed' WHERE source_rowid=1")
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("DELETE FROM core_source_snapshots WHERE snapshot_sha256=?", (source_hash,))

    def test_import_binds_to_opened_source_when_source_path_is_replaced_mid_import(self):
        from apc_core.core_provenance import CoreProvenanceStore, apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root, description="before")
            replacement = self._snapshot(root / "replacement", description="after")
            database = root / "core.sqlite"
            source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            apply_core_provenance_migrations(database)
            import apc_core.core_provenance as provenance

            original_open = provenance.os.open
            replaced = False

            def replace_after_open(path, flags, *args):
                nonlocal replaced
                descriptor = original_open(path, flags, *args)
                if not replaced and Path(path) == snapshot:
                    replacement.replace(snapshot)
                    replaced = True
                return descriptor

            store = CoreProvenanceStore(database)
            try:
                with mock.patch.object(provenance.os, "open", side_effect=replace_after_open):
                    receipt = store.import_order_item_snapshot(snapshot, snapshot_sha256=source_hash, imported_at="2026-09-01T00:00:00Z")
                rows = store.source_rows(source_hash)
            finally:
                store.close()

            self.assertTrue(replaced)
            self.assertEqual(4, receipt.row_count)
            self.assertEqual("before", __import__("json").loads(rows[0]["evidence_json"])["description_th"])
            self.assertNotEqual(source_hash, hashlib.sha256(snapshot.read_bytes()).hexdigest())

    def test_import_preserves_zero_negative_and_null_legacy_row_values(self):
        from apc_core.core_provenance import CoreProvenanceStore, apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root)
            with sqlite3.connect(snapshot) as connection:
                connection.execute('UPDATE "MainDB__ORDER_ITEM" SET rowid=0 WHERE rowid=1')
                connection.execute(
                    'UPDATE "MainDB__ORDER_ITEM" SET rowid=-9, "Order No"=NULL, "Line No"=NULL, "Item ID"=NULL, "Qty"=NULL WHERE rowid=2'
                )
            database = root / "core.sqlite"
            source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            apply_core_provenance_migrations(database)
            store = CoreProvenanceStore(database)
            try:
                receipt = store.import_order_item_snapshot(snapshot, snapshot_sha256=source_hash, imported_at="2026-09-01T00:00:00Z")
                rows = store.source_rows(source_hash)
            finally:
                store.close()

            self.assertEqual(4, receipt.row_count)
            self.assertEqual([-9, 0, 3, 4], [row["source_rowid"] for row in rows])
            self.assertEqual((None, None, None, None), tuple(rows[0][field] for field in ("document_id", "line_label", "item_id", "quantity")))

    def test_import_rejects_missing_projected_columns_before_persisting(self):
        from apc_core.core_provenance import CoreProvenanceError, CoreProvenanceStore, apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root, include_subcust=False)
            database = root / "core.sqlite"
            source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            apply_core_provenance_migrations(database)
            store = CoreProvenanceStore(database)
            try:
                with self.assertRaisesRegex(CoreProvenanceError, "order-item schema is incomplete"):
                    store.import_order_item_snapshot(snapshot, snapshot_sha256=source_hash, imported_at="2026-09-01T00:00:00Z")
                self.assertEqual([], store.source_rows(source_hash))
            finally:
                store.close()

    def test_import_rejects_hash_mismatch_and_nonempty_wal_before_persisting(self):
        from apc_core.core_provenance import CoreProvenanceError, CoreProvenanceStore, apply_core_provenance_migrations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = self._snapshot(root, wal_bytes=b"not-an-accepted-wal")
            database = root / "core.sqlite"
            apply_core_provenance_migrations(database)
            store = CoreProvenanceStore(database)
            try:
                with self.assertRaisesRegex(CoreProvenanceError, "non-empty WAL"):
                    store.import_order_item_snapshot(snapshot, snapshot_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(), imported_at="2026-09-01T00:00:00Z")
                snapshot.with_name(snapshot.name + "-wal").unlink()
                with self.assertRaisesRegex(CoreProvenanceError, "hash does not match"):
                    store.import_order_item_snapshot(snapshot, snapshot_sha256="0" * 64, imported_at="2026-09-01T00:00:00Z")
                self.assertEqual([], store.source_rows(hashlib.sha256(snapshot.read_bytes()).hexdigest()))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
