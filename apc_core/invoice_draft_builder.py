"""Pure, unsaved invoice-draft preview construction over caller-provided DTOs only."""

import hashlib
import json


def _canonical_digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_mapping(value, label):
    if type(value) is not dict:
        raise ValueError(f"invalid {label}")
    return value


def _line_preview(order_id, line):
    _require_mapping(line, "selected line")
    required = ("line_ref", "item_id", "quantity")
    if any(type(line.get(field)) is not str or not line[field] for field in required):
        raise ValueError("invalid selected line")
    result = {"order_id": order_id, "line_ref": line["line_ref"], "item_id": line["item_id"], "quantity": line["quantity"]}
    for field in ("unit_price", "source_annotation", "source_unit_price"):
        if field in line:
            if type(line[field]) is not str:
                raise ValueError("invalid selected line")
            result[field] = line[field]
    if "current_price" in line:
        current_price = line["current_price"]
        if (
            type(current_price) is not dict
            or set(current_price) != {"status", "value"}
            or type(current_price["status"]) is not str
            or type(current_price["value"]) is not str
        ):
            raise ValueError("invalid selected line")
        result["current_price"] = dict(current_price)
    return result


def _decision_preview(decision, conflicts):
    _require_mapping(decision, "decision")
    conflict_id = decision.get("conflict_id")
    if type(conflict_id) is not str or conflict_id not in conflicts:
        raise ValueError("unknown conflict decision")
    existing_value = decision.get("chosen_existing_value")
    existing_source = decision.get("chosen_existing_source")
    manual_value = decision.get("manual_value")
    rationale = decision.get("rationale")
    chose_existing = type(existing_value) is str and type(existing_source) is str and manual_value is None and rationale is None
    chose_manual = type(manual_value) is str and type(rationale) is str and manual_value and rationale and existing_value is None and existing_source is None
    if chose_existing:
        allowed = conflicts[conflict_id].get("existing_values", [])
        if not any(type(value) is dict and value.get("value") == existing_value and value.get("source") == existing_source for value in allowed):
            raise ValueError("unknown chosen existing value")
        return {"conflict_id": conflict_id, "chosen_existing_value": existing_value, "chosen_existing_source": existing_source}
    if manual_value is not None and not rationale:
        raise ValueError("manual decision requires rationale")
    if chose_manual:
        return {"conflict_id": conflict_id, "manual_value": manual_value, "rationale": rationale}
    raise ValueError("invalid decision")


def build_invoice_draft(source_provenance, orders, selected_order_ids, decisions):
    """Return a deterministic preview; this function never persists or issues an invoice."""
    provenance = _require_mapping(source_provenance, "source provenance")
    accepted_snapshot_sha256 = provenance.get("accepted_snapshot_sha256")
    if (
        type(accepted_snapshot_sha256) is not str
        or len(accepted_snapshot_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in accepted_snapshot_sha256)
    ):
        raise ValueError("invalid source provenance")
    if type(orders) not in (list, tuple) or type(selected_order_ids) not in (list, tuple) or type(decisions) not in (list, tuple):
        raise ValueError("invalid draft inputs")
    if not selected_order_ids:
        raise ValueError("explicit selection is required")
    if any(type(order_id) is not str or not order_id for order_id in selected_order_ids):
        raise ValueError("invalid explicit selection")
    if len(set(selected_order_ids)) != len(selected_order_ids):
        raise ValueError("duplicate selected order")

    by_id = {}
    for candidate in orders:
        _require_mapping(candidate, "order")
        order_id = candidate.get("order_id")
        if type(order_id) is not str or not order_id or order_id in by_id:
            raise ValueError("invalid order")
        by_id[order_id] = candidate
    if any(order_id not in by_id for order_id in selected_order_ids):
        raise ValueError("unknown selected order")

    selected = [candidate for candidate in orders if candidate["order_id"] in set(selected_order_ids)]
    customers = {entry.get("customer_id") for entry in selected}
    if len(customers) != 1 or type(next(iter(customers))) is not str or not next(iter(customers)):
        raise ValueError("mixed customers")
    families = {entry.get("document_family") for entry in selected}
    if len(families) != 1 or type(next(iter(families))) is not str or not next(iter(families)):
        raise ValueError("unsupported document-family mixing")

    lines, annotations, seen_refs = [], [], set()
    conflicts, unresolved = {}, []
    for entry in selected:
        order_id = entry["order_id"]
        order_lines = entry.get("lines")
        if type(order_lines) not in (list, tuple) or not order_lines:
            raise ValueError("empty selected order")
        for line in order_lines:
            frozen = _line_preview(order_id, line)
            ref_key = (order_id, frozen["line_ref"])
            if ref_key in seen_refs:
                raise ValueError("duplicate selected line ref")
            seen_refs.add(ref_key)
            lines.append(frozen)
            if "source_annotation" in frozen:
                annotations.append({"order_id": order_id, "line_ref": frozen["line_ref"], "value": frozen["source_annotation"]})
            if (
                "source_unit_price" in frozen
                and (
                    not frozen["source_unit_price"]
                    or frozen.get("current_price", {}).get("status") == "UNKNOWN"
                    or not frozen.get("current_price", {}).get("value", "")
                )
            ):
                unresolved.append({"reason": "source/current price unresolved", "order_id": order_id, "line_ref": frozen["line_ref"]})
        extra_annotations = entry.get("annotations", ())
        if type(extra_annotations) not in (list, tuple):
            raise ValueError("invalid annotations")
        for annotation in extra_annotations:
            _require_mapping(annotation, "annotation")
            line_ref = annotation.get("line_ref")
            value = annotation.get("value")
            if type(line_ref) is not str or not line_ref or type(value) is not str or not value:
                raise ValueError("invalid annotation")
            annotations.append({"order_id": order_id, "line_ref": line_ref, "value": value})
        rule = entry.get("pricing_rule")
        if rule is not None:
            if type(rule) is not str or not rule:
                raise ValueError("invalid pricing rule")
            unresolved.append({"reason": "unsupported pricing rule", "order_id": order_id, "pricing_rule": rule})
        shipment_conflicts = entry.get("shipment_conflicts", [])
        if type(shipment_conflicts) not in (list, tuple):
            raise ValueError("invalid shipment conflicts")
        for conflict in shipment_conflicts:
            _require_mapping(conflict, "shipment conflict")
            conflict_id = conflict.get("conflict_id")
            if type(conflict_id) is not str or not conflict_id or conflict_id in conflicts:
                raise ValueError("invalid shipment conflict")
            if type(conflict.get("required", False)) is not bool:
                raise ValueError("invalid shipment conflict")
            existing_values = conflict.get("existing_values", [])
            if type(existing_values) not in (list, tuple):
                raise ValueError("invalid shipment conflict")
            conflicts[conflict_id] = {"required": conflict.get("required", False), "existing_values": list(existing_values)}

    frozen_decisions = []
    decided = set()
    for decision in decisions:
        frozen = _decision_preview(decision, conflicts)
        if frozen["conflict_id"] in decided:
            raise ValueError("duplicate conflict decision")
        decided.add(frozen["conflict_id"])
        frozen_decisions.append(frozen)
    for conflict_id, conflict in conflicts.items():
        if conflict["required"] and conflict_id not in decided:
            unresolved.append({"conflict_id": conflict_id, "reason": "required shipment conflict unresolved"})

    material = {
        "source_provenance": {key: provenance[key] for key in sorted(provenance) if type(provenance[key]) in (str, int, float, bool, type(None))},
        "selected_order_ids": list(selected_order_ids),
        "lines": lines,
        "annotations": annotations,
        "decisions": frozen_decisions,
    }
    return {
        "selected_order_ids": tuple(selected_order_ids),
        "customer_id": next(iter(customers)),
        "document_family": next(iter(families)),
        "lines": tuple(lines),
        "annotations": tuple(annotations),
        "decisions": tuple(frozen_decisions),
        "unresolved": tuple(unresolved),
        "ready_to_save": not unresolved,
        "idempotency_material": _canonical_digest(material),
    }
