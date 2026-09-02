"""Pure Core-shaped invoice projection for display-only UI adapters."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from apc_core.invoice_workflow_ui import (
    p5_receipt_to_detail_view_model,
    p5_receipt_to_list_view_model,
)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _receipt(invoice: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(invoice.get("receipt"), "receipt")


def _customer(invoice: Mapping[str, object]) -> tuple[str, str]:
    customer = _mapping(invoice.get("customer"), "customer")
    customer_code = _text(customer.get("customer_code"), "customer code")
    approved_name = customer.get("approved_name")
    if approved_name is None or (isinstance(approved_name, str) and not approved_name.strip()):
        return customer_code, customer_code
    return customer_code, _text(approved_name, "approved customer name")


def _lines(invoice: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    source = invoice.get("lines")
    if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
        raise ValueError("lines must be a sequence")
    result: list[dict[str, object]] = []
    for line in source:
        item = _mapping(line, "invoice line")
        result.append(
            {
                "item": _text(item.get("item"), "line item"),
                "quantity": _text(item.get("quantity"), "line quantity"),
                "price": item.get("current_price"),
                "line_note": item.get("line_note", ""),
            }
        )
    return tuple(result)


def _reviewed_at(invoice: Mapping[str, object]) -> str | None:
    event = invoice.get("review_event")
    if event is None:
        return None
    return _text(_mapping(event, "review event").get("recorded_at"), "review event timestamp")


def project_invoice_detail(invoice: Mapping[str, object]) -> dict[str, object]:
    """Prepare one supplied Core-shaped record for the existing pure detail adapter."""
    source = _mapping(invoice, "invoice")
    _, customer_name = _customer(source)
    detail = p5_receipt_to_detail_view_model(
        _receipt(source),
        staff_name=_text(source.get("created_by"), "created by"),
        customer_name=customer_name,
        evidence_reference=_text(source.get("evidence_reference"), "evidence reference"),
        old_system_notes=None,
        lines=_lines(source),
    )
    replacement = source.get("replaced_by")
    if replacement is not None:
        record = _mapping(replacement, "replacement")
        detail["replaced_by"] = {
            "document_id": _text(record.get("document_id"), "replacement document id"),
            "label": f"Replaced by {_text(record.get('label'), 'replacement label')}",
        }
    return detail


def project_invoice_list(invoice: Mapping[str, object]) -> dict[str, object]:
    """Prepare one supplied Core-shaped record for the existing pure list adapter."""
    source = _mapping(invoice, "invoice")
    customer_code, customer_name = _customer(source)
    return p5_receipt_to_list_view_model(
        _receipt(source),
        customer_code=customer_code,
        customer_name=customer_name,
        evidence_reference=_text(source.get("evidence_reference"), "evidence reference"),
        staff_name=_text(source.get("created_by"), "created by"),
        recorded_at=_text(source.get("created_at"), "created at"),
        reviewed_at=_reviewed_at(source),
        order_number=_optional_text(source.get("order_number"), "order number"),
    )
