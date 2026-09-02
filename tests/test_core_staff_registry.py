import ast
import importlib
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path


class CoreStaffRegistryTests(unittest.TestCase):
    def _registry(self, root: Path):
        from apc_core.core_staff_registry import CoreStaffRegistry

        return CoreStaffRegistry(root / "fixture-core.sqlite")

    def test_migration_creates_only_the_separate_empty_staff_registry_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "fixture-core.sqlite"
            registry = self._registry(Path(temporary))
            try:
                registry.migrate()
                registry.migrate()
            finally:
                registry.close()

            with sqlite3.connect(database) as connection:
                tables = {
                    row[0]
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                columns = [row[1] for row in connection.execute("PRAGMA table_info(core_active_staff_registry)")]
                rows = connection.execute("SELECT name, role FROM core_active_staff_registry").fetchall()

            self.assertEqual({"core_active_staff_registry"}, tables)
            self.assertEqual(["name", "role", "active"], columns)
            self.assertEqual([], rows)

    def test_fixture_records_round_trip_as_an_active_staff_provider_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry = self._registry(Path(temporary))
            try:
                registry.migrate()
                registry.replace_fixture_records((("TEST_Y", "Fixture"), ("TEST_W", "Fixture")))
                provider = registry.active_staff_provider()
            finally:
                registry.close()

        self.assertEqual(
            (("TEST_W", "Fixture"), ("TEST_Y", "Fixture")),
            tuple((record.name, record.role) for record in provider.active_staff()),
        )
        self.assertTrue(provider.is_active("TEST_W"))
        self.assertFalse(provider.is_active("TESTER"))

    def test_invalid_fixture_does_not_replace_existing_registry_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry = self._registry(Path(temporary))
            try:
                registry.migrate()
                registry.replace_fixture_records((("TEST_W", "Fixture"),))
                with self.assertRaises(ValueError):
                    registry.replace_fixture_records((("TEST_W", "Fixture"), ("TEST_W", "Duplicate")))
                provider = registry.active_staff_provider()
            finally:
                registry.close()

        self.assertEqual((("TEST_W", "Fixture"),), tuple((record.name, record.role) for record in provider.active_staff()))

    def test_current_identity_staff_seed_is_idempotent_and_does_not_replace_existing_registry_records(self):
        from apc_core.core_staff_registry import CURRENT_IDENTITY_STAFF

        with tempfile.TemporaryDirectory() as temporary:
            registry = self._registry(Path(temporary))
            try:
                registry.migrate()
                registry.seed_current_identity_staff_if_empty()
                registry.seed_current_identity_staff_if_empty()
                seeded = registry.active_staff_provider()
                registry.replace_fixture_records((("TEST_W", "Fixture"),))
                registry.seed_current_identity_staff_if_empty()
                preserved = registry.active_staff_provider()
            finally:
                registry.close()

        self.assertEqual(CURRENT_IDENTITY_STAFF, tuple((record.name, record.role) for record in seeded.active_staff()))
        self.assertEqual((("TEST_W", "Fixture"),), tuple((record.name, record.role) for record in preserved.active_staff()))

    def test_module_has_no_picker_route_runtime_or_legacy_staff_dependency(self):
        module = importlib.import_module("apc_core.core_staff_registry")
        tree = ast.parse(inspect.getsource(module))
        source = inspect.getsource(module)

        self.assertEqual({"sqlite3", "pathlib", "apc_core"}, {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        })
        for forbidden in ("item_explorer", "core_users", "localStorage", "make_handler", "invoice_read", "server"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
