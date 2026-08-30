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


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"invalid {label}")
    return value


def _source_sha256(value: object) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in _HEX for character in value):
        raise ValueError("invalid source sha256")
    return value


def _line_page(
    value: object, *, forbid_order_provenance: bool, allowed_keys: tuple[str, ...] | None = None
) -> tuple[tuple[tuple[str, object], ...], ...]:
    if type(value) is not list:
        raise ValueError("invalid line page")
    frozen_lines: list[tuple[tuple[str, object], ...]] = []
    for line in value:
        if type(line) is not dict:
            raise ValueError("invalid line")
        if forbid_order_provenance and "order_id" in line:
            raise ValueError("source invoice cannot carry order provenance")
        if allowed_keys is not None and not set(line).issubset(allowed_keys):
            raise ValueError("invalid source invoice line")
        frozen_line: list[tuple[str, object]] = []
        items = ((key, line[key]) for key in allowed_keys if key in line) if allowed_keys is not None else line.items()
        for key, item in items:
            if type(key) is not str or type(item) not in (str, int, float, bool, type(None)):
                raise ValueError("invalid render line")
            frozen_line.append((key, item))
        frozen_lines.append(tuple(frozen_line))
    return tuple(frozen_lines)


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


def map_source_order(order: object, *, source_sha256: object) -> SourceRenderDTO:
    """Map one independently-read source order without adding any action surface."""
    _reject_keys_anywhere(order, _MUTATION_CONTROL_KEYS, "source data cannot carry mutation controls")
    if type(order) is not dict or "invoice_id" in order or "source_sha256" in order:
        raise ValueError("invalid source order")
    order_id = _text(order.get("order_id"), "source order id")
    customer_id = _text(order.get("customer_id"), "customer id")
    customer_name = _text(order.get("customer_name"), "customer name")
    lines = _line_page(order.get("lines"), forbid_order_provenance=False)
    total = _page_number(order.get("total", len(lines)), "line total", allow_none=False)
    next_offset = _page_number(order.get("next_offset"), "next offset", allow_none=True)
    if total is None or total < len(lines):
        raise ValueError("invalid line total")
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
