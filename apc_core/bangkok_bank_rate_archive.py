"""Local immutable archive for already-validated Bangkok Bank rate snapshots.

This module has no scheduler, browser, HTTP client, credential, or Customer mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from apc_core.bangkok_bank_rate_fetch import BangkokBankRateFetchSnapshot


_BANGKOK_ZONE = "Asia/Bangkok"


class BangkokBankRateArchiveError(ValueError):
    """A validated rate snapshot cannot be admitted to the local archive."""


@dataclass(frozen=True)
class BangkokBankArchivedRate:
    snapshot_id: int
    source_date: str
    update_slot: int
    selected_at: str
    displayed_updated_at: str
    retrieved_at: str
    usd_thb_per_unit: str
    sgd_thb_per_unit: str
    usd_to_sgd: str
    content_sha256: str


def _bangkok_datetime(value: object) -> datetime:
    if not (
        type(value) is datetime
        and isinstance(value.tzinfo, ZoneInfo)
        and value.tzinfo.key == _BANGKOK_ZONE
        and value.utcoffset() is not None
    ):
        raise BangkokBankRateArchiveError("Bangkok-local selected timestamp is required")
    return value


def _canonical_snapshot(snapshot: object, update_slot: object) -> dict[str, object]:
    if type(snapshot) is not BangkokBankRateFetchSnapshot:
        raise BangkokBankRateArchiveError("validated rate snapshot is required")
    if type(update_slot) is not int or not 1 <= update_slot <= 1440:
        raise BangkokBankRateArchiveError("update slot is invalid")
    selected_at = _bangkok_datetime(snapshot.selected_at)
    if type(snapshot.displayed_updated_at) is not datetime or snapshot.displayed_updated_at.tzinfo is not None:
        raise BangkokBankRateArchiveError("displayed timestamp is invalid")
    if snapshot.displayed_updated_at.date() != selected_at.date():
        raise BangkokBankRateArchiveError("displayed timestamp date does not match selected date")
    retrieved_at = _bangkok_datetime(snapshot.retrieved_at)
    values = (snapshot.usd.thb_per_unit, snapshot.sgd.thb_per_unit, snapshot.usd_to_sgd)
    if any(type(value) is not str or not value for value in values):
        raise BangkokBankRateArchiveError("rate values are invalid")
    return {
        "source_date": selected_at.date().isoformat(),
        "update_slot": update_slot,
        "selected_at": selected_at.isoformat(),
        "displayed_updated_at": snapshot.displayed_updated_at.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
        "usd_thb_per_unit": snapshot.usd.thb_per_unit,
        "sgd_thb_per_unit": snapshot.sgd.thb_per_unit,
        "usd_to_sgd": snapshot.usd_to_sgd,
    }


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _record(row: tuple[object, ...]) -> BangkokBankArchivedRate:
    return BangkokBankArchivedRate(*row)  # type: ignore[arg-type]


class BangkokBankRateArchive:
    """Explicit-path SQLite archive whose records are append-only evidence."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        if not self._path.is_absolute() or not self._path.name or self._path.exists() and not self._path.is_file():
            raise BangkokBankRateArchiveError("archive path is invalid")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bangkok_bank_rate_snapshots ("
                "snapshot_id INTEGER PRIMARY KEY, source_date TEXT NOT NULL, update_slot INTEGER NOT NULL, "
                "selected_at TEXT NOT NULL, displayed_updated_at TEXT NOT NULL, retrieved_at TEXT NOT NULL, "
                "usd_thb_per_unit TEXT NOT NULL, sgd_thb_per_unit TEXT NOT NULL, usd_to_sgd TEXT NOT NULL, "
                "content_sha256 TEXT NOT NULL UNIQUE)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def store(self, snapshot: BangkokBankRateFetchSnapshot, *, update_slot: int) -> BangkokBankArchivedRate:
        payload = _canonical_snapshot(snapshot, update_slot)
        content_sha256 = _digest(payload)
        fields = tuple(payload.values()) + (content_sha256,)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO bangkok_bank_rate_snapshots "
                "(source_date, update_slot, selected_at, displayed_updated_at, retrieved_at, usd_thb_per_unit, "
                "sgd_thb_per_unit, usd_to_sgd, content_sha256) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(content_sha256) DO NOTHING",
                fields,
            )
            row = connection.execute(
                "SELECT snapshot_id, source_date, update_slot, selected_at, displayed_updated_at, retrieved_at, "
                "usd_thb_per_unit, sgd_thb_per_unit, usd_to_sgd, content_sha256 "
                "FROM bangkok_bank_rate_snapshots WHERE content_sha256 = ?",
                (content_sha256,),
            ).fetchone()
        if row is None:
            raise BangkokBankRateArchiveError("archive write failed")
        return _record(row)

    def list_for_date(self, source_date: str) -> list[BangkokBankArchivedRate]:
        if type(source_date) is not str:
            raise BangkokBankRateArchiveError("source date is invalid")
        try:
            date.fromisoformat(source_date)
        except ValueError:
            raise BangkokBankRateArchiveError("source date is invalid") from None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_id, source_date, update_slot, selected_at, displayed_updated_at, retrieved_at, "
                "usd_thb_per_unit, sgd_thb_per_unit, usd_to_sgd, content_sha256 "
                "FROM bangkok_bank_rate_snapshots WHERE source_date = ? ORDER BY update_slot, snapshot_id",
                (source_date,),
            ).fetchall()
        return [_record(row) for row in rows]
