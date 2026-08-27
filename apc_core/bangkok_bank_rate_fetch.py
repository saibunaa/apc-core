"""Bounded, injected transport adapter for Bangkok Bank rate snapshots.

This module deliberately has no concrete HTTP client or official date-URL composition.
A caller supplies the documented date/time URL builder and a bounded requester.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Callable, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from apc_core.bangkok_bank_rate_source import (
    BangkokBankCurrencyRate,
    BangkokBankRateSourceError,
    parse_bangkok_bank_rate_snapshot,
)


_OFFICIAL_HOST = "www.bangkokbank.com"
_BANGKOK_ZONE_KEY = "Asia/Bangkok"


class BangkokBankRateFetchError(ValueError):
    """The bounded official-rate fetch could not safely produce a snapshot."""


@dataclass(frozen=True)
class BangkokBankHttpResponse:
    status: int
    body: bytes


class BangkokBankRequester(Protocol):
    def __call__(self, method: str, url: str, max_bytes: int) -> BangkokBankHttpResponse: ...


class BangkokBankRateUrlBuilder(Protocol):
    def __call__(self, selected_at: datetime) -> str: ...


@dataclass(frozen=True)
class BangkokBankFetchedRate:
    currency_label: str
    column_label: str
    raw_value: str
    thb_per_unit: str


@dataclass(frozen=True)
class BangkokBankRateFetchSnapshot:
    selected_at: datetime
    source_url: str
    displayed_updated_at: datetime
    retrieved_at: datetime
    currency_column_label: str
    tt_buying_column_label: str
    usd: BangkokBankFetchedRate
    sgd: BangkokBankFetchedRate
    usd_to_sgd: str
    source_document_sha256: str


def _is_bangkok_datetime(value: object) -> bool:
    return (
        type(value) is datetime
        and isinstance(value.tzinfo, ZoneInfo)
        and value.tzinfo.key == _BANGKOK_ZONE_KEY
        and value.utcoffset() is not None
    )


def _official_url(value: object) -> str:
    if type(value) is not str or not value:
        raise BangkokBankRateFetchError("official source URL is invalid")
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise BangkokBankRateFetchError("official source URL is invalid") from error
    if (
        parts.scheme != "https"
        or parts.netloc.casefold() != _OFFICIAL_HOST
        or not parts.path.startswith("/")
        or parts.fragment
    ):
        raise BangkokBankRateFetchError("official source URL is invalid")
    return value


def _allowlisted_rate(rate: BangkokBankCurrencyRate) -> BangkokBankFetchedRate:
    return BangkokBankFetchedRate(
        currency_label=rate.currency_label,
        column_label=rate.column_label,
        raw_value=rate.raw_value,
        thb_per_unit=rate.thb_per_unit,
    )


class BangkokBankRateFetchService:
    """Fetch one official document through a caller-provided bounded seam."""

    def __init__(
        self,
        *,
        requester: BangkokBankRequester,
        url_builder: BangkokBankRateUrlBuilder,
        max_response_bytes: int,
        max_age: timedelta,
    ) -> None:
        if not callable(requester) or not callable(url_builder):
            raise BangkokBankRateFetchError("fetch dependencies are invalid")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise BangkokBankRateFetchError("response cap is invalid")
        if type(max_age) is not timedelta or max_age < timedelta(0):
            raise BangkokBankRateFetchError("maximum timestamp age is invalid")
        self._requester = requester
        self._url_builder = url_builder
        self._max_response_bytes = max_response_bytes
        self._max_age = max_age

    def fetch(self, selected_at: datetime, *, retrieved_at: datetime) -> BangkokBankRateFetchSnapshot:
        if not _is_bangkok_datetime(selected_at) or not _is_bangkok_datetime(retrieved_at):
            raise BangkokBankRateFetchError("Bangkok-local timestamps are required")
        try:
            source_url = _official_url(self._url_builder(selected_at))
        except BangkokBankRateFetchError:
            raise
        except Exception as error:
            raise BangkokBankRateFetchError("official source URL is invalid") from error
        try:
            response = self._requester("GET", source_url, self._max_response_bytes)
        except Exception as error:
            raise BangkokBankRateFetchError("official source request failed") from error
        if type(response) is not BangkokBankHttpResponse:
            raise BangkokBankRateFetchError("official source response is invalid")
        if type(response.status) is not int or not 200 <= response.status < 300:
            raise BangkokBankRateFetchError("official source response was unsuccessful")
        if type(response.body) is not bytes or len(response.body) > self._max_response_bytes:
            raise BangkokBankRateFetchError("official source response is invalid")
        try:
            html = response.body.decode("utf-8", errors="strict")
            parsed = parse_bangkok_bank_rate_snapshot(
                html,
                source_url=source_url,
                retrieved_at=retrieved_at,
                max_age=self._max_age,
            )
        except (UnicodeDecodeError, BangkokBankRateSourceError) as error:
            raise BangkokBankRateFetchError("official source document is invalid") from error
        if parsed.source_url != source_url:
            raise BangkokBankRateFetchError("official source document is invalid")
        return BangkokBankRateFetchSnapshot(
            selected_at=selected_at,
            source_url=source_url,
            displayed_updated_at=parsed.displayed_updated_at,
            retrieved_at=parsed.retrieved_at,
            currency_column_label=parsed.currency_column_label,
            tt_buying_column_label=parsed.tt_buying_column_label,
            usd=_allowlisted_rate(parsed.usd),
            sgd=_allowlisted_rate(parsed.sgd),
            usd_to_sgd=parsed.usd_to_sgd,
            source_document_sha256=sha256(response.body).hexdigest(),
        )
