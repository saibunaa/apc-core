import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from apc_core.invoice_conversion_source import InvoiceConversionSource
from apc_core.invoice_draft_builder import build_invoice_draft
from apc_core.invoice_draft_service import InvoiceDraftService
from apc_core.invoice_drafts import InvoiceDraftStore
from apc_core.item_explorer import ItemExplorer, make_handler


class TestInvoiceDraftRoutes(unittest.TestCase):
    """INV-2A is a narrow source-evidence/preview/save boundary only."""

    def fixture(self, root: Path) -> Path:
        source = root / "accepted.sqlite"
        con = sqlite3.connect(source)
        con.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)')
        con.execute('INSERT INTO "MainDB__ITEM" VALUES (?,?,?,?,?)', ("IT-1", "Item", "", "Fish", "Tropical"))
        con.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
        con.execute('INSERT INTO "MainDB__CUST" VALUES (?,?)', ("C-1", "Customer"))
        con.execute('CREATE TABLE "MainDB__ORDER" ("Order No" TEXT, "Order Date" TEXT, "Cust ID" TEXT, "Shipment Date" TEXT, "AWB" TEXT)')
        con.execute('INSERT INTO "MainDB__ORDER" VALUES (?,?,?,?,?)', ("ORD-1", "2026-08-01", "C-1", "2026-08-02", "AWB-OPAQUE"))
        con.execute('CREATE TABLE "MainDB__ORDER_ITEM" ("Order No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Qty" TEXT, "Unit Price" TEXT)')
        con.execute('INSERT INTO "MainDB__ORDER_ITEM" VALUES (?,?,?,?,?)', ("ORD-1", "1", "IT-1", "2", "10.00"))
        con.commit(); con.close()
        return source

    def serve(self, root: Path, *, available: bool = True):
        source = self.fixture(root)
        explorer = ItemExplorer(source, data_dir=root / "core-state")
        # Establish the pre-existing active-actor registry before mutation assertions.
        explorer._local_store().active_staff()
        self.addCleanup(explorer.close)
        kwargs = {"allowed_mutation_origins": frozenset({"http://program.test"})}
        if available:
            invoice_source = InvoiceConversionSource(source, current_price_lookup=lambda customer_id, item_id: {"status": "KNOWN", "value": "10.00"})
            service = InvoiceDraftService(InvoiceDraftStore(root / "core-state"))
            kwargs.update(invoice_source=invoice_source, invoice_draft_service=service, accepted_snapshot_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
            self.addCleanup(invoice_source.close)
            self.addCleanup(service.store.close)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}, **kwargs))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return server.server_address, source, root / "core-state" / "apc_core.sqlite"

    @staticmethod
    def request(address, method, path, body=None, headers=None):
        conn = HTTPConnection(*address, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, response.getheaders(), response.read()
        conn.close()
        return result

    @staticmethod
    def valid_preview(snapshot: str):
        return build_invoice_draft(
            {"accepted_snapshot_sha256": snapshot},
            [{"order_id": "ORD-1", "customer_id": "C-1", "document_family": "legacy-order", "lines": [{"line_ref": "1", "item_id": "IT-1", "quantity": "2"}], "shipment_conflicts": []}],
            ["ORD-1"], [],
        )

    def test_routes_are_absent_without_a_constructed_source_or_service_and_menu_has_no_invoice_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            address, _, _ = self.serve(Path(temporary), available=False)
            status, _, body = self.request(address, "GET", "/")
            self.assertEqual(200, status)
            self.assertNotIn(b"Invoice", body)
            for path in ("/invoices/api/candidates?customer_id=C-1&shipment_date=2026-08-02", "/invoices/api/preview", "/invoices/api/drafts", "/invoices/api/drafts/x"):
                self.assertEqual(404, self.request(address, "GET", path)[0])

    def test_candidates_are_explicit_read_only_and_never_awb_linked_or_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            address, source, state = self.serve(root)
            before_source = hashlib.sha256(source.read_bytes()).hexdigest()
            before_state = hashlib.sha256(state.read_bytes()).hexdigest()
            self.assertEqual(400, self.request(address, "GET", "/invoices/api/candidates")[0])
            status, headers, body = self.request(address, "GET", "/invoices/api/candidates?customer_id=C-1&shipment_date=2026-08-02")
            self.assertEqual(200, status)
            self.assertIn(("Cache-Control", "no-store"), headers)
            payload = json.loads(body)
            self.assertEqual(["kind", "limit", "candidates"], list(payload))
            self.assertEqual("ORD-1", payload["candidates"][0]["order_id"])
            self.assertEqual("AWB-OPAQUE", payload["candidates"][0]["awb"])
            self.assertNotIn("selected_order_ids", payload)
            self.assertEqual(before_source, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(before_state, hashlib.sha256(state.read_bytes()).hexdigest())

    def test_invoice_page_and_api_are_denied_before_rendering_for_nonpermitted_clients(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixture(root)
            explorer = ItemExplorer(source, data_dir=root / "core-state")
            self.addCleanup(explorer.close)
            invoice_source = InvoiceConversionSource(source)
            service = InvoiceDraftService(InvoiceDraftStore(root / "core-state"))
            self.addCleanup(invoice_source.close)
            self.addCleanup(service.store.close)
            handler_class = make_handler(
                explorer,
                {"accepted": True},
                invoice_source=invoice_source,
                invoice_draft_service=service,
                accepted_snapshot_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            for path in ("/invoices/", "/invoices/api/candidates?customer_id=C-1&shipment_date=2026-08-02"):
                denied = object.__new__(handler_class)
                denied.client_address = ("100.69.141.75", 1)
                denied.path = path
                denied.wfile = io.BytesIO()
                statuses = []
                denied.send_response = statuses.append
                denied.send_header = lambda *_: None
                denied.end_headers = lambda: None
                handler_class.do_GET(denied)
                self.assertEqual([403], statuses)

    def test_preview_is_post_only_explicit_selection_and_raw_proposal_cannot_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            address, _, state = self.serve(Path(temporary))
            before_state = hashlib.sha256(state.read_bytes()).hexdigest()
            self.assertEqual(404, self.request(address, "GET", "/invoices/api/preview?order_id=ORD-1")[0])
            headers = {"Content-Type": "application/json", "Origin": "http://program.test"}
            self.assertEqual(400, self.request(address, "POST", "/invoices/api/previews", json.dumps({"selected_order_ids": []}).encode(), headers)[0])
            status, _, body = self.request(address, "POST", "/invoices/api/previews", json.dumps({"selected_order_ids": ["ORD-1"], "decisions": []}).encode(), headers)
            self.assertEqual(200, status)
            preview = json.loads(body)
            self.assertEqual({"preview_ref", "proposal"}, set(preview))
            self.assertNotIn("ORD-1", preview["preview_ref"])
            self.assertEqual(before_state, hashlib.sha256(state.read_bytes()).hexdigest())
            self.assertEqual(400, self.request(address, "POST", "/invoices/api/drafts", json.dumps({"proposal": preview["proposal"], "actor": "WAT"}).encode(), headers)[0])
            saved = self.request(address, "POST", "/invoices/api/drafts", json.dumps({"preview_ref": preview["preview_ref"], "actor": "WAT"}).encode(), headers)
            self.assertEqual(201, saved[0])
            self.assertEqual(400, self.request(address, "POST", "/invoices/api/drafts", json.dumps({"preview_ref": preview["preview_ref"], "actor": "WAT"}).encode(), headers)[0])
            for method in ("PUT", "PATCH", "DELETE"):
                self.assertEqual(405, self.request(address, method, "/invoices/api/drafts")[0])

    def test_preview_order_count_is_bounded_before_source_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            address, _, _ = self.serve(Path(temporary))
            body = json.dumps({"selected_order_ids": [f"ORD-{value}" for value in range(21)], "decisions": []})
            status, _, _ = self.request(address, "POST", "/invoices/api/previews", body, {"Content-Type": "application/json", "Origin": "http://program.test"})
            self.assertEqual(400, status)

    def test_save_is_origin_and_actor_guarded_and_returns_only_allowlisted_draft_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            address, source, state = self.serve(Path(temporary))
            headers = {"Content-Type": "application/json", "Origin": "http://program.test"}
            preview_status, _, preview_body = self.request(address, "POST", "/invoices/api/previews", json.dumps({"selected_order_ids": ["ORD-1"], "decisions": []}).encode(), headers)
            self.assertEqual(200, preview_status)
            body = json.dumps({"preview_ref": json.loads(preview_body)["preview_ref"], "actor": "WAT"}).encode()
            before = hashlib.sha256(state.read_bytes()).hexdigest()
            invalid_body = json.dumps({"preview_ref": "not-issued", "actor": "UNKNOWN"}).encode()
            for headers in ({"Content-Type": "text/plain"}, {"Content-Type": "application/json"}, {"Content-Type": "application/json", "Origin": "https://hostile.test"}, {"Content-Type": "application/json", "Origin": "http://program.test"}):
                candidate = invalid_body if headers.get("Content-Type") == "application/json" else b"{}"
                self.assertIn(self.request(address, "POST", "/invoices/api/drafts", candidate, headers)[0], (400, 403, 415))
            self.assertEqual(before, hashlib.sha256(state.read_bytes()).hexdigest())
            status, _, response = self.request(address, "POST", "/invoices/api/drafts", body, {"Content-Type": "application/json", "Origin": "http://program.test"})
            self.assertEqual(201, status)
            saved = json.loads(response)
            self.assertEqual({"draft_id", "accepted_snapshot_sha256", "created_by", "created_at", "status", "selected_order_ids", "lines"}, set(saved))
            self.assertEqual("WAT", saved["created_by"])
            self.assertNotEqual(before, hashlib.sha256(state.read_bytes()).hexdigest())

    def test_no_detail_list_or_issuance_print_export_sync_or_awb_write_surface_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            address, _, _ = self.serve(Path(temporary))
            for path in ("/invoices/api/drafts/x", "/invoices/api/drafts", "/invoices/api/issue", "/invoices/api/print", "/invoices/api/export", "/invoices/api/sync", "/invoices/api/awb"):
                self.assertEqual(404, self.request(address, "GET", path)[0])
