"""Fixture-only pure packing-plan value transitions.

This is not a same-process provenance or authorization boundary. It performs no
I/O and accepts only validated primitive/value inputs for Phase B fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class PackingStateError(ValueError):
    pass


class PlanStatus(str, Enum):
    OPEN = "OPEN"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"
    VOIDED = "VOIDED"


@dataclass(frozen=True, slots=True)
class PackingLine:
    reference: object
    quantity: Decimal
    chapter: str

    def __post_init__(self) -> None:
        if type(self.quantity) is not Decimal or not self.quantity.is_finite() or self.quantity <= 0:
            raise PackingStateError("invalid source quantity")
        if type(self.chapter) is not str or _reference_key(self.reference) is None:
            raise PackingStateError("invalid source line")


@dataclass(frozen=True, slots=True)
class PackingBox:
    plan_id: str
    number: int


@dataclass(frozen=True, slots=True)
class Allocation:
    line: PackingLine
    box_number: int
    quantity: Decimal

    @property
    def chapter(self) -> str:
        return self.line.chapter


@dataclass(frozen=True, slots=True)
class Unavailable:
    line: PackingLine
    quantity: Decimal
    reason: str

    @property
    def chapter(self) -> str:
        return self.line.chapter


@dataclass(frozen=True, slots=True)
class LineState:
    allocated: Decimal
    unavailable: Decimal
    remaining: Decimal


@dataclass(frozen=True, slots=True)
class PackingPlan:
    plan_id: str
    provenance: str
    lines: tuple[PackingLine, ...]
    status: PlanStatus = PlanStatus.OPEN
    version: int = 0
    boxes: tuple[PackingBox, ...] = ()
    allocations: tuple[Allocation, ...] = ()
    unavailable: tuple[Unavailable, ...] = ()

    def __post_init__(self) -> None:
        if type(self.plan_id) is not str or not self.plan_id or type(self.provenance) is not str or not self.provenance:
            raise PackingStateError("invalid plan identity")
        if type(self.lines) is not tuple or not self.lines or any(type(line) is not PackingLine for line in self.lines):
            raise PackingStateError("invalid plan membership")
        keys = tuple(_reference_key(line.reference) for line in self.lines)
        if len(set(keys)) != len(keys) or any(getattr(line.reference, "source_sha256", None) != self.provenance for line in self.lines):
            raise PackingStateError("invalid plan provenance")
        if type(self.status) is not PlanStatus or type(self.version) is not int or self.version < 0:
            raise PackingStateError("invalid plan state")
        if type(self.boxes) is not tuple or any(type(box) is not PackingBox or box.plan_id != self.plan_id or type(box.number) is not int or box.number <= 0 for box in self.boxes):
            raise PackingStateError("invalid boxes")
        if len({box.number for box in self.boxes}) != len(self.boxes):
            raise PackingStateError("duplicate box")

    @classmethod
    def open(cls, plan_id: object, provenance: object, lines: object) -> "PackingPlan":
        return cls(plan_id, provenance, lines)  # type: ignore[arg-type]

    def create_box(self, number: object, *, expected_version: object) -> tuple["PackingPlan", PackingBox]:
        self._authorize(expected_version)
        if type(number) is not int or number <= 0 or any(box.number == number for box in self.boxes):
            raise PackingStateError("invalid box")
        box = PackingBox(self.plan_id, number)
        return self._next(boxes=(*self.boxes, box)), box

    def allocate(self, reference: object, box_number: object, quantity: object, *, expected_version: object) -> "PackingPlan":
        self._authorize(expected_version)
        line = self._member(reference)
        if type(box_number) is not int or not any(box.number == box_number for box in self.boxes):
            raise PackingStateError("cross-plan or unknown box")
        amount = _positive_quantity(quantity)
        if amount > self.line_state(reference).remaining:
            raise PackingStateError("allocation exceeds remaining quantity")
        return self._next(allocations=(*self.allocations, Allocation(line, box_number, amount)))

    def mark_unavailable(self, reference: object, quantity: object, reason: object, *, expected_version: object) -> "PackingPlan":
        self._authorize(expected_version)
        line = self._member(reference)
        amount = _positive_quantity(quantity)
        if type(reason) is not str or not reason or amount > self.line_state(reference).remaining:
            raise PackingStateError("invalid unavailable mutation")
        return self._next(unavailable=(*self.unavailable, Unavailable(line, amount, reason)))

    def transition(self, status: object, *, expected_version: object) -> "PackingPlan":
        self._authorize(expected_version)
        if type(status) is not PlanStatus or status is PlanStatus.OPEN:
            raise PackingStateError("invalid plan status")
        return self._next(status=status)

    def line_state(self, reference: object) -> LineState:
        line = self._member(reference)
        allocated = sum((row.quantity for row in self.allocations if row.line == line), Decimal("0"))
        unavailable = sum((row.quantity for row in self.unavailable if row.line == line), Decimal("0"))
        return LineState(allocated, unavailable, line.quantity - allocated - unavailable)

    def _authorize(self, expected_version: object) -> None:
        if self.status is not PlanStatus.OPEN or type(expected_version) is not int or expected_version != self.version:
            raise PackingStateError("stale or non-open plan")

    def _member(self, reference: object) -> PackingLine:
        key = _reference_key(reference)
        for line in self.lines:
            if _reference_key(line.reference) == key:
                return line
        raise PackingStateError("unknown source line")

    def _next(self, **changes: object) -> "PackingPlan":
        values = {"plan_id": self.plan_id, "provenance": self.provenance, "lines": self.lines, "status": self.status, "version": self.version + 1, "boxes": self.boxes, "allocations": self.allocations, "unavailable": self.unavailable}
        values.update(changes)
        return PackingPlan(**values)


def _reference_key(reference: object) -> tuple[str, str, str, str] | None:
    values = tuple(getattr(reference, name, None) for name in ("source_type", "document_id", "line_id", "source_sha256"))
    if any(type(value) is not str or not value for value in values):
        return None
    return tuple(str(value) for value in values)  # type: ignore[return-value]


def _positive_quantity(value: object) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise PackingStateError("invalid quantity")
    return value
