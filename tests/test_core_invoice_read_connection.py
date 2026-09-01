import hashlib
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path


class CoreInvoiceReadConnectionTests(unittest.TestCase):
    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _p5_database(root: Path) -> Path:
        from apc_core.core_provenance import apply_core_invoice_workflow_migrations

        database = root / "core.sqlite"
        apply_core_invoice_workflow_migrations(database)
        return database

    def test_opens_a_new_closeable_p5_database_connection_for_each_request(self):
        from apc_core.core_invoice_read_connection import open_core_invoice_read_connection

        with tempfile.TemporaryDirectory() as temporary:
            database = self._p5_database(Path(temporary))

            first = open_core_invoice_read_connection(database)
            second = open_core_invoice_read_connection(database)
            try:
                self.assertIsNot(first.connection, second.connection)
                self.assertEqual(1, first.connection.execute("PRAGMA query_only").fetchone()[0])
                self.assertEqual(5, first.connection.execute("SELECT MAX(version) FROM core_schema_migrations").fetchone()[0])
            finally:
                first.close()
                second.close()

            with self.assertRaises(sqlite3.ProgrammingError):
                first.connection.execute("SELECT 1")

    def test_reading_through_the_boundary_leaves_database_bytes_unchanged(self):
        from apc_core.core_invoice_read_connection import open_core_invoice_read_connection

        with tempfile.TemporaryDirectory() as temporary:
            database = self._p5_database(Path(temporary))
            before = self._sha256(database)

            boundary = open_core_invoice_read_connection(database)
            try:
                self.assertEqual(5, boundary.connection.execute("SELECT MAX(version) FROM core_schema_migrations").fetchone()[0])
            finally:
                boundary.close()

            self.assertEqual(before, self._sha256(database))

    def test_rejects_missing_and_unmigrated_databases_without_creating_them(self):
        from apc_core.core_invoice_read_connection import CoreInvoiceReadConnectionError, open_core_invoice_read_connection

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.sqlite"
            with self.assertRaisesRegex(CoreInvoiceReadConnectionError, "missing"):
                open_core_invoice_read_connection(missing)
            self.assertFalse(missing.exists())

            unmigrated = root / "unmigrated.sqlite"
            with sqlite3.connect(unmigrated) as connection:
                connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            before = self._sha256(unmigrated)
            with self.assertRaisesRegex(CoreInvoiceReadConnectionError, "migrations"):
                open_core_invoice_read_connection(unmigrated)
            self.assertEqual(before, self._sha256(unmigrated))

    def test_module_is_a_read_only_connection_boundary_not_a_workflow_store_or_migration_path(self):
        import apc_core.core_invoice_read_connection as module

        source = inspect.getsource(module)
        self.assertNotIn("CoreInvoiceWorkflowStore", source)
        self.assertNotIn("apply_core_invoice_migrations", source)
        self.assertIn("mode=ro", source)
        self.assertIn("query_only", source)
        self.assertIn("check_same_thread=False", source)


if __name__ == "__main__":
    unittest.main()
