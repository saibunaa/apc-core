"""Display-only staff date and timestamp formatting.

These helpers deliberately preserve unparseable source text rather than inventing
values. They are rendering helpers only: adapters, API payloads, filters, and
provenance retain their original values.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Final

_DATE_FORMATS: Final = ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y")
_TIMESTAMP_FORMATS: Final = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)


def _parse_date(value: str) -> date | None:
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def _parse_timestamp(value: str) -> datetime | None:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for pattern in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def format_staff_date(value: object) -> str:
    """Render a date as DD/MM/YYYY, retaining invalid source text unchanged."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    parsed = _parse_date(value)
    if parsed is None:
        timestamp = _parse_timestamp(value)
        return timestamp.strftime("%d/%m/%Y") if timestamp is not None else value
    return parsed.strftime("%d/%m/%Y")


def format_staff_timestamp(value: object) -> str:
    """Render a timestamp as DD/MM/YYYY HH:MM:SS without fabricating a time."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    timestamp = _parse_timestamp(value)
    if timestamp is not None:
        return timestamp.strftime("%d/%m/%Y %H:%M:%S")
    parsed = _parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed is not None else value
