import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path


class CoreInvoiceReadPageTests(unittest.TestCase):
    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _sidecars(path: Path) -> tuple[Path, Path, Path]:
        return (
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
            path.with_name(path.name + "-journal"),
        )

    @staticmethod
    def _database(root: Path) -> Path:
        from apc_core.core_invoices import CoreInvoiceStore, CoreInvoiceWorkflowStore
        from apc_core.core_orders import CoreOrderStore
        from apc_core.core_provenance import apply_core_invoice_workflow_migrations

        database = root / "local-p5-fixture.sqlite"
        apply_core_invoice_workflow_migrations(database)
        snapshot = "a" * 64
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO core_source_snapshots(snapshot_sha256,artifact_path,imported_at) VALUES (?,?,?)",
                (snapshot, "/fixture/accepted.sqlite", "2026-09-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO core_source_rows(snapshot_sha256,source_table,source_rowid,source_kind,document_id,line_label,item_id,quantity,evidence_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (snapshot, "MainDB__ORDER_ITEM", 1, "order_item", "ORD-ACME-441", "1", "Tube", "12", "{}"),
            )
        orders = CoreOrderStore(database)
        try:
            orders.create_order("order-1", "WAT", "order-key", [{
                "line_id": "line-1", "snapshot_sha256": snapshot,
                "source_table": "MainDB__ORDER_ITEM", "source_rowid": 1,
            }])
        finally:
            orders.close()
        base = CoreInvoiceStore(database)
        try:
            base.create_invoice("base-1", "WAT", "base-key", ["line-1"], expected_version=0)
        finally:
            base.close()
        workflow = CoreInvoiceWorkflowStore(database)
        try:
            workflow.create_temporary_invoice(
                "doc-1", "base-1", "WAT", "ACME", {"line-1": "12.50"}, "doc-key",
                expected_version=0, consignee="ACME Receiving", delivery_reference="PO-441",
                reference_year=2026,
            )
        finally:
            workflow.close()
        return database

    def _server(self, database: Path):
        from apc_core.core_invoice_read_page import make_core_invoice_read_handler

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_core_invoice_read_handler(database, active_staff=(("WAT", "Office"), ("YIM", "Office"))),
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return int(server.server_address[1])

    @staticmethod
    def _request(port: int, method: str, path: str):
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def test_page_is_a_separate_unmounted_get_only_local_read_surface_using_the_shared_picker(self):
        from apc_core.core_invoice_read_page import make_core_invoice_read_handler
        import apc_core.server as server_module
        import inspect

        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            before = self._sha256(database)
            port = self._server(database)
            status, headers, body = self._request(port, "GET", "/private-invoice-read/?actor=WAT")

            self.assertEqual(200, status)
            self.assertEqual("no-store", headers["Cache-Control"])
            html = body.decode("utf-8")
            for marker in (
                'id="identity-picker"', 'id="identity-change-user"', 'window.apcCoreActiveStaff',
                "Invoice list", "ACME-T26-001", "ACME Receiving", "PO-441",
                "Search by customer code, invoice reference, or order number.",
            ):
                self.assertIn(marker, html)
            for forbidden in ("<form", "Save", "Make real", "Cancel", "Correct", "POST", "Print", "Export"):
                self.assertNotIn(forbidden, html)
            self.assertNotIn("core_invoice_read_page", inspect.getsource(server_module))
            self.assertNotIn("/invoices/", inspect.getsource(make_core_invoice_read_handler))
            self.assertNotIn("/drafts/", inspect.getsource(make_core_invoice_read_handler))
            self.assertEqual(before, self._sha256(database))
            self.assertFalse(any(path.exists() for path in self._sidecars(database)))

    def test_page_rejects_non_loopback_and_invalid_or_missing_staff_before_opening_the_database(self):
        from apc_core.core_invoice_read_page import make_core_invoice_read_handler

        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            handler_class = make_core_invoice_read_handler(database, active_staff=(("WAT", "Office"),))
            request = object.__new__(handler_class)
            request.client_address = ("192.168.1.42", 1)
            request.path = "/private-invoice-read/?actor=WAT"
            request.headers = {}
            response_body = io.BytesIO()
            request.wfile = response_body
            statuses = []
            request.send_response = statuses.append
            request.send_header = lambda *_: None
            request.end_headers = lambda: None
            with patch("apc_core.core_invoice_read_page.open_core_invoice_read_connection", side_effect=AssertionError("database must not open")) as opener:
                request.do_GET()
                self.assertEqual([403], statuses)
                self.assertEqual({"error": "loopback access required"}, json.loads(response_body.getvalue()))

                port = self._server(database)
                for path in ("/private-invoice-read/", "/private-invoice-read/?actor=UNKNOWN", "/private-invoice-read/?actor=WAT&actor=YIM"):
                    status, _, _ = self._request(port, "GET", path)
                    self.assertEqual(403, status, path)
                opener.assert_not_called()

    def test_all_non_get_methods_are_rejected_without_mutating_database_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            before = self._sha256(database)
            port = self._server(database)
            with patch("apc_core.core_invoice_read_page.open_core_invoice_read_connection", side_effect=AssertionError("database must not open")) as opener:
                for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"):
                    status, _, _ = self._request(port, method, "/private-invoice-read/?actor=WAT")
                    self.assertEqual(405, status, method)
                opener.assert_not_called()
            self.assertEqual(before, self._sha256(database))
            self.assertFalse(any(path.exists() for path in self._sidecars(database)))

    def test_concurrent_gets_open_independent_read_connections_and_leave_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            before = self._sha256(database)
            port = self._server(database)
            outcomes = []
            lock = threading.Lock()

            def fetch():
                try:
                    status, _, body = self._request(port, "GET", "/private-invoice-read/?actor=WAT")
                    result = (status, "ACME-T26-001" in body.decode("utf-8"))
                except Exception as error:  # assertion below makes failures visible.
                    result = error
                with lock:
                    outcomes.append(result)

            workers = [threading.Thread(target=fetch) for _ in range(12)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual([(200, True)] * 12, outcomes)
            self.assertEqual(before, self._sha256(database))


if __name__ == "__main__":
    unittest.main()
