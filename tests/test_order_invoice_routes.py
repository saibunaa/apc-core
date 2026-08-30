import inspect
import io
import json
import os
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import Mock, patch


class _BrowseOrderSource:
    def __init__(self, row=None):
        self.calls = 0
        self.open_calls = 0
        self.row = row or {"order_id": "ORD//2026/001", "order_date": "2026-08-29", "customer_id": "C/001"}

    def open_order(self, order_id, *, limit, offset):
        self.open_calls += 1
        assert (order_id, limit, offset) == ("ORD//2026/001", 2, 0)
        return {
            "order_id": order_id, "order_date": "2026-08-29", "customer_id": "C/001", "customer_name": "Customer One",
            "lines": [
                {"line_no": "0", "item_id": "ITEM-0", "qty": "1", "description_th": "ไทย", "reference": "", "description_en": "English", "is_annotation": False},
                {"line_no": "1", "item_id": "ITEM-1", "qty": "2", "description_th": "ไทย 2", "reference": "", "description_en": "English 2", "is_annotation": False},
            ],
            "total": 1661, "limit": limit, "offset": offset, "has_more": True, "next_offset": 2,
        }

    def browse_orders(self, query, *, limit, offset):
        self.calls += 1
        assert (query, limit, offset) == ("ORD//", 1, 0)
        return {"total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None, "orders": [self.row]}


class _BrowseInvoiceSource:
    def __init__(self):
        self.calls = 0

    def search_invoices(self, *, prefix, limit, offset):
        self.calls += 1
        assert (prefix, limit, offset) == ("C//", 1, 0)
        return {
            "total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None,
            "invoices": [{
                "source_type": "source_invoice", "invoice_id": "C//2026/001", "invoice_date": "2026-08-29",
                "customer_id": "C/001", "customer_name": "Customer One", "slash_family": "repeated_slash",
            }],
        }


class _BrowseDraftStore:
    def __init__(self):
        self.calls = 0

    def list_drafts(self, query, *, limit, offset):
        self.calls += 1
        assert (query, limit, offset) == ("draft-", 1, 0)
        return {
            "total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None,
            "drafts": [{
                "draft_id": "draft-001", "created_by": "YIM", "created_at": "2026-08-29 10:00:00", "status": "draft",
                "accepted_snapshot_sha256": "a" * 64,
            }],
        }


class TestOrderInvoiceBrowseRoute(unittest.TestCase):
    def handler(self, *, order_source=None, source_invoice_explorer=None, invoice_draft_service=None,
                customer_lan_ingress=False):
        from apc_core.item_explorer import make_handler

        handler_class = make_handler(
            object(), {}, order_explorer=order_source or _BrowseOrderSource(),
            source_invoice_explorer=source_invoice_explorer, invoice_draft_service=invoice_draft_service,
            customer_lan_ingress=customer_lan_ingress,
        )
        request = object.__new__(handler_class)
        request.client_address = ("127.0.0.1", 1)
        request.headers = {}
        request.wfile = io.BytesIO()
        statuses = []
        request.send_response = statuses.append
        request.send_header = lambda *_: None
        request.end_headers = lambda: None
        return request, statuses

    def get(self, path, **handler_kwargs):
        request, statuses = self.handler(**handler_kwargs)
        request.path = path
        request.do_GET()
        return statuses, json.loads(request.wfile.getvalue())

    def test_browse_denies_private_lan_client_without_customer_lan_ingress_before_reading(self):
        source = _BrowseOrderSource()
        request, statuses = self.handler(order_source=source)
        request.client_address = ("192.168.1.42", 1)
        request.path = "/order-invoice/api/browse?type=source_order&query=ORD//&limit=1&offset=0"

        request.do_GET()

        self.assertEqual([HTTPStatus.FORBIDDEN], statuses)
        self.assertEqual(
            {"error": "customer access is loopback-only unless customer LAN ingress is enabled"},
            json.loads(request.wfile.getvalue()),
        )
        self.assertEqual(0, source.calls)

    def test_browse_allows_private_lan_client_when_customer_lan_ingress_is_enabled(self):
        source = _BrowseOrderSource()
        request, statuses = self.handler(order_source=source, customer_lan_ingress=True)
        request.client_address = ("192.168.1.42", 1)
        request.path = "/order-invoice/api/browse?type=source_order&query=ORD//&limit=1&offset=0"

        request.do_GET()

        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertEqual(1, source.calls)

    def test_open_source_order_denies_private_lan_before_reader_open(self):
        source = _BrowseOrderSource()
        request, statuses = self.handler(order_source=source)
        request.client_address = ("192.168.1.42", 1)
        request.path = "/order-invoice/api/source-orders/ORD//2026/001?limit=2&offset=0"

        request.do_GET()

        self.assertEqual([HTTPStatus.FORBIDDEN], statuses)
        self.assertEqual(
            {"error": "customer access is loopback-only unless customer LAN ingress is enabled"},
            json.loads(request.wfile.getvalue()),
        )
        self.assertEqual(0, source.open_calls)

    def test_open_source_order_line_page_is_closed_read_only_and_preserves_exact_slashes(self):
        source = _BrowseOrderSource()
        statuses, payload = self.get(
            "/order-invoice/api/source-orders/ORD//2026/001?limit=2&offset=0", order_source=source
        )

        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertEqual(1, source.open_calls)
        self.assertEqual(
            {"record_type", "record_id", "order_id", "order_date", "customer_id", "customer_name", "lines", "total", "limit", "offset", "has_more", "next_offset"},
            set(payload),
        )
        self.assertEqual("source_order:ORD//2026/001", payload["record_id"])
        self.assertEqual(1661, payload["total"])
        self.assertEqual(2, len(payload["lines"]))
        self.assertEqual(
            {"line_no", "item_id", "qty", "description_th", "reference", "description_en", "is_annotation"},
            set(payload["lines"][0]),
        )
        self.assertNotIn("source_sha256", repr(payload))
        self.assertNotIn("provenance", repr(payload).lower())

    def test_open_source_order_accepts_actual_order_explorer_line_contract(self):
        from apc_core.order_explorer import OrderExplorer
        from tests.test_order_explorer import TestOrderExplorerContract

        with tempfile.TemporaryDirectory() as tmp:
            source = OrderExplorer(TestOrderExplorerContract().make_snapshot(Path(tmp)))
            try:
                statuses, payload = self.get(
                    "/order-invoice/api/source-orders/ORD%2F2026%2F001?limit=2&offset=0", order_source=source
                )
            finally:
                source.close()

        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertEqual(["2", "3"], [line["line_no"] for line in payload["lines"]])
        self.assertEqual(
            {"line_no", "item_id", "qty", "description_th", "reference", "description_en", "is_annotation"},
            set(payload["lines"][0]),
        )

    def test_open_source_order_rejects_bad_page_parameters_before_reading(self):
        source = _BrowseOrderSource()
        statuses, payload = self.get(
            "/order-invoice/api/source-orders/ORD//2026/001?limit=251&offset=0", order_source=source
        )

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid order detail query"}, payload)
        self.assertEqual(0, source.open_calls)

    def test_open_source_order_rejects_sqlite_overflow_offset_before_reader_open(self):
        source = _BrowseOrderSource()
        statuses, payload = self.get(
            "/order-invoice/api/source-orders/ORD//2026/001?limit=2&offset=9223372036854775808", order_source=source
        )

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid order detail query"}, payload)
        self.assertEqual(0, source.open_calls)

    def test_browse_rejects_sqlite_overflow_offset_before_reader_access(self):
        source = _BrowseOrderSource()
        statuses, payload = self.get(
            "/order-invoice/api/browse?type=source_order&query=ORD&limit=50&offset=9223372036854775808", order_source=source
        )

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid browse query"}, payload)
        self.assertEqual(0, source.calls)

    def test_browse_source_order_returns_a_closed_type_specific_dto(self):
        statuses, payload = self.get("/order-invoice/api/browse?type=source_order&query=ORD//&limit=1&offset=0")

        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertEqual({"record_type", "total", "limit", "offset", "has_more", "next_offset", "results"}, set(payload))
        self.assertEqual("source_order", payload["record_type"])
        self.assertEqual({"record_type", "record_id", "order_id", "order_date", "customer_id"}, set(payload["results"][0]))
        self.assertEqual("source_order:ORD//2026/001", payload["results"][0]["record_id"])
        self.assertEqual("ORD//2026/001", payload["results"][0]["order_id"])
        self.assertNotIn("invoice", repr(payload).lower())

    def test_browse_source_order_projects_only_allowlisted_fields_and_controls_record_identity(self):
        hostile_row = {
            "order_id": "ORD//2026/001", "order_date": "2026-08-29", "customer_id": "C/001",
            "record_type": "hostile", "record_id": "hostile-id", "provenance": "leak", "source_sha256": "x" * 64,
        }
        statuses, payload = self.get(
            "/order-invoice/api/browse?type=source_order&query=ORD//&limit=1&offset=0",
            order_source=_BrowseOrderSource(hostile_row),
        )

        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertEqual(
            {"record_type": "source_order", "record_id": "source_order:ORD//2026/001", "order_id": "ORD//2026/001", "order_date": "2026-08-29", "customer_id": "C/001"},
            payload["results"][0],
        )

    def test_browse_source_invoice_preserves_exact_slashes_without_order_provenance(self):
        statuses, payload = self.get(
            "/order-invoice/api/browse?type=source_invoice&query=C//&limit=1&offset=0",
            source_invoice_explorer=_BrowseInvoiceSource(),
        )

        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertEqual({"record_type", "record_id", "source_invoice_number", "invoice_date", "customer_id", "customer_name", "slash_family"}, set(payload["results"][0]))
        self.assertEqual("source_invoice:C//2026/001", payload["results"][0]["record_id"])
        self.assertEqual("C//2026/001", payload["results"][0]["source_invoice_number"])
        self.assertNotIn("order", repr(payload).lower())
        self.assertNotIn("source_sha256", repr(payload))

    def test_browse_core_drafts_has_no_provenance_and_never_creates_a_draft(self):
        store = _BrowseDraftStore()
        service = type("Service", (), {"store": store})()
        statuses, payload = self.get(
            "/order-invoice/api/browse?type=core_draft&query=draft-&limit=1&offset=0",
            order_source=None, invoice_draft_service=service,
        )

        self.assertEqual([HTTPStatus.OK], statuses)
        self.assertEqual({"record_type", "record_id", "draft_id", "created_by", "created_at", "status"}, set(payload["results"][0]))
        self.assertEqual("core_draft:draft-001", payload["results"][0]["record_id"])
        self.assertNotIn("sha256", repr(payload).lower())
        self.assertNotIn("provenance", repr(payload).lower())

    def test_missing_invalid_or_repeated_type_is_a_safe_client_error_without_search(self):
        for path in (
            "/order-invoice/api/browse?query=ORD//&limit=1&offset=0",
            "/order-invoice/api/browse?type=unknown&query=ORD//&limit=1&offset=0",
            "/order-invoice/api/browse?type=source_order&type=source_invoice&query=ORD//&limit=1&offset=0",
            "/order-invoice/api/browse?type=source_order&query=&limit=1&offset=0",
        ):
            source = _BrowseOrderSource()
            with self.subTest(path=path):
                statuses, payload = self.get(path, order_source=source)
                self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
                self.assertEqual({"error": "invalid browse query"}, payload)
                self.assertEqual(0, source.calls)

    def test_browse_rejects_unexpected_query_key_before_reading(self):
        source = _BrowseOrderSource()
        request, statuses = self.handler(order_source=source)
        request.path = "/order-invoice/api/browse?type=source_order&query=ORD//&limit=1&offset=0&unexpected=1"

        request.do_GET()

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid browse query"}, json.loads(request.wfile.getvalue()))
        self.assertEqual(0, source.calls)

    def test_browse_requires_exactly_one_valid_ascii_decimal_page_parameter_before_reading(self):
        invalid_pages = (
            "type=source_order&query=ORD//&offset=0",
            "type=source_order&query=ORD//&limit=1",
            "type=source_order&query=ORD//&limit=1&limit=2&offset=0",
            "type=source_order&query=ORD//&limit=1&offset=0&offset=1",
            "type=source_order&query=ORD//&limit=0&offset=0",
            "type=source_order&query=ORD//&limit=251&offset=0",
            "type=source_order&query=ORD//&limit=-1&offset=0",
            "type=source_order&query=ORD//&limit=true&offset=0",
            "type=source_order&query=ORD//&limit=1.0&offset=0",
            "type=source_order&query=ORD//&limit=+1&offset=0",
            "type=source_order&query=ORD//&limit=%201&offset=0",
            "type=source_order&query=ORD//&limit=1&offset=-1",
            "type=source_order&query=ORD//&limit=1&offset=false",
            "type=source_order&query=ORD//&limit=1&offset=%200",
        )
        for page_query in invalid_pages:
            source = _BrowseOrderSource()
            with self.subTest(page_query=page_query):
                statuses, payload = self.get("/order-invoice/api/browse?" + page_query, order_source=source)
                self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
                self.assertEqual({"error": "invalid browse query"}, payload)
                self.assertEqual(0, source.calls)

    def test_browse_mutation_handlers_do_not_route_to_browse_api(self):
        from apc_core.item_explorer import make_handler

        handler_class = make_handler(object(), {})
        for method_name in ("do_POST", "do_PUT", "do_PATCH", "do_DELETE"):
            with self.subTest(method_name=method_name):
                self.assertNotIn("/order-invoice/api/browse", inspect.getsource(getattr(handler_class, method_name)))
                self.assertNotIn("/order-invoice/api/source-orders/", inspect.getsource(getattr(handler_class, method_name)))


class TestOrderInvoiceRuntimeLifecycle(unittest.TestCase):
    def test_runtime_wires_source_invoice_reader_from_accepted_descriptor_and_item_close_owns_it(self):
        from apc_core import server
        from apc_core.item_explorer import ItemExplorer

        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "accepted.sqlite"
            artifact.write_bytes(b"fixture-only accepted descriptor")
            descriptor = os.open(artifact, os.O_RDONLY)
            item = ItemExplorer.__new__(ItemExplorer)
            item._lock = threading.RLock()
            item._connection = Mock()
            item._store = None
            item._source_invoice_explorer = None
            reader = Mock()
            manifest = {"accepted_artifact_sha256": "a" * 64, "capabilities": {}}

            def build_item(_descriptor, _artifact, *, data_dir):
                self.assertIsNone(data_dir)
                return item

            try:
                with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, artifact, manifest)), \
                     patch.object(server.ItemExplorer, "from_open_descriptor", side_effect=build_item) as item_factory, \
                     patch.object(server.SourceInvoiceExplorer, "from_open_descriptor", return_value=reader) as reader_factory:
                    runtime = server.load_accepted_customer_price_order_runtime(Path(directory) / "manifest.json")

                self.assertEqual(6, len(runtime))
                self.assertIs(runtime[0], item)
                item_factory.assert_called_once_with(descriptor, artifact, data_dir=None)
                reader_factory.assert_called_once_with(descriptor, artifact)
                self.assertIs(reader, item.source_invoice_explorer)
                item.close()
                reader.close.assert_called_once_with()
                item._connection.close.assert_called_once_with()
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
