import hashlib
import tempfile
import unittest
from pathlib import Path


class TestVerifiedStagedLegacyInvoiceRuntime(unittest.TestCase):
    def make_invoice_fixture(self, root: Path) -> Path:
        from tests.test_source_invoice_explorer import TestSourceInvoiceExplorerContract

        return TestSourceInvoiceExplorerContract().make_snapshot(root)

    def test_verified_staged_snapshot_requires_the_exact_hash_and_has_no_wal_sidecars(self):
        from apc_core import server

        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_invoice_fixture(Path(tmp))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            reader = server.load_verified_legacy_invoice_snapshot(source, digest)
            try:
                self.assertEqual(digest, reader.source_sha256)
                self.assertEqual("source_invoice", reader.search_invoices(prefix="C//")["invoices"][0]["source_type"])
            finally:
                reader.close()

            with self.assertRaises(server.RuntimeContractError):
                server.load_verified_legacy_invoice_snapshot(source, "0" * 64)

            (Path(str(source) + "-wal")).write_bytes(b"not a real journal")
            with self.assertRaises(server.RuntimeContractError):
                server.load_verified_legacy_invoice_snapshot(source, digest)

    def test_workspace_uses_exact_legacy_invoices_read_only_copy_without_new_actions(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn("Legacy Invoices · Read-only", html)
        self.assertIn("LEGACY INVOICES · READ-ONLY", html)
        self.assertNotIn("Print invoice", html)
        self.assertNotIn("Delete invoice", html)
        self.assertNotIn("Save invoice", html)
