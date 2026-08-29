"""Persist only an already-frozen, ready Core invoice-draft proposal."""

from __future__ import annotations

from apc_core.invoice_drafts import InvoiceDraftStore


_REQUIRED_PROPOSAL_KEYS = frozenset(
    {
        "selected_order_ids",
        "customer_id",
        "document_family",
        "lines",
        "annotations",
        "decisions",
        "unresolved",
        "ready_to_save",
        "idempotency_material",
    }
)
_LINE_KEYS = frozenset({"order_id", "line_ref", "item_id", "quantity", "unit_price", "source_annotation"})
_DECISION_KEYS = (
    frozenset({"conflict_id", "chosen_existing_value", "chosen_existing_source"}),
    frozenset({"conflict_id", "manual_value", "rationale"}),
)
_HEX = frozenset("0123456789abcdef")


class InvoiceDraftService:
    """Validate and save a caller-provided frozen preview; it never reads a source."""

    def __init__(self, store: InvoiceDraftStore):
        if type(store) is not InvoiceDraftStore:
            raise ValueError("invalid draft store")
        self.store = store

    @staticmethod
    def _text(value: object, label: str) -> str:
        if type(value) is not str or not value:
            raise ValueError(f"invalid {label}")
        return value

    @classmethod
    def _digest(cls, value: object, label: str) -> str:
        if type(value) is not str or len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError(f"invalid {label}")
        return value

    @classmethod
    def _lines(cls, value: object, selected: tuple[str, ...]) -> tuple[dict[str, str], ...]:
        if type(value) is not tuple or not value:
            raise ValueError("invalid lines")
        lines: list[dict[str, str]] = []
        allocations: set[tuple[str, str]] = set()
        seen_orders: set[str] = set()
        for line in value:
            if type(line) is not dict or not {"order_id", "line_ref", "item_id", "quantity"}.issubset(line) or not set(line).issubset(_LINE_KEYS):
                raise ValueError("invalid line")
            frozen = {key: cls._text(line[key], "line") for key in line}
            if frozen["order_id"] not in selected:
                raise ValueError("invalid line")
            allocation = (frozen["order_id"], frozen["line_ref"])
            if allocation in allocations:
                raise ValueError("duplicate allocation")
            allocations.add(allocation)
            seen_orders.add(frozen["order_id"])
            lines.append(frozen)
        if seen_orders != set(selected):
            raise ValueError("invalid lines")
        return tuple(lines)

    @classmethod
    def _decisions(cls, value: object) -> tuple[dict[str, str], ...]:
        if type(value) is not tuple:
            raise ValueError("invalid decisions")
        decisions: list[dict[str, str]] = []
        ids: set[str] = set()
        for decision in value:
            if type(decision) is not dict or frozenset(decision) not in _DECISION_KEYS:
                raise ValueError("invalid decision")
            clean = {key: cls._text(item, "decision") for key, item in decision.items()}
            if clean["conflict_id"] in ids:
                raise ValueError("duplicate decision")
            ids.add(clean["conflict_id"])
            decisions.append(clean)
        return tuple(decisions)

    @classmethod
    def _annotations(cls, value: object, lines: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
        if type(value) is not tuple:
            raise ValueError("invalid annotations")
        permitted = {(line["order_id"], line["line_ref"]) for line in lines if "source_annotation" in line}
        annotations: list[dict[str, str]] = []
        for annotation in value:
            if type(annotation) is not dict or frozenset(annotation) != {"order_id", "line_ref", "value"}:
                raise ValueError("invalid annotations")
            clean = {key: cls._text(item, "annotation") for key, item in annotation.items()}
            if (clean["order_id"], clean["line_ref"]) not in permitted:
                raise ValueError("invalid annotations")
            annotations.append(clean)
        return tuple(annotations)

    @classmethod
    def _proposal(cls, value: object) -> tuple[str, str, tuple[str, ...], tuple[dict[str, str], ...], tuple[dict[str, str], ...], tuple[dict[str, str], ...], str]:
        if type(value) is not dict or frozenset(value) != _REQUIRED_PROPOSAL_KEYS:
            raise ValueError("invalid proposal")
        if value["ready_to_save"] is not True or value["unresolved"] != ():
            raise ValueError("proposal is not ready")
        if type(value["selected_order_ids"]) is not tuple or not value["selected_order_ids"]:
            raise ValueError("invalid selected orders")
        selected = tuple(cls._text(order_id, "selected order") for order_id in value["selected_order_ids"])
        if len(set(selected)) != len(selected):
            raise ValueError("invalid selected orders")
        customer = cls._text(value["customer_id"], "customer")
        document_family = cls._text(value["document_family"], "document family")
        lines = cls._lines(value["lines"], selected)
        annotations = cls._annotations(value["annotations"], lines)
        decisions = cls._decisions(value["decisions"])
        return customer, document_family, selected, lines, annotations, decisions, cls._digest(value["idempotency_material"], "idempotency key")

    def save(self, proposal: object, accepted_snapshot_sha256: object, actor: object) -> dict[str, object]:
        """Atomically store frozen preview values and provenance without recomputation."""
        customer, document_family, selected, lines, annotations, decisions, idempotency_key = self._proposal(proposal)
        snapshot = self._digest(accepted_snapshot_sha256, "accepted snapshot")
        creator = self._text(actor, "actor")
        saved = self.store.create_converter_draft(
            snapshot,
            creator,
            idempotency_key,
            lines,
            annotations,
            customer,
            document_family,
            selected,
            decisions,
        )
        return {
            "draft_id": saved["draft_id"],
            "accepted_snapshot_sha256": saved["accepted_snapshot_sha256"],
            "created_by": saved["created_by"],
            "created_at": saved["created_at"],
            "status": saved["status"],
            "selected_order_ids": selected,
            "lines": lines,
        }
