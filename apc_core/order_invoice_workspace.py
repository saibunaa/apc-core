"""Pure, immutable render DTOs for independently read order, invoice, and draft records.

This module maps already-read data only.  It opens no database, selects no source
schema, and has no mutation capability; its callers retain ownership of their
separate source or Core readers.
"""

from __future__ import annotations

from dataclasses import dataclass


_HEX = frozenset("0123456789abcdef")
_SOURCE_INVOICE_ORDER_PROVENANCE_KEYS = frozenset(
    {"order_id", "order_no", "order_number", "order_reference"}
)
_MUTATION_CONTROL_KEYS = frozenset({"action", "actions", "mutation_control", "mutation_controls"})
_SOURCE_INVOICE_HEADER_FIELDS = frozenset({"invoice_date", "customer_id", "customer_name"})
_SOURCE_INVOICE_LINE_FIELDS = (
    "line_no", "item_id", "description", "qty", "price", "amount", "sub_customer"
)
_SOURCE_ORDER_LINE_FIELDS = (
    "line_no", "item_id", "qty", "description_th", "description_th_provenance", "sub_customer",
    "description_en", "description_en_provenance", "is_annotation"
)


def _reject_keys_anywhere(value: object, forbidden_keys: frozenset[str], label: str) -> None:
    if isinstance(value, dict):
        for key, item in dict.items(value):
            if type(key) is not str or key in forbidden_keys:
                raise ValueError(label)
            _reject_keys_anywhere(item, forbidden_keys, label)
    elif isinstance(value, list):
        for item in list.__iter__(value):
            _reject_keys_anywhere(item, forbidden_keys, label)
    elif isinstance(value, tuple):
        for item in tuple.__iter__(value):
            _reject_keys_anywhere(item, forbidden_keys, label)


@dataclass(frozen=True, slots=True)
class SourceLineReference:
    """Immutable source coordinate reserved for later Core-local planning."""

    source_type: str
    document_id: str
    line_id: str
    source_sha256: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class SourceRenderDTO:
    record_type: str
    record_id: str
    source_sha256: str
    customer_id: str
    customer_name: str
    line_page: tuple[tuple[tuple[str, object], ...], ...]
    line_total: int
    next_offset: int | None
    line_references: tuple[SourceLineReference, ...]
    read_only: bool
    document_date: str | None = None
    line_limit: int | None = None
    line_offset: int | None = None
    has_more: bool | None = None


@dataclass(frozen=True, slots=True)
class CoreDraftRenderDTO:
    record_type: str
    record_id: str
    customer_id: str
    customer_name: str
    line_page: tuple[tuple[tuple[str, object], ...], ...]
    line_total: int
    next_offset: int | None
    read_only: bool


@dataclass(frozen=True, slots=True)
class BrowseRenderDTO:
    """Closed, source-family-specific summary safe for the browse route."""

    record_type: str
    record_id: str
    fields: tuple[tuple[str, str], ...]
    read_only: bool


@dataclass(frozen=True, slots=True)
class BrowsePageDTO:
    """Validated browse pagination bound to the request that produced it."""

    rows: tuple[dict[str, object], ...]
    total: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"invalid {label}")
    return value


def _source_sha256(value: object) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in _HEX for character in value):
        raise ValueError("invalid source sha256")
    return value


def _line_page(
    value: object, *, forbid_order_provenance: bool, allowed_keys: tuple[str, ...] | None = None, require_exact_keys: bool = False
) -> tuple[tuple[tuple[str, object], ...], ...]:
    if type(value) is not list:
        raise ValueError("invalid line page")
    frozen_lines: list[tuple[tuple[str, object], ...]] = []
    for line in value:
        if type(line) is not dict:
            raise ValueError("invalid line")
        if forbid_order_provenance and "order_id" in line:
            raise ValueError("source invoice cannot carry order provenance")
        if allowed_keys is not None and (
            set(line) != set(allowed_keys) if require_exact_keys else not set(line).issubset(allowed_keys)
        ):
            raise ValueError("invalid source invoice line")
        frozen_line: list[tuple[str, object]] = []
        items = ((key, line[key]) for key in allowed_keys if key in line) if allowed_keys is not None else line.items()
        for key, item in items:
            if type(key) is not str or type(item) not in (str, int, float, bool, type(None)):
                raise ValueError("invalid render line")
            frozen_line.append((key, item))
        frozen_lines.append(tuple(frozen_line))
    return tuple(frozen_lines)


def map_browse_page(
    page: object,
    *,
    row_key: str,
    requested_limit: int,
    requested_offset: int,
) -> BrowsePageDTO:
    """Validate an adapter browse page and bind it to this exact request."""
    if (
        type(page) is not dict
        or type(row_key) is not str
        or type(requested_limit) is not int
        or type(requested_offset) is not int
        or set(page) != {"total", "limit", "offset", "has_more", "next_offset", row_key}
    ):
        raise ValueError("invalid browse page")
    rows = page[row_key]
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise ValueError("invalid browse rows")
    total = _page_number(page["total"], "browse total", allow_none=False)
    limit = _page_number(page["limit"], "browse limit", allow_none=False)
    offset = _page_number(page["offset"], "browse offset", allow_none=False)
    next_offset = _page_number(page["next_offset"], "browse next offset", allow_none=True)
    has_more = page["has_more"]
    if total is None or limit is None or offset is None:
        raise ValueError("invalid browse page")
    if (
        limit != requested_limit
        or offset != requested_offset
        or not 1 <= limit <= 250
        or offset > total
        or len(rows) != min(limit, total - offset)
        or offset + len(rows) > total
        or has_more is not (offset + limit < total)
        or (has_more is True and next_offset != offset + limit)
        or (has_more is False and next_offset is not None)
    ):
        raise ValueError("invalid browse page")
    return BrowsePageDTO(tuple(rows), total, limit, offset, has_more, next_offset)


def _page_number(value: object, label: str, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def _source_line_references(
    source_type: str,
    document_id: str,
    line_page: tuple[tuple[tuple[str, object], ...], ...],
    source_sha256: str,
) -> tuple[SourceLineReference, ...]:
    """Freeze exact source coordinates without creating a packing plan or write path."""
    references = tuple(
        SourceLineReference(
            source_type,
            document_id,
            _text(dict(line).get("line_no"), "source line id"),
            source_sha256,
        )
        for line in line_page
    )
    if len({reference.line_id for reference in references}) != len(references):
        raise ValueError("duplicate source line id")
    return references


def map_source_order(
    order: object,
    *,
    source_sha256: object,
    strict_served_page: bool = False,
    requested_limit: int | None = None,
    requested_offset: int | None = None,
    requested_order_id: str | None = None,
) -> SourceRenderDTO:
    """Map one independently-read source order without adding any action surface."""
    _reject_keys_anywhere(order, _MUTATION_CONTROL_KEYS, "source data cannot carry mutation controls")
    if type(order) is not dict or "invoice_id" in order or "source_sha256" in order:
        raise ValueError("invalid source order")
    if strict_served_page and set(order) != {
        "order_id", "order_date", "customer_id", "customer_name", "lines", "total", "limit", "offset", "has_more", "next_offset"
    }:
        raise ValueError("invalid source order page")
    order_id = _text(order.get("order_id"), "source order id")
    if strict_served_page and (type(requested_order_id) is not str or order_id != requested_order_id):
        raise ValueError("source order does not match request")
    customer_id = _text(order.get("customer_id"), "customer id")
    customer_name = _text(order.get("customer_name"), "customer name")
    lines = _line_page(
        order.get("lines"),
        forbid_order_provenance=False,
        allowed_keys=_SOURCE_ORDER_LINE_FIELDS if strict_served_page else None,
        require_exact_keys=strict_served_page,
    )
    if strict_served_page:
        for line in lines:
            fields = dict(line)
            if (
                fields.get("description_th_provenance") not in {"order", "item_master"}
                or fields.get("description_en_provenance") not in {"order", "item_master"}
            ):
                raise ValueError("invalid source order description provenance")
    total = _page_number(order.get("total", len(lines)), "line total", allow_none=False)
    next_offset = _page_number(order.get("next_offset"), "next offset", allow_none=True)
    if total is None or total < len(lines):
        raise ValueError("invalid line total")
    order_date = line_limit = line_offset = has_more = None
    if strict_served_page:
        order_date = _text(order.get("order_date"), "order date")
        line_limit = _page_number(order.get("limit"), "line limit", allow_none=False)
        line_offset = _page_number(order.get("offset"), "line offset", allow_none=False)
        has_more = order.get("has_more")
        if (
            line_limit is None
            or type(requested_limit) is not int
            or type(requested_offset) is not int
            or line_limit != requested_limit
            or line_offset != requested_offset
            or not 1 <= line_limit <= 250
            or line_offset is None
            or line_offset > total
            or len(lines) != min(line_limit, total - line_offset)
            or line_offset + len(lines) > total
            or has_more is not (line_offset + line_limit < total)
            or (has_more is True and next_offset != line_offset + line_limit)
            or (has_more is False and next_offset is not None)
        ):
            raise ValueError("invalid source order page")
    snapshot_hash = _source_sha256(source_sha256)
    return SourceRenderDTO(
        record_type="source_order",
        record_id=f"source_order:{order_id}",
        source_sha256=snapshot_hash,
        customer_id=customer_id,
        customer_name=customer_name,
        line_page=lines,
        line_total=total,
        next_offset=next_offset,
        line_references=_source_line_references("source_order", order_id, lines, snapshot_hash),
        read_only=True,
        document_date=order_date,
        line_limit=line_limit,
        line_offset=line_offset,
        has_more=has_more,
    )


def map_source_invoice(invoice: object, *, source_sha256: object = None) -> SourceRenderDTO:
    """Map one independently-read source invoice; it never claims order provenance."""
    _reject_keys_anywhere(invoice, _MUTATION_CONTROL_KEYS, "source data cannot carry mutation controls")
    _reject_keys_anywhere(
        invoice,
        _SOURCE_INVOICE_ORDER_PROVENANCE_KEYS,
        "source invoice cannot carry order provenance",
    )
    if type(invoice) is not dict or "order_id" in invoice:
        raise ValueError("invalid source invoice")
    embedded_sha256 = invoice.get("source_sha256")
    if source_sha256 is None:
        source_sha256 = embedded_sha256
    elif embedded_sha256 is not None and embedded_sha256 != source_sha256:
        raise ValueError("conflicting source sha256")
    invoice_id = _text(invoice.get("invoice_id"), "source invoice id")
    header = invoice.get("header")
    if type(header) is not dict:
        raise ValueError("invalid source invoice header")
    if not set(header).issubset(_SOURCE_INVOICE_HEADER_FIELDS):
        raise ValueError("invalid source invoice header")
    customer_id = _text(header.get("customer_id"), "customer id")
    customer_name = _text(header.get("customer_name"), "customer name")
    lines = _line_page(
        invoice.get("lines"),
        forbid_order_provenance=True,
        allowed_keys=_SOURCE_INVOICE_LINE_FIELDS,
    )
    total = _page_number(invoice.get("total"), "line total", allow_none=False)
    if total is None:
        raise ValueError("invalid line total")
    next_offset = _page_number(invoice.get("next_offset"), "next offset", allow_none=True)
    if total < len(lines):
        raise ValueError("invalid line total")
    snapshot_hash = _source_sha256(source_sha256)
    return SourceRenderDTO(
        record_type="source_invoice",
        record_id=f"source_invoice:{invoice_id}",
        source_sha256=snapshot_hash,
        customer_id=customer_id,
        customer_name=customer_name,
        line_page=lines,
        line_total=total,
        next_offset=next_offset,
        line_references=_source_line_references("source_invoice", invoice_id, lines, snapshot_hash),
        read_only=True,
    )


def _browse_text(row: dict[str, object], key: str) -> str:
    return _text(row.get(key), key.replace("_", " "))


def map_source_order_browse(row: object) -> BrowseRenderDTO:
    """Reject hostile source-order summaries before browser projection."""
    _reject_keys_anywhere(row, _MUTATION_CONTROL_KEYS, "source data cannot carry mutation controls")
    if type(row) is not dict or set(row) != {"order_id", "order_date", "customer_id"}:
        raise ValueError("invalid source order browse row")
    order_id = _browse_text(row, "order_id")
    return BrowseRenderDTO(
        record_type="source_order",
        record_id=f"source_order:{order_id}",
        fields=tuple((key, _browse_text(row, key)) for key in ("order_id", "order_date", "customer_id")),
        read_only=True,
    )


def map_source_invoice_browse(row: object) -> BrowseRenderDTO:
    """Reject hostile invoice summary rows before browser projection."""
    _reject_keys_anywhere(row, _MUTATION_CONTROL_KEYS, "source data cannot carry mutation controls")
    _reject_keys_anywhere(row, _SOURCE_INVOICE_ORDER_PROVENANCE_KEYS, "source invoice cannot carry order provenance")
    allowed = {"source_type", "invoice_id", "invoice_date", "customer_id", "customer_name", "slash_family"}
    if type(row) is not dict or set(row) != allowed or row.get("source_type") != "source_invoice":
        raise ValueError("invalid source invoice browse row")
    invoice_id = _browse_text(row, "invoice_id")
    return BrowseRenderDTO(
        record_type="source_invoice",
        record_id=f"source_invoice:{invoice_id}",
        fields=tuple((key, _browse_text(row, key)) for key in ("invoice_id", "invoice_date", "customer_id", "customer_name", "slash_family")),
        read_only=True,
    )


def map_core_draft_browse(row: object) -> BrowseRenderDTO:
    """Reject source provenance and mutation controls from Core draft summaries."""
    _reject_keys_anywhere(row, frozenset({"source_sha256", "accepted_snapshot_sha256"}), "core draft cannot carry source provenance")
    _reject_keys_anywhere(row, _MUTATION_CONTROL_KEYS, "core draft cannot carry mutation controls")
    allowed = {"draft_id", "created_by", "created_at", "status"}
    if type(row) is not dict or set(row) != allowed:
        raise ValueError("invalid core draft browse row")
    draft_id = _browse_text(row, "draft_id")
    return BrowseRenderDTO(
        record_type="core_draft",
        record_id=f"core_draft:{draft_id}",
        fields=tuple((key, _browse_text(row, key)) for key in ("draft_id", "created_by", "created_at", "status")),
        read_only=True,
    )


def map_core_draft(draft: object) -> CoreDraftRenderDTO:
    """Map a Core-owned draft as a distinct non-source render record."""
    _reject_keys_anywhere(draft, frozenset({"source_sha256"}), "core draft cannot carry source provenance")
    if type(draft) is not dict or "source_sha256" in draft:
        raise ValueError("invalid core draft")
    draft_id = _text(draft.get("draft_id"), "core draft id")
    customer_id = _text(draft.get("customer_id"), "customer id")
    customer_name = _text(draft.get("customer_name"), "customer name")
    lines = _line_page(draft.get("lines"), forbid_order_provenance=False)
    return CoreDraftRenderDTO(
        record_type="core_draft",
        record_id=f"core_draft:{draft_id}",
        customer_id=customer_id,
        customer_name=customer_name,
        line_page=lines,
        line_total=len(lines),
        next_offset=None,
        read_only=True,
    )
