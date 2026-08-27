from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest


SOURCE_URL = "https://www.bangkokbank.com/en/Personal/Other-Services/View-Rates/Foreign-Exchange-Rates"
RETRIEVED_AT = datetime(2026, 8, 27, 8, 35, tzinfo=timezone(timedelta(hours=7)))

OFFICIAL_TABLE_HTML = """
<!doctype html>
<html><body>
  <p>Last Update: 27 August 2026 08:30</p>
  <table>
    <thead>
      <tr><th>Currency</th><th>Bank Notes Buying</th><th>TT Buying</th><th>TT Selling</th></tr>
    </thead>
    <tbody>
      <tr><td>USD: 1-2</td><td>33.10</td><td>33.20</td><td>33.50</td></tr>
      <tr><td>USD: 50-100</td><td>33.20</td><td>33.35</td><td>33.65</td></tr>
      <tr><td>SGD</td><td>25.50</td><td>25.60</td><td>25.90</td></tr>
    </tbody>
  </table>
</body></html>
"""


class BangkokBankRateSourceTests(unittest.TestCase):
    def test_00_module_exists_before_parser_contracts_are_exercised(self):
        source = Path(__file__).resolve().parents[1] / "apc_core" / "bangkok_bank_rate_source.py"
        self.assertTrue(source.is_file())

    def test_parses_required_tt_buying_rates_and_immutable_provenance_snapshot(self):
        from apc_core.bangkok_bank_rate_source import parse_bangkok_bank_rate_snapshot

        snapshot = parse_bangkok_bank_rate_snapshot(
            OFFICIAL_TABLE_HTML,
            source_url=SOURCE_URL,
            retrieved_at=RETRIEVED_AT,
            max_age=timedelta(hours=1),
        )

        self.assertEqual(SOURCE_URL, snapshot.source_url)
        self.assertEqual(datetime(2026, 8, 27, 8, 30), snapshot.displayed_updated_at)
        self.assertEqual(RETRIEVED_AT, snapshot.retrieved_at)
        self.assertEqual("Currency", snapshot.currency_column_label)
        self.assertEqual("TT Buying", snapshot.tt_buying_column_label)
        self.assertEqual("USD: 50-100", snapshot.usd.currency_label)
        self.assertEqual("33.35", snapshot.usd.raw_value)
        self.assertEqual("SGD", snapshot.sgd.currency_label)
        self.assertEqual("25.60", snapshot.sgd.raw_value)
        self.assertEqual("33.35", snapshot.usd.thb_per_unit)
        self.assertEqual("25.60", snapshot.sgd.thb_per_unit)
        self.assertEqual("1.302734375", snapshot.usd_to_sgd)
        with self.assertRaises((AttributeError, TypeError)):
            snapshot.usd.raw_value = "0"

    def test_parses_bangkok_bank_rendered_date_time_and_column_labels(self):
        from apc_core.bangkok_bank_rate_source import parse_bangkok_bank_rate_snapshot

        rendered_html = OFFICIAL_TABLE_HTML.replace(
            "Last Update: 27 August 2026 08:30",
            "Update as of 27 Aug 2026 1: 08:30",
        ).replace("<th>TT Buying</th>", "<th>TT Buying Rates</th>")
        snapshot = parse_bangkok_bank_rate_snapshot(
            rendered_html,
            source_url=SOURCE_URL,
            retrieved_at=RETRIEVED_AT,
            max_age=timedelta(hours=1),
        )
        self.assertEqual(datetime(2026, 8, 27, 8, 30), snapshot.displayed_updated_at)
        self.assertEqual("TT Buying Rates", snapshot.tt_buying_column_label)

    def test_rejects_missing_required_rate(self):
        from apc_core.bangkok_bank_rate_source import BangkokBankRateSourceError, parse_bangkok_bank_rate_snapshot

        html = OFFICIAL_TABLE_HTML.replace("<tr><td>SGD</td><td>25.50</td><td>25.60</td><td>25.90</td></tr>", "")
        with self.assertRaisesRegex(BangkokBankRateSourceError, "SGD"):
            parse_bangkok_bank_rate_snapshot(html, source_url=SOURCE_URL, retrieved_at=RETRIEVED_AT, max_age=timedelta(hours=1))

    def test_rejects_multiple_required_rate_rows(self):
        from apc_core.bangkok_bank_rate_source import BangkokBankRateSourceError, parse_bangkok_bank_rate_snapshot

        duplicate = "<tr><td>SGD</td><td>25.50</td><td>25.60</td><td>25.90</td></tr>"
        html = OFFICIAL_TABLE_HTML.replace("</tbody>", duplicate + "</tbody>")
        with self.assertRaisesRegex(BangkokBankRateSourceError, "SGD"):
            parse_bangkok_bank_rate_snapshot(html, source_url=SOURCE_URL, retrieved_at=RETRIEVED_AT, max_age=timedelta(hours=1))

    def test_rejects_malformed_or_non_positive_tt_buying_value(self):
        from apc_core.bangkok_bank_rate_source import BangkokBankRateSourceError, parse_bangkok_bank_rate_snapshot

        for raw_value in ("not-a-rate", "0", "-25.60"):
            with self.subTest(raw_value=raw_value):
                html = OFFICIAL_TABLE_HTML.replace("<td>25.60</td>", f"<td>{raw_value}</td>")
                with self.assertRaises(BangkokBankRateSourceError):
                    parse_bangkok_bank_rate_snapshot(html, source_url=SOURCE_URL, retrieved_at=RETRIEVED_AT, max_age=timedelta(hours=1))

    def test_rejects_missing_or_stale_displayed_timestamp(self):
        from apc_core.bangkok_bank_rate_source import BangkokBankRateSourceError, parse_bangkok_bank_rate_snapshot

        missing = OFFICIAL_TABLE_HTML.replace("Last Update: 27 August 2026 08:30", "Rates")
        with self.assertRaisesRegex(BangkokBankRateSourceError, "timestamp"):
            parse_bangkok_bank_rate_snapshot(missing, source_url=SOURCE_URL, retrieved_at=RETRIEVED_AT, max_age=timedelta(hours=1))

        stale = OFFICIAL_TABLE_HTML.replace("27 August 2026 08:30", "27 August 2026 06:00")
        with self.assertRaisesRegex(BangkokBankRateSourceError, "stale"):
            parse_bangkok_bank_rate_snapshot(stale, source_url=SOURCE_URL, retrieved_at=RETRIEVED_AT, max_age=timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
