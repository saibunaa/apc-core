from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from tests.test_bangkok_bank_rate_source import OFFICIAL_TABLE_HTML


BANGKOK = ZoneInfo("Asia/Bangkok")
SELECTED_AT = datetime(2026, 8, 27, 8, 30, tzinfo=BANGKOK)
RETRIEVED_AT = datetime(2026, 8, 27, 8, 35, tzinfo=BANGKOK)
OFFICIAL_URL = "https://www.bangkokbank.com/en/Personal/Other-Services/View-Rates/Foreign-Exchange-Rates"
RESPONSE_CAP = 32_768


class RecordingRequester:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, method: str, url: str, max_bytes: int) -> object:
        self.calls.append((method, url, max_bytes))
        return self.response


class BangkokBankRateFetchTests(unittest.TestCase):
    def make_service(self, requester: RecordingRequester, *, url_builder= None):
        from apc_core.bangkok_bank_rate_fetch import BangkokBankRateFetchService

        return BangkokBankRateFetchService(
            requester=requester,
            url_builder=url_builder or (lambda selected_at: OFFICIAL_URL),
            max_response_bytes=RESPONSE_CAP,
            max_age=timedelta(hours=1),
        )

    def test_fetch_passes_selected_bangkok_time_to_injected_builder_and_requests_exact_bounded_get(self):
        from apc_core.bangkok_bank_rate_fetch import BangkokBankHttpResponse

        requester = RecordingRequester(BangkokBankHttpResponse(status=200, body=OFFICIAL_TABLE_HTML.encode("utf-8")))
        builder_calls: list[datetime] = []

        def build_url(selected_at: datetime) -> str:
            builder_calls.append(selected_at)
            return OFFICIAL_URL

        snapshot = self.make_service(requester, url_builder=build_url).fetch(SELECTED_AT, retrieved_at=RETRIEVED_AT)

        self.assertEqual([SELECTED_AT], builder_calls)
        self.assertEqual([("GET", OFFICIAL_URL, RESPONSE_CAP)], requester.calls)
        self.assertEqual(SELECTED_AT, snapshot.selected_at)
        self.assertEqual(RETRIEVED_AT, snapshot.retrieved_at)
        self.assertEqual(datetime(2026, 8, 27, 8, 30), snapshot.displayed_updated_at)
        self.assertEqual("33.35", snapshot.usd.thb_per_unit)
        self.assertEqual("25.60", snapshot.sgd.thb_per_unit)
        self.assertEqual("1.302734375", snapshot.usd_to_sgd)
        self.assertNotIn("<table", repr(snapshot).lower())
        self.assertNotIn("<!doctype", repr(snapshot).lower())
        with self.assertRaises((AttributeError, FrozenInstanceError)):
            snapshot.usd.raw_value = "0"

    def test_rejects_non_bangkok_or_malformed_selected_time_before_builder_or_request(self):
        from apc_core.bangkok_bank_rate_fetch import BangkokBankRateFetchError, BangkokBankHttpResponse

        requester = RecordingRequester(BangkokBankHttpResponse(status=200, body=b"ignored"))
        builder_calls: list[object] = []
        service = self.make_service(requester, url_builder=lambda selected_at: builder_calls.append(selected_at) or OFFICIAL_URL)
        invalid_values = (
            None,
            "2026-08-27T08:30:00+07:00",
            datetime(2026, 8, 27, 8, 30),
            datetime(2026, 8, 27, 8, 30, tzinfo=ZoneInfo("UTC")),
            datetime(2026, 2, 30, 8, 30, tzinfo=BANGKOK) if False else True,
        )
        for selected_at in invalid_values:
            with self.subTest(selected_at=repr(selected_at)):
                with self.assertRaises(BangkokBankRateFetchError):
                    service.fetch(selected_at, retrieved_at=RETRIEVED_AT)
        self.assertEqual([], builder_calls)
        self.assertEqual([], requester.calls)

    def test_rejects_non_official_or_non_https_url_before_request(self):
        from apc_core.bangkok_bank_rate_fetch import BangkokBankHttpResponse, BangkokBankRateFetchError

        for url in (
            "http://www.bangkokbank.com/rates",
            "https://bangkokbank.com/rates",
            "https://www.bangkokbank.com.evil.test/rates",
            "https://user@www.bangkokbank.com/rates",
            "https://www.bangkokbank.com:444/rates",
            "not a url",
        ):
            with self.subTest(url=url):
                requester = RecordingRequester(BangkokBankHttpResponse(status=200, body=b"ignored"))
                with self.assertRaises(BangkokBankRateFetchError):
                    self.make_service(requester, url_builder=lambda selected_at, url=url: url).fetch(SELECTED_AT, retrieved_at=RETRIEVED_AT)
                self.assertEqual([], requester.calls)

    def test_fails_closed_for_request_failures_and_malformed_or_non_success_response_without_parsing(self):
        from apc_core.bangkok_bank_rate_fetch import BangkokBankHttpResponse, BangkokBankRateFetchError

        cases = (
            RuntimeError("network failure"),
            BangkokBankHttpResponse(status=503, body=OFFICIAL_TABLE_HTML.encode()),
            BangkokBankHttpResponse(status=True, body=OFFICIAL_TABLE_HTML.encode()),
            BangkokBankHttpResponse(status=200, body="not bytes"),
            object(),
        )
        for response in cases:
            with self.subTest(response=type(response).__name__):
                requester = RecordingRequester(response)
                with self.assertRaises(BangkokBankRateFetchError) as raised:
                    self.make_service(requester).fetch(SELECTED_AT, retrieved_at=RETRIEVED_AT)
                self.assertNotIn("<!doctype", str(raised.exception).lower())
                self.assertEqual([("GET", OFFICIAL_URL, RESPONSE_CAP)], requester.calls)

    def test_rejects_over_cap_body_and_parser_failures_without_returning_source_body(self):
        from apc_core.bangkok_bank_rate_fetch import BangkokBankHttpResponse, BangkokBankRateFetchError

        for body in (b"x" * (RESPONSE_CAP + 1), b"\xff", b"<html>not the official table</html>"):
            with self.subTest(body_kind=body[:12]):
                requester = RecordingRequester(BangkokBankHttpResponse(status=200, body=body))
                with self.assertRaises(BangkokBankRateFetchError) as raised:
                    self.make_service(requester).fetch(SELECTED_AT, retrieved_at=RETRIEVED_AT)
                body_text = body.decode("utf-8", errors="ignore")
                if body_text:
                    self.assertNotIn(body_text, str(raised.exception))
                self.assertEqual([("GET", OFFICIAL_URL, RESPONSE_CAP)], requester.calls)

    def test_rejects_invalid_retrieval_timestamp_before_request(self):
        from apc_core.bangkok_bank_rate_fetch import BangkokBankHttpResponse, BangkokBankRateFetchError

        requester = RecordingRequester(BangkokBankHttpResponse(status=200, body=b"ignored"))
        with self.assertRaises(BangkokBankRateFetchError):
            self.make_service(requester).fetch(SELECTED_AT, retrieved_at=datetime(2026, 8, 27, 8, 35))
        self.assertEqual([], requester.calls)


if __name__ == "__main__":
    unittest.main()
