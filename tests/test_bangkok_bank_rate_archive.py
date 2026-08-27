from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo


BANGKOK = ZoneInfo("Asia/Bangkok")


def _snapshot(*, usd: str = "33.35", sgd: str = "25.60"):
    from apc_core.bangkok_bank_rate_fetch import BangkokBankFetchedRate, BangkokBankRateFetchSnapshot

    selected_at = datetime(2026, 8, 27, 8, 30, tzinfo=BANGKOK)
    return BangkokBankRateFetchSnapshot(
        selected_at=selected_at,
        source_url="https://www.bangkokbank.com/en/personal/other-services/view-rates/foreign-exchange-rates",
        displayed_updated_at=datetime(2026, 8, 27, 8, 30),
        retrieved_at=datetime(2026, 8, 27, 18, 0, tzinfo=BANGKOK),
        currency_column_label="Currency",
        tt_buying_column_label="TT Buying",
        usd=BangkokBankFetchedRate("USD: 50-100", "TT Buying", usd, usd),
        sgd=BangkokBankFetchedRate("SGD", "TT Buying", sgd, sgd),
        usd_to_sgd=str(float(usd) / float(sgd)),
    )


class BangkokBankRateArchiveTests(unittest.TestCase):
    def test_00_archive_module_exists_before_storage_contracts_are_exercised(self):
        source = Path(__file__).resolve().parents[1] / "apc_core" / "bangkok_bank_rate_archive.py"
        self.assertTrue(source.is_file())

    def test_stores_one_immutable_snapshot_for_selected_bank_date_and_slot(self):
        from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive

        with TemporaryDirectory() as temporary_directory:
            archive = BangkokBankRateArchive(Path(temporary_directory) / "rates.sqlite")
            stored = archive.store(_snapshot(), update_slot=1)
            records = archive.list_for_date("2026-08-27")

        self.assertEqual("2026-08-27", stored.source_date)
        self.assertEqual(1, stored.update_slot)
        self.assertEqual("33.35", stored.usd_thb_per_unit)
        self.assertEqual("25.60", stored.sgd_thb_per_unit)
        self.assertEqual(1, len(records))
        self.assertEqual(stored, records[0])

    def test_exact_repeat_is_idempotent_but_corrected_same_slot_is_retained(self):
        from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive

        with TemporaryDirectory() as temporary_directory:
            archive = BangkokBankRateArchive(Path(temporary_directory) / "rates.sqlite")
            first = archive.store(_snapshot(), update_slot=1)
            repeated = archive.store(_snapshot(), update_slot=1)
            corrected = archive.store(_snapshot(usd="33.40"), update_slot=1)
            records = archive.list_for_date("2026-08-27")

        self.assertEqual(first, repeated)
        self.assertNotEqual(first.content_sha256, corrected.content_sha256)
        self.assertEqual([first, corrected], records)

    def test_rejects_snapshot_whose_displayed_bank_date_differs_from_selected_date(self):
        from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive, BangkokBankRateArchiveError

        mismatched = replace(_snapshot(), displayed_updated_at=datetime(2026, 8, 26, 17, 0))
        with TemporaryDirectory() as temporary_directory:
            archive = BangkokBankRateArchive(Path(temporary_directory) / "rates.sqlite")
            with self.assertRaises(BangkokBankRateArchiveError):
                archive.store(mismatched, update_slot=1)

    def test_rejects_relative_archive_path_without_creating_runtime_state(self):
        from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive, BangkokBankRateArchiveError

        with TemporaryDirectory() as temporary_directory:
            prior_directory = Path.cwd()
            try:
                os.chdir(temporary_directory)
                with self.assertRaises(BangkokBankRateArchiveError):
                    BangkokBankRateArchive(Path("rates.sqlite"))
                self.assertFalse(Path("rates.sqlite").exists())
            finally:
                os.chdir(prior_directory)

    def test_invalid_date_error_has_no_chained_raw_input_cause(self):
        from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive, BangkokBankRateArchiveError

        with TemporaryDirectory() as temporary_directory:
            archive = BangkokBankRateArchive(Path(temporary_directory) / "rates.sqlite")
            with self.assertRaises(BangkokBankRateArchiveError) as raised:
                archive.list_for_date("raw-date-marker")

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("raw-date-marker", str(raised.exception))

    def test_idempotency_sql_scopes_conflict_to_content_digest_only(self):
        source = (Path(__file__).resolve().parents[1] / "apc_core" / "bangkok_bank_rate_archive.py").read_text()
        self.assertIn("ON CONFLICT(content_sha256) DO NOTHING", source)
        self.assertNotIn("INSERT OR IGNORE", source)
