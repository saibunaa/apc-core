import ast
import importlib
import inspect
import unittest


class ActiveStaffProviderTests(unittest.TestCase):
    def _provider(self, fixture=(("WAT", "Office"), ("YIM", "Editor"))):
        from apc_core.active_staff_provider import ActiveStaffProvider

        return ActiveStaffProvider(fixture)

    def test_returns_sorted_immutable_fixture_records(self):
        provider = self._provider((("YIM", "Editor"), ("WAT", "Office")))

        self.assertEqual(
            (("WAT", "Office"), ("YIM", "Editor")),
            tuple((record.name, record.role) for record in provider.active_staff()),
        )
        self.assertIsInstance(provider.active_staff(), tuple)
        with self.assertRaises(Exception):
            provider.active_staff()[0].name = "BON"

    def test_exact_active_name_check_is_case_and_whitespace_sensitive(self):
        provider = self._provider()

        self.assertTrue(provider.is_active("WAT"))
        for invalid_name in (None, "", "wat", "WAT ", "UNKNOWN", 1):
            self.assertFalse(provider.is_active(invalid_name))

    def test_rejects_malformed_or_duplicate_fixture_records(self):
        for fixture in (
            [],
            (("WAT", "Office"), ("WAT", "Admin")),
            (("", "Office"),),
            (("WAT", ""),),
            (("WAT", "Office", "extra"),),
            (("WAT", 1),),
        ):
            with self.subTest(fixture=fixture):
                with self.assertRaises(ValueError):
                    self._provider(fixture)

    def test_module_is_pure_and_has_no_runtime_or_staff_exposure_dependencies(self):
        module = importlib.import_module("apc_core.active_staff_provider")
        tree = ast.parse(inspect.getsource(module))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )

        self.assertFalse(imported_roots & {"sqlite3", "http", "socket", "pathlib", "os", "apc_core"})
        source = inspect.getsource(module)
        for forbidden in ("item_explorer", "server", "localStorage", "core_users", "route", "handler"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
