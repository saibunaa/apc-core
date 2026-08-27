"""Pure parsing for Bangkok Bank's public foreign-exchange rate table.

This module performs no network, filesystem, database, or runtime/UI operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re


class BangkokBankRateSourceError(ValueError):
    """The supplied official-rate document cannot safely produce a snapshot."""


@dataclass(frozen=True)
class BangkokBankCurrencyRate:
    currency_label: str
    column_label: str
    raw_value: str
    thb_per_unit: str


@dataclass(frozen=True)
class BangkokBankRateSnapshot:
    source_url: str
    displayed_updated_at: datetime
    retrieved_at: datetime
    currency_column_label: str
    tt_buying_column_label: str
    usd: BangkokBankCurrencyRate
    sgd: BangkokBankCurrencyRate
    usd_to_sgd: str


class _OfficialTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document_text: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._table_rows: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table_rows.append([])
        elif tag == "tr" and self._table_rows:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table_rows:
            self._table_rows[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_rows:
            self.tables.append(self._table_rows.pop())

    def handle_data(self, data: str) -> None:
        self.document_text.append(data)
        if self._cell is not None:
            self._cell.append(data)


_TIMESTAMP_PATTERN = re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s+(?:(?:\d+\s*:\s*)(\d{1,2}:\d{2})|(\d{1,2}:\d{2}))\b")
_TIMESTAMP_FORMATS = ("%d %B %Y %H:%M", "%d %b %Y %H:%M")
_USD50_LABEL = "USD: 50-100"
_SGD_LABEL = "SGD"
_TT_BUYING_PATTERN = re.compile(r"^TT Buying(?: Rates)?$")
_CURRENCY_LABEL = "Currency"


def _parse_displayed_timestamp(document_text: str) -> datetime:
    matches = _TIMESTAMP_PATTERN.findall(document_text)
    if len(matches) != 1:
        raise BangkokBankRateSourceError("displayed update timestamp is missing or ambiguous")
    candidate = f"{matches[0][0]} {matches[0][1] or matches[0][2]}"
    for timestamp_format in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(candidate, timestamp_format)
        except ValueError:
            continue
    raise BangkokBankRateSourceError("displayed update timestamp is malformed")


def _required_row(rows: list[list[str]], label: str) -> list[str]:
    matches = [row for row in rows if row and row[0] == label]
    if len(matches) != 1:
        raise BangkokBankRateSourceError(f"required {label} rate row is missing or ambiguous")
    return matches[0]


def _positive_decimal(raw_value: str, label: str) -> Decimal:
    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError) as error:
        raise BangkokBankRateSourceError(f"{label} TT Buying rate is malformed") from error
    if not value.is_finite() or value <= 0:
        raise BangkokBankRateSourceError(f"{label} TT Buying rate must be positive")
    return value


def _tt_buying_index(header: list[str]) -> int:
    matches = [index for index, label in enumerate(header) if _TT_BUYING_PATTERN.fullmatch(label)]
    if len(matches) != 1:
        raise BangkokBankRateSourceError("official rate table TT Buying column is missing or ambiguous")
    return matches[0]


def _select_official_table(tables: list[list[list[str]]]) -> tuple[list[str], list[list[str]]]:
    matches: list[tuple[list[str], list[list[str]]]] = []
    for table in tables:
        for index, row in enumerate(table):
            if _CURRENCY_LABEL in row and len([label for label in row if _TT_BUYING_PATTERN.fullmatch(label)]) == 1:
                matches.append((row, table[index + 1 :]))
    if len(matches) != 1:
        raise BangkokBankRateSourceError("official rate table is missing or ambiguous")
    return matches[0]


def parse_bangkok_bank_rate_snapshot(
    html: str,
    *,
    source_url: str,
    retrieved_at: datetime,
    max_age: timedelta,
) -> BangkokBankRateSnapshot:
    """Parse a saved official table and derive USD-to-SGD without external I/O.

    ``retrieved_at`` must be timezone-aware so timestamp freshness has an explicit
    reference zone. The displayed page timestamp is retained exactly as a naive
    local datetime because the source display itself provides no offset.
    """
    if type(html) is not str or not html:
        raise BangkokBankRateSourceError("official rate document is missing")
    if type(source_url) is not str or not source_url:
        raise BangkokBankRateSourceError("source URL is missing")
    if type(retrieved_at) is not datetime or retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise BangkokBankRateSourceError("retrieved timestamp must be timezone-aware")
    if type(max_age) is not timedelta or max_age < timedelta(0):
        raise BangkokBankRateSourceError("maximum timestamp age is invalid")

    parser = _OfficialTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as error:
        raise BangkokBankRateSourceError("official rate document is malformed") from error

    displayed_updated_at = _parse_displayed_timestamp(" ".join(parser.document_text))
    displayed_in_retrieval_zone = displayed_updated_at.replace(tzinfo=retrieved_at.tzinfo)
    age = retrieved_at - displayed_in_retrieval_zone
    if age < timedelta(0) or age > max_age:
        raise BangkokBankRateSourceError("displayed update timestamp is stale")

    header, rows = _select_official_table(parser.tables)
    currency_index = header.index(_CURRENCY_LABEL)
    tt_buying_index = _tt_buying_index(header)
    tt_buying_column_label = header[tt_buying_index]
    if currency_index != 0:
        raise BangkokBankRateSourceError("official rate table currency column is malformed")

    usd_row = _required_row(rows, _USD50_LABEL)
    sgd_row = _required_row(rows, _SGD_LABEL)
    if len(usd_row) <= tt_buying_index or len(sgd_row) <= tt_buying_index:
        raise BangkokBankRateSourceError("official rate table TT Buying value is missing")

    usd_raw = usd_row[tt_buying_index]
    sgd_raw = sgd_row[tt_buying_index]
    usd_value = _positive_decimal(usd_raw, _USD50_LABEL)
    sgd_value = _positive_decimal(sgd_raw, _SGD_LABEL)

    return BangkokBankRateSnapshot(
        source_url=source_url,
        displayed_updated_at=displayed_updated_at,
        retrieved_at=retrieved_at,
        currency_column_label=_CURRENCY_LABEL,
        tt_buying_column_label=tt_buying_column_label,
        usd=BangkokBankCurrencyRate(_USD50_LABEL, tt_buying_column_label, usd_raw, str(usd_value)),
        sgd=BangkokBankCurrencyRate(_SGD_LABEL, tt_buying_column_label, sgd_raw, str(sgd_value)),
        usd_to_sgd=str(usd_value / sgd_value),
    )
