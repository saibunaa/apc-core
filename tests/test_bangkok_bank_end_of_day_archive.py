from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo


BANGKOK = ZoneInfo("Asia/Bangkok")


def _snapshot(slot: int):
    from apc_core.bangkok_bank_rate_fetch import BangkokBankFetchedRate, BangkokBankRateFetchSnapshot

    selected_at = datetime(2026, 8, 27, 8, 30 + slot, tzinfo=BANGKOK)
    return BangkokBankRateFetchSnapshot(
        selected_at=selected_at,
        source_url="https://www.bangkokbank.com/en/personal/other-services/view-rates/foreign-exchange-rates",
        displayed_updated_at=selected_at.replace(tzinfo=None),
        retrieved_at=datetime(2026, 8, 27, 18, 0, tzinfo=BANGKOK),
        currency_column_label="Currency",
        tt_buying_column_label="TT Buying",
        usd=BangkokBankFetchedRate("USD: 50-100", "TT Buying", "33.35", "33.35"),
        sgd=BangkokBankFetchedRate("SGD", "TT Buying", "25.60", "25.60"),
        usd_to_sgd="1.302734375",
        source_document_sha256="a" * 64,
    )


class BangkokBankEndOfDayArchiveTests(unittest.TestCase):
    def test_00_end_of_day_module_exists_before_orchestration_contracts(self):
        source = Path(__file__).resolve().parents[1] / "apc_core" / "bangkok_bank_end_of_day_archive.py"
        self.assertTrue(source.is_file())

    def test_archives_each_discovered_slot_once_with_injected_dependencies(self):
        from apc_core.bangkok_bank_end_of_day_archive import BangkokBankEndOfDayArchive
        from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive

        calls: list[tuple[str, int]] = []
        with TemporaryDirectory() as temporary_directory:
            archive = BangkokBankRateArchive(Path(temporary_directory) / "rates.sqlite")
            runner = BangkokBankEndOfDayArchive(
                archive=archive,
                list_slots=lambda source_date: [1, 2],
                fetch_snapshot=lambda source_date, slot: calls.append((source_date, slot)) or _snapshot(slot),
            )
            run = runner.archive_date("2026-08-27")
            stored = archive.list_for_date("2026-08-27")

        self.assertEqual(("2026-08-27", (1, 2), ()), run)
        self.assertEqual([("2026-08-27", 1), ("2026-08-27", 2)], calls)
        self.assertEqual(2, len(stored))

    def test_failed_slot_is_reported_while_other_slots_are_archived(self):
        from apc_core.bangkok_bank_end_of_day_archive import BangkokBankEndOfDayArchive
        from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive

        with TemporaryDirectory() as temporary_directory:
            archive = BangkokBankRateArchive(Path(temporary_directory) / "rates.sqlite")
            runner = BangkokBankEndOfDayArchive(
                archive=archive,
                list_slots=lambda source_date: [1, 2],
                fetch_snapshot=lambda source_date, slot: _snapshot(slot) if slot == 1 else (_ for _ in ()).throw(RuntimeError("upstream")),
            )
            run = runner.archive_date("2026-08-27")
            stored = archive.list_for_date("2026-08-27")

        self.assertEqual(("2026-08-27", (1,), (2,)), run)
        self.assertEqual(1, len(stored))
