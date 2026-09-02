import hashlib
import inspect
import sqlite3
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path


class CoreInvoiceReadTestServerTests(unittest.TestCase):
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

    @staticmethod
    def _request(port: int):
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/private-invoice-read/?actor=WAT")
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_composes_the_unmounted_factory_directly_into_a_loopback_test_server(self):
        from tests.core_invoice_read_test_server import core_invoice_read_test_server
        import tests.core_invoice_read_test_server as module

        with tempfile.TemporaryDirectory() as temporary:
            database = self._database(Path(temporary))
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            with core_invoice_read_test_server(database, active_staff=(("WAT", "Office"),)) as server:
                status, body = self._request(server.port)

            self.assertEqual(200, status)
            self.assertIn("ACME-T26-001", body.decode("utf-8"))
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())
            self.assertFalse(any(candidate.exists() for candidate in (
                database.with_name(database.name + "-wal"),
                database.with_name(database.name + "-shm"),
                database.with_name(database.name + "-journal"),
            )))
            source = inspect.getsource(module)
            for forbidden in ("apc_core.server", "/invoices/", "/drafts/", ".do_GET("):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
