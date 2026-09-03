"""Pure, fixture-driven invoice detail HTML."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape

from apc_core.staff_dates import format_staff_timestamp


_STATES = {"Temporary", "Real", "Cancelled", "Corrected"}


def _required_text(invoice: Mapping[str, object], key: str) -> str:
    value = invoice.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _text(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _history_link(label: str, record: object) -> str:
    if not isinstance(record, Mapping):
        return ""
    document_id = _required_text(record, "document_id")
    description = _required_text(record, "label")
    return (
        f'<dt>{label}</dt><dd><a class="history-link" href="#invoice-{_text(document_id)}">'
        f'{_text(description)} <span class="history-id">({_text(document_id)})</span></a></dd>'
    )


def _line_rows(lines: object) -> str:
    if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)):
        raise ValueError("lines must be a sequence")
    rows: list[str] = []
    for line in lines:
        if not isinstance(line, Mapping):
            raise ValueError("each line must be a mapping")
        price = line.get("price")
        price_cell = '<span class="no-price">No price</span>' if price in (None, "") else _text(price)
        rows.append(
            "<tr>"
            f"<td>{_text(line.get('item'))}</td>"
            f"<td>{_text(line.get('quantity'))}</td>"
            f"<td>{price_cell}</td>"
            f"<td>{_text(line.get('line_note'))}</td>"
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="4">No line items recorded.</td></tr>'


_P5_STATE_LABELS = {
    "temporary": "Temporary",
    "real": "Real",
    "cancelled": "Cancelled",
}
_P5_RECEIPT_REQUIRED_KEYS = (
    "invoice_id",
    "state",
    "version",
    "permanent_number",
    "temporary_reference",
    "consignee",
    "delivery_reference",
)


def _p5_receipt(receipt: object) -> Mapping[str, object]:
    """Validate one caller-supplied receipt without accessing any external boundary."""
    if not isinstance(receipt, Mapping):
        raise ValueError("P5 receipt must be a mapping")
    for key in _P5_RECEIPT_REQUIRED_KEYS:
        if key in {"version", "permanent_number"}:
            continue
        _required_text(receipt, key)
    version = receipt["version"]
    if type(version) is not int or version < 1:
        raise ValueError("P5 receipt version must be a positive integer")
    state = receipt["state"]
    if state not in _P5_STATE_LABELS:
        raise ValueError("P5 receipt state must be temporary, real, or cancelled")
    permanent_number = receipt["permanent_number"]
    if permanent_number is not None and (not isinstance(permanent_number, str) or not permanent_number):
        raise ValueError("permanent_number must be text or null")
    if state == "real" and permanent_number is None:
        raise ValueError("real P5 receipt requires permanent_number")
    return receipt


def _optional_text(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text or null")
    return value


def p5_receipt_to_detail_view_model(
    receipt: Mapping[str, object],
    *,
    staff_name: str,
    customer_name: str,
    evidence_reference: str,
    old_system_notes: str | None,
    lines: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Adapt a supplied P5 receipt plus display-only fixture fields for detail rendering."""
    source = _p5_receipt(receipt)
    if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)):
        raise ValueError("lines must be a sequence")
    for line in lines:
        if not isinstance(line, Mapping):
            raise ValueError("each line must be a mapping")
    result: dict[str, object] = {
        "document_id": _required_text(source, "invoice_id"),
        "state": _P5_STATE_LABELS[_required_text(source, "state")],
        "staff_name": _required_text({"staff_name": staff_name}, "staff_name"),
        "customer_name": _required_text({"customer_name": customer_name}, "customer_name"),
        "evidence_reference": _required_text({"evidence_reference": evidence_reference}, "evidence_reference"),
        "old_system_notes": _optional_text(old_system_notes, "old_system_notes"),
        "lines": list(lines),
    }
    if source["state"] == "real":
        result["permanent_number"] = _required_text(source, "permanent_number")
    correction_of = source.get("correction_of")
    if correction_of is not None:
        result["replaces"] = {
            "document_id": _required_text({"correction_of": correction_of}, "correction_of"),
            "label": "Corrects invoice",
        }
    return result


def p5_receipt_to_list_view_model(
    receipt: Mapping[str, object],
    *,
    customer_code: str,
    customer_name: str,
    evidence_reference: str,
    staff_name: str,
    recorded_at: str,
    reviewed_at: str | None,
    order_number: str | None = None,
) -> dict[str, object]:
    """Adapt a supplied P5 receipt plus display-only fixture fields for list rendering."""
    source = _p5_receipt(receipt)
    external = {
        "customer_code": customer_code,
        "customer_name": customer_name,
        "evidence_reference": evidence_reference,
        "staff_name": staff_name,
        "recorded_at": recorded_at,
    }
    for key in external:
        _required_text(external, key)
    result: dict[str, object] = {
        "display_reference": _required_text(source, "temporary_reference"),
        "customer_code": customer_code,
        "customer_name": customer_name,
        "consignee": _required_text(source, "consignee"),
        "delivery_po_reference": _required_text(source, "delivery_reference"),
        "evidence_reference": evidence_reference,
        "state": _P5_STATE_LABELS[_required_text(source, "state")],
        "staff_name": staff_name,
        "recorded_at": recorded_at,
    }
    if reviewed_at is not None:
        result["reviewed_at"] = _required_text({"reviewed_at": reviewed_at}, "reviewed_at")
    if order_number is not None:
        result["order_number"] = _required_text({"order number": order_number}, "order number")
    return result


def invoice_detail_html(invoice: Mapping[str, object]) -> str:
    """Render one invoice from supplied fixture data without any runtime dependency."""
    if not isinstance(invoice, Mapping):
        raise ValueError("invoice must be a mapping")
    state = _required_text(invoice, "state")
    if state not in _STATES:
        raise ValueError("state must be Temporary, Real, Cancelled, or Corrected")
    document_id = _required_text(invoice, "document_id")
    number = _required_text(invoice, "permanent_number") if state == "Real" else "No number yet"
    history = _history_link("Replaces", invoice.get("replaces")) + _history_link("Replaced by", invoice.get("replaced_by"))
    state_class = state.lower()
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice</title>
<style>
:root{{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#eadbc8;--paper:#fffdfa;--accent:#1d6b57;--temporary:#7252a3;--real:#176b52;--cancelled:#a2413a;--corrected:#9a6515}}*{{box-sizing:border-box}}body{{margin:0;background:var(--cream);color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}}.shell{{max-width:900px;margin:auto;padding:28px}}.card{{background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:4px 4px 0 #24272b22;padding:24px}}h1,h2{{line-height:1.2}}.eyebrow{{color:var(--muted);font-size:.85rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase}}.state-badge{{display:inline-flex;align-items:center;min-height:28px;border:1px solid currentColor;border-radius:999px;padding:3px 10px;font-weight:800}}.state-badge--temporary{{color:var(--temporary);background:#eee8fa}}.state-badge--real{{color:var(--real);background:#dcefe5}}.state-badge--cancelled{{color:var(--cancelled);background:#fae8e5}}.state-badge--corrected{{color:var(--corrected);background:#faefd9}}.state-copy{{margin-left:8px;font-weight:700}}dl{{display:grid;grid-template-columns:minmax(9rem,auto) 1fr;gap:8px 16px}}dt{{font-weight:800}}dd{{margin:0}}.history{{margin-top:20px;padding-top:16px;border-top:1px solid var(--line)}}.history-link{{display:inline-flex;align-items:center;min-height:44px;color:var(--accent);font-weight:750}}.history-link:focus-visible{{outline:3px solid var(--accent);outline-offset:3px;border-radius:4px}}.history-id{{color:var(--muted);font-weight:600;margin-left:4px}}table{{width:100%;border-collapse:collapse;margin-top:12px}}caption{{text-align:left;font-weight:800;margin-bottom:8px}}th,td{{padding:10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:#f7f2eb}}.no-price{{font-weight:800;color:#6b3b18}}.history-only{{color:var(--muted);background:#f7f2eb;border-left:4px solid #b49770;padding:12px}}@media(max-width:620px){{.shell{{padding:16px}}.card{{padding:18px}}dl{{grid-template-columns:1fr}}th,td{{padding:8px}}}}
</style>
</head>
<body>
<main class="shell" id="invoice-{_text(document_id)}">
<article class="card" aria-labelledby="invoice-title">
<p class="eyebrow">Invoice detail · display only</p>
<h1 id="invoice-title">Invoice</h1>
<p><span class="state-badge state-badge--{state_class}" aria-label="Invoice state: {_text(state)}">{_text(state)}</span><span class="state-copy">State is stated in text, not color alone.</span></p>
<dl aria-label="Invoice attribution and reference">
<dt>Document reference</dt><dd>{_text(document_id)}</dd>
<dt>Number</dt><dd>{_text(number)}</dd>
<dt>Created by</dt><dd>{_text(_required_text(invoice, 'staff_name'))}</dd>
<dt>Customer</dt><dd>{_text(_required_text(invoice, 'customer_name'))}</dd>
<dt>Evidence reference</dt><dd>{_text(_required_text(invoice, 'evidence_reference'))}</dd>
</dl>
<section aria-labelledby="invoice-lines-title"><h2 id="invoice-lines-title">Lines</h2><table><caption>Invoice line details</caption><thead><tr><th scope="col">Item</th><th scope="col">Quantity</th><th scope="col">Price</th><th scope="col">Line note</th></tr></thead><tbody>{_line_rows(invoice.get('lines'))}</tbody></table></section>
<section class="history" aria-labelledby="history-title"><h2 id="history-title">History</h2><p class="history-only"><strong>Old system notes (history only)</strong><br>{_text(invoice.get('old_system_notes') or 'No old-system notes recorded.')}</p><dl>{history}</dl></section>
</article>
</main>
</body>
</html>'''


_LIST_STATES = ("All", "Temporary", "Real", "Cancelled", "Corrected")
_LIST_SEARCH_KEYS = (
    "customer_code",
    "display_reference",
    "order_number",
)
_LIST_REQUIRED_KEYS = (
    "display_reference",
    "customer_code",
    "customer_name",
    "consignee",
    "delivery_po_reference",
    "evidence_reference",
    "state",
    "staff_name",
    "recorded_at",
)


def _list_record(record: object) -> Mapping[str, object]:
    if not isinstance(record, Mapping):
        raise ValueError("each invoice record must be a mapping")
    for key in _LIST_REQUIRED_KEYS:
        _required_text(record, key)
    if "order_number" in record:
        _required_text(record, "order_number")
    if _required_text(record, "state") not in _STATES:
        raise ValueError("invoice record has an unsupported state")
    return record


def filter_invoice_list(
    records: Sequence[Mapping[str, object]], *, search: str = "", state: str = "All"
) -> tuple[Mapping[str, object], ...]:
    """Select supplied invoice fixture records as a pure derived result."""
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError("records must be a sequence")
    if not isinstance(search, str):
        raise ValueError("search must be text")
    if state not in _LIST_STATES:
        raise ValueError("state must be All, Temporary, Real, Cancelled, or Corrected")
    needle = search.casefold().strip()
    selected: list[Mapping[str, object]] = []
    for candidate in records:
        record = _list_record(candidate)
        if state != "All" and record["state"] != state:
            continue
        if needle and not any(
            needle in str(record.get(key, "")).casefold() for key in _LIST_SEARCH_KEYS
        ):
            continue
        selected.append(record)
    return tuple(selected)


def _list_rows(records: Sequence[Mapping[str, object]]) -> str:
    rows: list[str] = []
    for record in records:
        state = _required_text(record, "state")
        reviewed_at = record.get("reviewed_at")
        reviewed = "" if reviewed_at is None else f'<br><span>Last reviewed {_text(format_staff_timestamp(reviewed_at))}</span>'
        rows.append(
            f'''<tr class="invoice-list__row">
<td class="invoice-list__primary">{_text(record["display_reference"])}<br><span>{_text(record["customer_code"])}</span></td>
<td>{_text(record["customer_name"])}</td>
<td>{_text(record["consignee"])}</td>
<td>{_text(record["delivery_po_reference"])}</td>
<td>{_text(record["evidence_reference"])}</td>
<td><span class="state-badge state-badge--{state.lower()}" aria-label="Invoice state: {_text(state)}">{_text(state)}</span></td>
<td>Recorded {_text(format_staff_timestamp(record["recorded_at"]))}{reviewed}</td>
</tr>'''
        )
    return "".join(rows) or '<tr><td colspan="7">No matching invoice records.</td></tr>'


def invoice_list_html(
    records: Sequence[Mapping[str, object]], *, search: str = "", state: str = "All"
) -> str:
    """Render a static invoice-list candidate from supplied fixture records only."""
    matches = filter_invoice_list(records, search=search, state=state)
    timestamp_heading = "Recorded / reviewed" if any("reviewed_at" in record for record in matches) else "Recorded"
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice list</title>
<style>
:root{{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#eadbc8;--paper:#fffdfa;--temporary:#7252a3;--real:#176b52;--cancelled:#a2413a;--corrected:#9a6515}}*{{box-sizing:border-box}}body{{margin:0;background:var(--cream);color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}}.invoice-list-shell{{max-width:1240px;margin:auto;padding:24px}}.invoice-list-card{{overflow-x:auto;background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:4px 4px 0 #24272b22;padding:20px}}h1{{margin:0;line-height:1.2}}.invoice-list__eyebrow,.invoice-list__summary{{color:var(--muted);font-weight:700}}.invoice-list__summary{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}.invoice-list__summary strong{{color:var(--ink)}}table{{width:100%;min-width:1040px;border-collapse:collapse}}caption{{padding:8px 0;text-align:left;font-weight:800}}th,td{{padding:12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:#f7f2eb;font-size:.82rem;letter-spacing:.02em}}.invoice-list__row{{min-height:44px}}.invoice-list__primary{{font-weight:850;color:#173f35}}.invoice-list__primary span,td span{{color:var(--muted);font-weight:650}}.state-badge{{display:inline-flex;align-items:center;min-height:28px;border:1px solid currentColor;border-radius:999px;padding:3px 10px;font-weight:800}}.state-badge--temporary{{color:var(--temporary);background:#eee8fa}}.state-badge--real{{color:var(--real);background:#dcefe5}}.state-badge--cancelled{{color:var(--cancelled);background:#fae8e5}}.state-badge--corrected{{color:var(--corrected);background:#faefd9}}@media(max-width:620px){{.invoice-list-shell{{padding:14px}}.invoice-list-card{{padding:14px}}}}
</style>
</head>
<body>
<main class="invoice-list-shell" aria-labelledby="invoice-list-title">
<article class="invoice-list-card">
<p class="invoice-list__eyebrow">Fixture display · read only</p>
<h1 id="invoice-list-title">Invoice list</h1>
<section class="invoice-list__summary" aria-label="Invoice list filters"><strong>Search by customer code, invoice reference, or order number.</strong><span>Search: {_text(search or "All records")}</span><span>Filter: {_text(state)}</span><span>{len(matches)} matching record{'s' if len(matches) != 1 else ''}</span></section>
<section aria-label="Invoice list results"><table><caption>Matching invoice records</caption><thead><tr><th scope="col">Reference</th><th scope="col">Customer</th><th scope="col">Consignee</th><th scope="col">Delivery / PO ref</th><th scope="col">Evidence reference</th><th scope="col">State</th><th scope="col">{timestamp_heading}</th></tr></thead><tbody>{_list_rows(matches)}</tbody></table></section>
</article>
</main>
</body>
</html>'''
