from __future__ import annotations

from datetime import datetime
import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from apc_core.bangkok_bank_rate_fetch import BangkokBankFetchedRate, BangkokBankRateFetchSnapshot
from apc_core.item_explorer import ItemExplorer, make_handler


BANGKOK = ZoneInfo("Asia/Bangkok")


class RecordingBangkokRateService:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.failure = failure

    def selected_rate(self, selected_date: str, update_slot: int) -> BangkokBankRateFetchSnapshot:
        self.calls.append((selected_date, update_slot))
        if self.failure is not None:
            raise self.failure
        selected_at = datetime(2026, 8, 27, 9, 15, tzinfo=BANGKOK)
        return BangkokBankRateFetchSnapshot(
            selected_at=selected_at,
            source_url="https://example.test/internal-fixture-only",
            displayed_updated_at=datetime(2026, 8, 27, 9, 10),
            retrieved_at=datetime(2026, 8, 27, 9, 16, tzinfo=BANGKOK),
            currency_column_label="Currency",
            tt_buying_column_label="TT Buying",
            usd=BangkokBankFetchedRate("USD: 50-100", "TT Buying", "33.35", "33.35"),
            sgd=BangkokBankFetchedRate("SGD", "TT Buying", "25.60", "25.60"),
            usd_to_sgd="1.302734375",
            source_document_sha256="a" * 64,
        )


class CustomerBangkokRateApiTests(unittest.TestCase):
    def make_server(self, service=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "items.sqlite"
        connection = sqlite3.connect(source)
        connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
        connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-001")')
        connection.commit()
        connection.close()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                ItemExplorer(source, data_dir=root / "state"),
                {"accepted": True},
                bangkok_rate_service=service,
            ),
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return int(server.server_address[1])

    @staticmethod
    def get(port: int, path: str) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_selected_rate_uses_only_injected_service_and_returns_allowlisted_snapshot(self):
        service = RecordingBangkokRateService()
        port = self.make_server(service)

        for path in (
            "/customers/api/rates/bangkok-bank?date=2026-08-27&update_slot=3",
            "/program/customers/api/rates/bangkok-bank?date=2026-08-27&update_slot=3",
        ):
            status, payload = self.get(port, path)
            self.assertEqual(200, status)
            self.assertEqual(
                {
                    "date": "2026-08-27",
                    "update_slot": 3,
                    "selected_at": "2026-08-27T09:15:00+07:00",
                    "rates": {
                        "usd": {"currency_label": "USD: 50-100", "column_label": "TT Buying", "thb_per_unit": "33.35"},
                        "sgd": {"currency_label": "SGD", "column_label": "TT Buying", "thb_per_unit": "25.60"},
                        "usd_to_sgd": "1.302734375",
                    },
                },
                payload,
            )
            self.assertNotIn("source_url", payload)
            self.assertNotIn("raw_value", repr(payload))
        self.assertEqual([("2026-08-27", 3), ("2026-08-27", 3)], service.calls)

    def test_unconfigured_rate_service_is_stable_503_without_a_fake_rate(self):
        status, payload = self.get(
            self.make_server(),
            "/customers/api/rates/bangkok-bank?date=2026-08-27&update_slot=1",
        )
        self.assertEqual(503, status)
        self.assertEqual({"error": "Bangkok Bank rate service is not configured"}, payload)

    def test_malformed_unknown_duplicate_or_out_of_range_query_fails_closed_before_service(self):
        service = RecordingBangkokRateService()
        port = self.make_server(service)
        for query in (
            "",
            "date=2026-08-27",
            "date=2026-02-29&update_slot=1",
            "date=2026-08-27&update_slot=0",
            "date=2026-08-27&update_slot=01",
            "date=2026-08-27&update_slot=1441",
            "date=2026-08-27&update_slot=1&extra=value",
            "date=2026-08-27&date=2026-08-28&update_slot=1",
        ):
            with self.subTest(query=query):
                suffix = f"?{query}" if query else ""
                status, payload = self.get(port, "/customers/api/rates/bangkok-bank" + suffix)
                self.assertEqual(400, status)
                self.assertEqual({"error": "invalid Bangkok Bank rate query"}, payload)
        self.assertEqual([], service.calls)

    def test_service_failure_is_generic_502_and_does_not_expose_details(self):
        service = RecordingBangkokRateService(failure=RuntimeError("credential=top-secret <table>"))
        status, payload = self.get(
            self.make_server(service),
            "/customers/api/rates/bangkok-bank?date=2026-08-27&update_slot=1",
        )
        self.assertEqual(502, status)
        self.assertEqual({"error": "Bangkok Bank rate service failed"}, payload)
        self.assertNotIn("secret", repr(payload))
        self.assertEqual([("2026-08-27", 1)], service.calls)

    def test_rate_path_is_exact_and_uses_the_existing_customer_access_guard(self):
        status, payload = self.get(
            self.make_server(RecordingBangkokRateService()),
            "/customers/api/rates/bangkok-bank-extra?date=2026-08-27&update_slot=1",
        )
        self.assertEqual(404, status)
        self.assertEqual({"error": "not found"}, payload)
        source = (Path(__file__).resolve().parents[1] / "apc_core" / "item_explorer.py").read_text(encoding="utf-8")
        self.assertIn('_BANGKOK_RATE_PATH = "/customers/api/rates/bangkok-bank"', source)
        self.assertIn("_customer_client_allowed(self.client_address[0], customer_lan_ingress)", source)
        self.assertNotIn("bangkokbank.com", source)


if __name__ == "__main__":
    unittest.main()
