"""Synthetic INV-2C reconciliation evidence for the existing draft-only contracts."""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from apc_core.invoice_conversion_source import InvoiceConversionSource
from apc_core.invoice_draft_service import InvoiceDraftService
from apc_core.invoice_drafts import InvoiceDraftStore
from apc_core.item_explorer import ItemExplorer, make_handler


class InvoiceDraftReconciliationTests(unittest.TestCase):
    """All fixtures are synthetic and AWB evidence remains opaque display-only text."""

    def fixture(self, root: Path) -> Path:
        source = root / "synthetic-accepted.sqlite"
        connection = sqlite3.connect(source)
        connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)')
        connection.executemany('INSERT INTO "MainDB__ITEM" VALUES (?,?,?,?,?)', [("IT-A", "Synthetic A", "", "Fish", "Tropical"), ("IT-B", "Synthetic B", "", "Fish", "Tropical")])
        connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
        connection.execute('INSERT INTO "MainDB__CUST" VALUES (?,?)', ("C-SAFE", "Synthetic Customer"))
        connection.execute('CREATE TABLE "MainDB__ORDER" ("Order No" TEXT, "Order Date" TEXT, "Cust ID" TEXT, "Shipment Date" TEXT, "AWB" TEXT)')
        connection.executemany('INSERT INTO "MainDB__ORDER" VALUES (?,?,?,?,?)', [("ORD-SAFE-1", "2026-08-01", "C-SAFE", "2026-08-02", "AWB-OPAQUE-A"), ("ORD-SAFE-2", "2026-08-01", "C-SAFE", "2026-08-02", "AWB-OPAQUE-B")])
        connection.execute('CREATE TABLE "MainDB__ORDER_ITEM" ("Order No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Qty" TEXT, "Unit Price" TEXT, "AWB" TEXT)')
        connection.executemany('INSERT INTO "MainDB__ORDER_ITEM" VALUES (?,?,?,?,?,?)', [("ORD-SAFE-1", "L-01", "IT-A", "2", "10.00", "AWB-OPAQUE-A"), ("ORD-SAFE-2", "L-02", "IT-B", "3", "11.00", "AWB-OPAQUE-B")])
        connection.commit()
        connection.close()
        return source

    def serve(self, root: Path):
        source = self.fixture(root)
        explorer = ItemExplorer(source, data_dir=root / "core-state")
        explorer._local_store().active_staff()
        invoice_source = InvoiceConversionSource(source, current_price_lookup=lambda *_: {"status": "KNOWN", "value": "10.00"})
        service = InvoiceDraftService(InvoiceDraftStore(root / "core-state"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}, invoice_source=invoice_source, invoice_draft_service=service, accepted_snapshot_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), allowed_mutation_origins=frozenset({"http://program.test"})))
        worker = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(invoice_source.close)
        self.addCleanup(service.store.close)
        self.addCleanup(explorer.close)
        return server.server_address, service

    @staticmethod
    def request(address, path, body):
        connection = HTTPConnection(*address, timeout=3)
        connection.request("POST", path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Origin": "http://program.test"})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_multi_order_opaque_awb_conflict_blocks_then_persists_exact_selection_and_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            address, service = self.serve(Path(temporary))
            selected = ["ORD-SAFE-1", "ORD-SAFE-2"]
            status, blocked = self.request(address, "/invoices/api/previews", {"selected_order_ids": selected, "decisions": []})
            self.assertEqual(200, status)
            self.assertFalse(blocked["proposal"]["ready_to_save"])
            self.assertIn("selected:awb", {entry["conflict_id"] for entry in blocked["proposal"]["unresolved"]})
            self.assertNotIn("AWB-OPAQUE-A", json.dumps(blocked["proposal"]))
            self.assertNotIn("AWB-OPAQUE-B", json.dumps(blocked["proposal"]))
            self.assertEqual(0, service.store.audit_count())

            decision = {"conflict_id": "selected:awb", "chosen_existing_value": "AWB-OPAQUE-A", "chosen_existing_source": "ORD-SAFE-1:awb"}
            status, ready = self.request(address, "/invoices/api/previews", {"selected_order_ids": selected, "decisions": [decision]})
            self.assertEqual(200, status)
            self.assertTrue(ready["proposal"]["ready_to_save"])
            self.assertNotIn("AWB-OPAQUE-A", json.dumps(ready["proposal"]))
            self.assertNotIn("AWB-OPAQUE-B", json.dumps(ready["proposal"]))
            status, saved = self.request(address, "/invoices/api/drafts", {"preview_ref": ready["preview_ref"], "actor": "WAT"})
            self.assertEqual(201, status)
            self.assertEqual(selected, saved["selected_order_ids"])
            self.assertEqual([("ORD-SAFE-1", "L-01"), ("ORD-SAFE-2", "L-02")], [(line["order_id"], line["line_ref"]) for line in saved["lines"]])

    def test_repeated_ready_request_returns_original_draft_without_duplicate_allocations_or_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            address, service = self.serve(Path(temporary))
            payload = {"selected_order_ids": ["ORD-SAFE-1"], "decisions": []}
            _, first_preview = self.request(address, "/invoices/api/previews", payload)
            first_status, first = self.request(address, "/invoices/api/drafts", {"preview_ref": first_preview["preview_ref"], "actor": "WAT"})
            _, replay_preview = self.request(address, "/invoices/api/previews", payload)
            replay_status, replay = self.request(address, "/invoices/api/drafts", {"preview_ref": replay_preview["preview_ref"], "actor": "WAT"})
            self.assertEqual((201, 201), (first_status, replay_status))
            self.assertEqual(first, replay)
            self.assertEqual(1, service.store.connection.execute("SELECT COUNT(*) FROM invoice_drafts").fetchone()[0])
            self.assertEqual(1, service.store.connection.execute("SELECT COUNT(*) FROM invoice_line_allocations").fetchone()[0])
            self.assertEqual(1, service.store.audit_count())

    def test_source_hash_mismatch_leaves_invoice_save_and_ready_capabilities_unavailable(self):
        from apc_core import server

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "synthetic-accepted.sqlite"
            accepted.write_bytes(b"synthetic accepted artifact")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {"accepted_artifact_sha256": "a" * 64, "capabilities": {}}
            items, mismatched_source = Mock(), Mock(source_sha256="b" * 64)
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items), patch.object(server.InvoiceConversionSource, "from_open_descriptor", return_value=mismatched_source), patch.object(server, "InvoiceDraftService") as service:
                result = server.load_accepted_customer_price_order_runtime(root / "ignored", data_dir=root / "core-state", with_invoice_drafts=True)
            self.assertIsNone(result[5])
            self.assertIsNone(result[6])
            service.assert_not_called()
            mismatched_source.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
