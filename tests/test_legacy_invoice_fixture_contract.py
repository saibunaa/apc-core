import hashlib
import os
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


_FIXED_CHILD = "legacy-invoice-fixture.sqlite"


class LegacyInvoiceFixtureContractTests(unittest.TestCase):
    def make_fixture(self, owner: tempfile.TemporaryDirectory[str]) -> Path:
        database = Path(owner.name) / _FIXED_CHILD
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE "MainDB__INVOICE" ("Inv No" TEXT, "Cust ID" TEXT, "Date" TEXT)')
            connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
            connection.executemany(
                'INSERT INTO "MainDB__INVOICE" VALUES (?, ?, ?)',
                (("C//2026/002", "C/002", "2026-08-30"), ("C//2026/001", "C/001", "2026-08-29")),
            )
            connection.executemany(
                'INSERT INTO "MainDB__CUST" VALUES (?, ?)',
                (("C/001", "Fixture customer one"), ("C/002", "Fixture customer two")),
            )
        return database

    def test_contract_accepts_only_a_live_fixture_owner_and_returns_closed_frozen_records(self):
        from apc_core.legacy_invoice_fixture_contract import LegacyInvoiceFixtureContract

        with tempfile.TemporaryDirectory() as temporary:
            owner = tempfile.TemporaryDirectory(dir=temporary)
            try:
                database = self.make_fixture(owner)
                before = database.read_bytes()
                contract = LegacyInvoiceFixtureContract(owner)
                records = contract.list_imported_invoices()
                self.assertEqual(before, database.read_bytes())
                with self.assertRaises(FrozenInstanceError):
                    records[0].invoice_id = "mutated"
            finally:
                owner.cleanup()

        self.assertEqual(
            (("C//2026/001", "2026-08-29", "C/001", "Fixture customer one"),
             ("C//2026/002", "2026-08-30", "C/002", "Fixture customer two")),
            tuple((record.invoice_id, record.invoice_date, record.customer_id, record.customer_name) for record in records),
        )
        with self.assertRaises(ValueError):
            LegacyInvoiceFixtureContract(Path("not-an-owner"))

    def test_contract_rejects_source_replacement_and_expired_owner_before_reading(self):
        from apc_core.legacy_invoice_fixture_contract import LegacyInvoiceFixtureContract

        owner = tempfile.TemporaryDirectory()
        try:
            database = self.make_fixture(owner)
            contract = LegacyInvoiceFixtureContract(owner)
            replacement = Path(owner.name) / "replacement.sqlite"
            with sqlite3.connect(replacement) as connection:
                connection.execute('CREATE TABLE "MainDB__INVOICE" ("Inv No" TEXT, "Cust ID" TEXT, "Date" TEXT)')
                connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
            database.unlink()
            replacement.rename(database)
            with self.assertRaises(ValueError):
                contract.list_imported_invoices()
        finally:
            owner.cleanup()

        with self.assertRaises(ValueError):
            contract.list_imported_invoices()

    def test_contract_rejects_a_symlinked_fixed_child_without_following_it(self):
        from apc_core.legacy_invoice_fixture_contract import LegacyInvoiceFixtureContract

        with tempfile.TemporaryDirectory() as temporary:
            owner = tempfile.TemporaryDirectory(dir=temporary)
            try:
                database = self.make_fixture(owner)
                target = Path(temporary) / "target.sqlite"
                database.rename(target)
                database.symlink_to(target)
                with self.assertRaises(ValueError):
                    LegacyInvoiceFixtureContract(owner)
            finally:
                owner.cleanup()

    def test_private_connection_authorizer_denies_mutation_ddl_pragma_and_attach(self):
        from apc_core.legacy_invoice_fixture_contract import LegacyInvoiceFixtureContract

        with tempfile.TemporaryDirectory() as temporary:
            owner = tempfile.TemporaryDirectory(dir=temporary)
            try:
                self.make_fixture(owner)
                contract = LegacyInvoiceFixtureContract(owner)
                connection = contract._open_read_connection()
                try:
                    for statement in ("UPDATE MainDB__INVOICE SET 'Inv No'='changed'", "DROP TABLE MainDB__CUST", "PRAGMA user_version", "ATTACH ':memory:' AS extra"):
                        with self.assertRaises(sqlite3.DatabaseError):
                            connection.execute(statement)
                finally:
                    connection.close()
            finally:
                owner.cleanup()

    def test_module_has_no_runtime_route_or_general_source_path_surface(self):
        module_path = Path(__file__).parents[1] / "apc_core" / "legacy_invoice_fixture_contract.py"
        source = module_path.read_text(encoding="utf-8")

        for forbidden in ("source_invoice_explorer", "item_explorer", "make_handler", "server", "socket", "requests", "subprocess", "source_path"):
            self.assertNotIn(forbidden, source)
        self.assertIn("legacy-invoice-fixture.sqlite", source)


if __name__ == "__main__":
    unittest.main()
