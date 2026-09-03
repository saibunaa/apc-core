"""Strict, pure normalization for read-only legacy source browse dates."""

from __future__ import annotations

import re
from datetime import date, datetime


_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LEGACY_TIMESTAMP = re.compile(r"[0-9]{2}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def normalize_source_date(value: object) -> str | None:
    """Return an ISO date for the two accepted source forms, otherwise ``None``.

    This deliberately preserves no timestamp representation: callers retain the
    original source value for their DTO/display/provenance boundaries and use the
    return value only for bounded comparisons.
    """
    if type(value) is not str:
        return None
    if _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    if not _LEGACY_TIMESTAMP.fullmatch(value):
        return None
    normalized_timestamp = f"20{value[6:8]}-{value[0:2]}-{value[3:5]} {value[9:]}"
    try:
        return datetime.fromisoformat(normalized_timestamp).date().isoformat()
    except ValueError:
        return None


def normalized_source_date_sql(column: str) -> str:
    """Return the equivalent fail-closed SQLite expression for a fixed column."""
    legacy_timestamp = (
        f"printf('20%s-%s-%s %s', substr({column}, 7, 2), substr({column}, 1, 2), "
        f"substr({column}, 4, 2), substr({column}, 10, 8))"
    )
    return (
        "CASE "
        f"WHEN typeof({column}) = 'text' AND length({column}) = 10 "
        f"AND {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        f"AND strftime('%Y-%m-%d', julianday({column})) = {column} THEN {column} "
        f"WHEN typeof({column}) = 'text' AND length({column}) = 17 "
        f"AND {column} GLOB '[0-9][0-9]/[0-9][0-9]/[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]' "
        f"AND strftime('%Y-%m-%d %H:%M:%S', julianday({legacy_timestamp})) = {legacy_timestamp} "
        f"THEN substr({legacy_timestamp}, 1, 10) ELSE NULL END"
    )
