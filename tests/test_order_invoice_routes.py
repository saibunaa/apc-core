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
    def __init__(self, row=None, page=None):
        self.calls = 0
        self.open_calls = 0
        self.source_sha256 = "a" * 64
        self.row = row or {"order_id": "ORD//2026/001", "order_date": "2026-08-29", "customer_id": "C/001"}
        self.page = page

    def open_order(self, order_id, *, limit, offset):
        self.open_calls += 1
        assert (order_id, limit, offset) == ("ORD//2026/001", 2, 0)
        return {
            "order_id": order_id, "order_date": "2026-08-29", "customer_id": "C/001", "customer_name": "Customer One",
            "lines": [
                {"line_no": "0", "item_id": "ITEM-0", "qty": "1", "description_th": "ไทย", "description_th_provenance": "order", "sub_customer": "A1", "description_en": "English", "description_en_provenance": "order", "is_annotation": False},
                {"line_no": "1", "item_id": "ITEM-1", "qty": "2", "description_th": "ไทย 2", "description_th_provenance": "item_master", "sub_customer": "", "description_en": "English 2", "description_en_provenance": "item_master", "is_annotation": False},
            ],
            "total": 1661, "limit": limit, "offset": offset, "has_more": True, "next_offset": 2,
        }

    def browse_orders(self, query, *, limit, offset):
        self.calls += 1
        if self.page is not None:
            return self.page
        assert (query, limit, offset) == ("ORD//", 1, 0)
        return {"total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None, "orders": [self.row]}


class _BrowseInvoiceSource:
    def __init__(self, row=None, page=None):
        self.calls = 0
        self.row = row or {
            "source_type": "source_invoice", "invoice_id": "C//2026/001", "invoice_date": "2026-08-29",
            "customer_id": "C/001", "customer_name": "Customer One", "slash_family": "repeated_slash",
        }
        self.page = page

    def search_invoices(self, *, prefix, limit, offset):
        self.calls += 1
        if self.page is not None:
            return self.page
        assert (prefix, limit, offset) == ("C//", 1, 0)
        return {
            "total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None,
            "invoices": [self.row],
        }


class _BrowseDraftStore:
    def __init__(self, row=None, page=None):
        self.calls = 0
        self.row = row or {
            "draft_id": "draft-001", "created_by": "YIM", "created_at": "2026-08-29 10:00:00", "status": "draft",
        }
        self.page = page

    def list_drafts(self, query, *, limit, offset):
        self.calls += 1
        if self.page is not None:
            return self.page
        assert (query, limit, offset) == ("draft-", 1, 0)
        return {
            "total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None,
            "drafts": [self.row],
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

    def test_workspace_hides_core_drafts_when_handler_has_no_draft_service(self):
        request, statuses = self.handler()
        request.path = "/order-invoice/"

        request.do_GET()

        self.assertEqual([HTTPStatus.OK], statuses)
        html = request.wfile.getvalue().decode("utf-8")
        self.assertIn("Source Orders", html)
        self.assertIn("Source Invoices", html)
        self.assertNotIn("Core Drafts", html)
        self.assertNotIn("local draft review", html)

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
            {
                "line_no", "item_id", "qty", "description_th", "description_th_provenance",
                "sub_customer", "description_en", "description_en_provenance", "is_annotation",
            },
            set(payload["lines"][0]),
        )
        self.assertNotIn("source_sha256", repr(payload))

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
            {
                "line_no", "item_id", "qty", "description_th", "description_th_provenance",
                "sub_customer", "description_en", "description_en_provenance", "is_annotation",
            },
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

    def test_browse_accepts_ascii_case_insensitive_adapter_prefix_matches(self):
        page_meta = {"total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None}
        cases = (
            ("source_order", "ord", _BrowseOrderSource(page={**page_meta, "orders": [{"order_id": "ORD//2026/001", "order_date": "2026-08-29", "customer_id": "C/001"}]}), None, None),
            ("source_invoice", "c//", None, _BrowseInvoiceSource(page={**page_meta, "invoices": [{"source_type": "source_invoice", "invoice_id": "C//2026/001", "invoice_date": "2026-08-29", "customer_id": "C/001", "customer_name": "Customer One", "slash_family": "repeated_slash"}]}), None),
            ("core_draft", "draft-", None, None, type("Service", (), {"store": _BrowseDraftStore(page={**page_meta, "drafts": [{"draft_id": "DRAFT-001", "created_by": "YIM", "created_at": "2026-08-29 10:00:00", "status": "draft"}]})})()),
        )
        for record_type, query, order_source, invoice_source, service in cases:
            with self.subTest(record_type=record_type):
                statuses, payload = self.get(
                    f"/order-invoice/api/browse?type={record_type}&query={query}&limit=1&offset=0",
                    order_source=order_source, source_invoice_explorer=invoice_source, invoice_draft_service=service,
                )
                self.assertEqual([HTTPStatus.OK], statuses)
                self.assertEqual(1, len(payload["results"]))

        invoice_row = {
            "source_type": "source_invoice", "invoice_id": "OTHER/2026/001", "invoice_date": "2026-08-29",
            "customer_id": "C/001", "customer_name": "Customer One", "slash_family": "single_slash",
        }
        invoice_source = _BrowseInvoiceSource(page={
            "total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None, "invoices": [invoice_row],
        })
        draft_store = _BrowseDraftStore(page={
            "total": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None,
            "drafts": [{"draft_id": "other-001", "created_by": "YIM", "created_at": "2026-08-29 10:00:00", "status": "draft"}],
        })
        cases = (
            ("source_invoice", "C//", invoice_source, None),
            ("core_draft", "draft-", None, type("Service", (), {"store": draft_store})()),
        )
        for record_type, query, source, service in cases:
            with self.subTest(record_type=record_type):
                statuses, payload = self.get(
                    f"/order-invoice/api/browse?type={record_type}&query={query}&limit=1&offset=0",
                    source_invoice_explorer=source, invoice_draft_service=service,
                )
                self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
                self.assertEqual({"error": "invalid browse query"}, payload)

        source = _BrowseOrderSource({"order_id": "OTHER/2026/001", "order_date": "2026-08-29", "customer_id": "C/001"})
        statuses, payload = self.get("/order-invoice/api/browse?type=source_order&query=ORD//&limit=1&offset=0", order_source=source)

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid browse query"}, payload)

        hostile_row = {
            "order_id": "ORD//2026/001", "order_date": "2026-08-29", "customer_id": "C/001",
            "record_type": "hostile", "record_id": "hostile-id", "provenance": "leak", "source_sha256": "x" * 64,
        }
        statuses, payload = self.get(
            "/order-invoice/api/browse?type=source_order&query=ORD//&limit=1&offset=0",
            order_source=_BrowseOrderSource(hostile_row),
        )

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid browse query"}, payload)

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

    def test_browse_rejects_reader_page_metadata_that_disagrees_with_the_request(self):
        row = {
            "source_type": "source_invoice", "invoice_id": "C//2026/001", "invoice_date": "2026-08-29",
            "customer_id": "C/001", "customer_name": "Customer One", "slash_family": "repeated_slash",
        }
        source = _BrowseInvoiceSource(page={
            "total": 3, "limit": 3, "offset": 0, "has_more": False, "next_offset": None,
            "invoices": [row, {**row, "invoice_id": "C//2026/002"}, {**row, "invoice_id": "C//2026/003"}],
        })
        statuses, payload = self.get(
            "/order-invoice/api/browse?type=source_invoice&query=C//&limit=2&offset=0",
            source_invoice_explorer=source,
        )

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid browse query"}, payload)

        cases = (
            (
                "source_invoice",
                _BrowseInvoiceSource({
                    "invoice_id": "C//2026/001", "invoice_date": "2026-08-29", "customer_id": "C/001",
                    "customer_name": "Customer One", "slash_family": "repeated_slash", "order_reference": "ORD/hostile",
                }),
                None,
            ),
            (
                "core_draft",
                None,
                _BrowseDraftStore({
                    "draft_id": "draft-001", "created_by": "YIM", "created_at": "2026-08-29 10:00:00",
                    "status": "draft", "source_sha256": "x" * 64,
                }),
            ),
        )
        for record_type, invoice_source, draft_store in cases:
            with self.subTest(record_type=record_type):
                service = None if draft_store is None else type("Service", (), {"store": draft_store})()
                statuses, payload = self.get(
                    f"/order-invoice/api/browse?type={record_type}&query=" + ("C//" if record_type == "source_invoice" else "draft-") + "&limit=1&offset=0",
                    source_invoice_explorer=invoice_source,
                    invoice_draft_service=service,
                )
                self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
                self.assertEqual({"error": "invalid browse query"}, payload)

    def test_browse_rejects_truncated_nonfinal_reader_page(self):
        row = {
            "source_type": "source_invoice", "invoice_id": "C//2026/001", "invoice_date": "2026-08-29",
            "customer_id": "C/001", "customer_name": "Customer One", "slash_family": "repeated_slash",
        }
        source = _BrowseInvoiceSource(page={
            "total": 3, "limit": 2, "offset": 0, "has_more": True, "next_offset": 2, "invoices": [row],
        })
        statuses, payload = self.get(
            "/order-invoice/api/browse?type=source_invoice&query=C//&limit=2&offset=0",
            source_invoice_explorer=source,
        )

        self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
        self.assertEqual({"error": "invalid browse query"}, payload)

    def test_open_source_order_rejects_hostile_cross_family_mutation_or_missing_line_identity(self):
        safe_line = {
            "line_no": "1", "item_id": "ITEM-1", "qty": "2", "description_th": "ไทย",
            "description_th_provenance": "order", "sub_customer": "A1", "description_en": "English",
            "description_en_provenance": "order", "is_annotation": False,
        }
        hostile_pages = (
            {"invoice_id": "INV/hostile"},
            {"order_id": "ORD/hostile"},
            {"provenance": "hostile"},
            {"order_date": None},
            {"limit": "2"},
            {"total": 2, "limit": 2, "offset": 0, "has_more": True, "next_offset": None},
            {"lines": [safe_line], "total": 3, "limit": 2, "offset": 0, "has_more": True, "next_offset": 2},
            {"lines": [
                safe_line,
                {**safe_line, "line_no": "2"},
                {**safe_line, "line_no": "3"},
            ], "total": 3, "limit": 3, "offset": 0, "has_more": False, "next_offset": None},
            {"lines": [{**safe_line, "action": "delete"}]},
            {"lines": [{**safe_line, "invoice_id": "INV/hostile"}]},
            {"lines": [{**safe_line, "source_sha256": "b" * 64}]},
            {"lines": [{"line_no": "1"}], "total": 1, "limit": 2, "offset": 0, "has_more": False, "next_offset": None},
            {"lines": [{**safe_line, "line_no": ""}]},
        )
        for override in hostile_pages:
            with self.subTest(override=override):
                source = _BrowseOrderSource()
                source.source_sha256 = "a" * 64
                original_open = source.open_order

                def hostile_open(order_id, *, limit, offset, override=override):
                    page = original_open(order_id, limit=limit, offset=offset)
                    page.update(override)
                    return page

                source.open_order = hostile_open
                statuses, payload = self.get(
                    "/order-invoice/api/source-orders/ORD//2026/001?limit=2&offset=0", order_source=source
                )
                self.assertEqual([HTTPStatus.BAD_REQUEST], statuses)
                self.assertEqual({"error": "invalid order detail query"}, payload)

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
