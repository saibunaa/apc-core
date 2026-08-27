"""Injected local coordinator for one Bangkok Bank end-of-day archive pass.

This is not a scheduler and provides no browser, HTTP, credential, or runtime wiring.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from apc_core.bangkok_bank_rate_archive import BangkokBankRateArchive
from apc_core.bangkok_bank_rate_fetch import BangkokBankRateFetchSnapshot


class BangkokBankEndOfDayArchiveError(ValueError):
    """An end-of-day archive pass cannot be safely started."""


class BangkokBankEndOfDayArchive:
    """Coordinate one explicit date pass through injected discovery/fetch seams."""

    def __init__(
        self,
        *,
        archive: BangkokBankRateArchive,
        list_slots: Callable[[str], list[int]],
        fetch_snapshot: Callable[[str, int], BangkokBankRateFetchSnapshot],
    ) -> None:
        if type(archive) is not BangkokBankRateArchive or not callable(list_slots) or not callable(fetch_snapshot):
            raise BangkokBankEndOfDayArchiveError("archive dependencies are invalid")
        self._archive = archive
        self._list_slots = list_slots
        self._fetch_snapshot = fetch_snapshot

    def archive_date(self, source_date: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
        if type(source_date) is not str:
            raise BangkokBankEndOfDayArchiveError("source date is invalid")
        try:
            date.fromisoformat(source_date)
        except ValueError:
            raise BangkokBankEndOfDayArchiveError("source date is invalid") from None
        try:
            slots = self._list_slots(source_date)
        except Exception:
            raise BangkokBankEndOfDayArchiveError("slot discovery failed") from None
        if type(slots) is not list or any(type(slot) is not int or not 1 <= slot <= 1440 for slot in slots):
            raise BangkokBankEndOfDayArchiveError("discovered slots are invalid")
        if len(set(slots)) != len(slots):
            raise BangkokBankEndOfDayArchiveError("discovered slots are ambiguous")

        archived: list[int] = []
        failed: list[int] = []
        for slot in sorted(slots):
            try:
                snapshot = self._fetch_snapshot(source_date, slot)
                if snapshot.selected_at.date().isoformat() != source_date:
                    raise BangkokBankEndOfDayArchiveError("selected snapshot date is invalid")
                self._archive.store(snapshot, update_slot=slot)
            except Exception:
                failed.append(slot)
            else:
                archived.append(slot)
        return source_date, tuple(archived), tuple(failed)
