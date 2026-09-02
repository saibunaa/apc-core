import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestVerifiedStagedLegacyInvoiceRuntime(unittest.TestCase):
    def make_invoice_fixture(self, root: Path) -> Path:
        from tests.test_source_invoice_explorer import TestSourceInvoiceExplorerContract

        return TestSourceInvoiceExplorerContract().make_snapshot(root)

    def make_item_manifest(self, root: Path) -> Path:
        """Build an accepted artifact that also happens to satisfy the source-invoice
        schema, so a fallback wiring bug (mounting source_invoice from the accepted
        descriptor alone) would be exercised rather than accidentally missed."""
        from apc_core.snapshot_contract import certify_snapshot

        source = root / "accepted-items.sqlite"
        connection = sqlite3.connect(source)
        connection.execute(
            'CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)'
        )
        connection.execute('INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?)', ("IT-001", "Item", "สินค้า", "Fish", "Tropical"))
        connection.execute(
            'CREATE TABLE "MainDB__INVOICE" ('
            '"Inv No" TEXT, "Cust ID" TEXT, "Date" TEXT, "AWB" TEXT, "ShipBy" TEXT, "ShipBy2" TEXT, "Box" TEXT, '
            '"Total Amt" TEXT, "Total Qty" TEXT, "Total QtyTC" TEXT, "Total QtyCHV" TEXT, "XRate" TEXT, '
            '"Consignee" TEXT, "Province" TEXT, "Country" TEXT, "Time" TEXT, "Time2" TEXT, "Broker" TEXT)'
        )
        connection.execute(
            'CREATE TABLE "MainDB__INV_ITEM" ('
            '"Inv No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Description" TEXT, "Qty" TEXT, "Price" TEXT, "Amount" TEXT, "SubCust" TEXT)'
        )
        connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Price Type" TEXT, "Name" TEXT)')
        connection.execute(
            'INSERT INTO "MainDB__INVOICE" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ("ACCEPTED//001", "C-001", "2026-09-01", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""),
        )
        connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?, ?)', ("C-001", "PT", "Accepted customer"))
        connection.commit()
        connection.close()
        manifest_path = root / "state" / "accepted_snapshot.json"
        certify_snapshot(source, manifest_path, "2026-09-02T00:00:00Z")
        return manifest_path

    def make_plain_item_manifest(self, root: Path) -> Path:
        """A minimal accepted artifact with no customer/order/AWB capability, so this
        test exercises only the legacy-invoice path and not unrelated Core modules."""
        from apc_core.snapshot_contract import certify_snapshot

        source = root / "accepted-items.sqlite"
        connection = sqlite3.connect(source)
        connection.execute(
            'CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)'
        )
        connection.execute('INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?)', ("IT-001", "Item", "สินค้า", "Fish", "Tropical"))
        connection.commit()
        connection.close()
        manifest_path = root / "state" / "accepted_snapshot.json"
        certify_snapshot(source, manifest_path, "2026-09-02T00:00:00Z")
        return manifest_path

    def test_explicit_verified_snapshot_wires_a_reader_the_accepted_artifact_alone_cannot_provide(self):
        """Only an explicit verified staged snapshot may mount source_invoice routes (PR #38 blocker 1)."""
        from apc_core import server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = self.make_item_manifest(root)
            legacy_source = self.make_invoice_fixture(root / "legacy")
            legacy_digest = hashlib.sha256(legacy_source.read_bytes()).hexdigest()

            item_explorer, _customers, _prices, _orders, _awb, _invoice_source, _invoice_drafts, _manifest = server.load_accepted_customer_price_order_runtime(
                manifest_path, data_dir=root / "core-state",
                legacy_invoice_snapshot=legacy_source, legacy_invoice_sha256=legacy_digest,
            )
            try:
                self.assertIsNotNone(item_explorer.source_invoice_explorer)
                self.assertEqual(
                    "source_invoice",
                    item_explorer.source_invoice_explorer.search_invoices(prefix="C//")["invoices"][0]["source_type"],
                )
            finally:
                item_explorer.close()

            no_snapshot_explorer, *_rest = server.load_accepted_customer_price_order_runtime(
                manifest_path, data_dir=root / "core-state-2",
            )
            try:
                self.assertIsNone(no_snapshot_explorer.source_invoice_explorer)
            finally:
                no_snapshot_explorer.close()

    def test_legacy_invoice_snapshot_mode_never_touches_core_sqlite_via_the_shared_staff_endpoint(self):
        """PR #38 blocker 3: browsing legacy invoices must not migrate/seed the Core staff registry or create Core SQLite."""
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer

        from apc_core import server
        from apc_core.active_staff_provider import ActiveStaffProvider
        from apc_core.core_staff_registry import CURRENT_IDENTITY_STAFF
        from apc_core.item_explorer import make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = self.make_plain_item_manifest(root)
            legacy_source = self.make_invoice_fixture(root / "legacy")
            legacy_digest = hashlib.sha256(legacy_source.read_bytes()).hexdigest()
            data_dir = root / "core-state"

            item_explorer, customers, prices, orders, awb, _invoice_source, _invoice_drafts, manifest = server.load_accepted_customer_price_order_runtime(
                manifest_path, data_dir=data_dir,
                legacy_invoice_snapshot=legacy_source, legacy_invoice_sha256=legacy_digest,
            )
            handler = make_handler(
                item_explorer, manifest, customers, prices, orders, awb,
                source_invoice_explorer=item_explorer.source_invoice_explorer,
                accepted_snapshot_sha256=manifest["accepted_artifact_sha256"],
                identity_staff_provider=ActiveStaffProvider(CURRENT_IDENTITY_STAFF),
            )
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=http_server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = http_server.server_address
                for path in (
                    "/order-invoice/",
                    "/api/staff",
                    "/order-invoice/api/browse?type=source_invoice&query=C%2F%2F&limit=1&offset=0",
                ):
                    connection = HTTPConnection(host, port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    self.assertLess(response.status, 500, path)
            finally:
                http_server.shutdown()
                http_server.server_close()
                item_explorer.close()

            self.assertFalse((data_dir / "apc_core.sqlite").exists())

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

    def test_main_wires_a_fixture_staff_provider_only_in_explicit_legacy_invoice_snapshot_mode(self):
        """PR #38 blocker 3: the shared staff endpoint must not require Core SQLite when legacy invoices are used."""
        import sys
        from unittest.mock import Mock, patch

        from apc_core import server
        from apc_core.active_staff_provider import ActiveStaffProvider
        from apc_core.core_staff_registry import CURRENT_IDENTITY_STAFF

        items = Mock()
        manifest = {"accepted": True, "accepted_artifact_sha256": "a" * 64}
        handler = object()
        created_servers = []

        class FakeServer:
            def __init__(self, address, supplied_handler):
                self.address = address
                self.supplied_handler = supplied_handler
                created_servers.append(self)

            def serve_forever(self):
                return None

        argv = [
            "server", "--manifest", "accepted.json",
            "--legacy-invoice-snapshot", "legacy.sqlite", "--legacy-invoice-sha256", "a" * 64,
        ]
        with patch.object(sys, "argv", argv), \
             patch.object(server, "load_accepted_customer_price_order_runtime", return_value=(items, None, None, None, None, None, None, manifest)) as loader, \
             patch.object(server, "make_handler", return_value=handler) as handler_factory, \
             patch.object(server, "ThreadingHTTPServer", FakeServer):
            server.main()

        loader.assert_called_once_with(
            Path("accepted.json"), data_dir=None, with_invoice_drafts=False,
            legacy_invoice_snapshot=Path("legacy.sqlite"), legacy_invoice_sha256="a" * 64,
        )
        handler_factory.assert_called_once_with(
            items, manifest, None, None, None, None,
            invoice_source=None, invoice_draft_service=None, accepted_snapshot_sha256=manifest["accepted_artifact_sha256"],
            customer_lan_ingress=False, allowed_mutation_origins=None,
            recovery_authorizer=None, recovery_service=None, recovery_maintenance=None,
            identity_staff_provider=ActiveStaffProvider(CURRENT_IDENTITY_STAFF),
        )
        self.assertEqual(1, len(created_servers))
        items.close.assert_called_once_with()

    def test_workspace_uses_exact_legacy_invoices_read_only_copy_without_new_actions(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn("Legacy Invoices · Read-only", html)
        self.assertIn("LEGACY INVOICES · READ-ONLY", html)
        self.assertNotIn("Print invoice", html)
        self.assertNotIn("Delete invoice", html)
        self.assertNotIn("Save invoice", html)
