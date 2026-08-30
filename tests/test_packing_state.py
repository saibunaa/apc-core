import ast
from decimal import Decimal
from pathlib import Path
from typing import cast
import unittest


class TestPackingStateContract(unittest.TestCase):
    module_path = Path(__file__).parents[1] / "apc_core" / "packing_state.py"

    def test_00_fixture_only_packing_state_module_is_required(self):
        self.assertTrue(self.module_path.is_file(), "apc_core/packing_state.py must exist for the fixture-only Phase B state machine")

    def make_open_plan(self):
        from apc_core.order_invoice_workspace import SourceLineReference
        from apc_core.packing_state import PackingLine, PackingPlan

        provenance = "a" * 64
        first = SourceLineReference("source_order", "ORD/2026/001", "007", provenance)
        second = SourceLineReference("source_order", "ORD/2026/001", "008", provenance)
        return PackingPlan.open(
            "plan-fixture-1",
            provenance,
            (
                PackingLine(first, Decimal("10"), "A1"),
                PackingLine(second, Decimal("4"), "B2"),
            ),
        ), first, second

    def test_split_allocations_reconcile_exactly_and_multi_chapter_box_is_valid(self):
        plan, first, second = self.make_open_plan()
        plan, box = plan.create_box(1, expected_version=0)
        plan = plan.allocate(first, box.number, Decimal("3.25"), expected_version=1)
        plan, other_box = plan.create_box(2, expected_version=2)
        plan = plan.allocate(first, other_box.number, Decimal("1.75"), expected_version=3)
        plan = plan.allocate(second, box.number, Decimal("4"), expected_version=4)

        self.assertEqual(Decimal("5"), plan.line_state(first).allocated)
        self.assertEqual(Decimal("5"), plan.line_state(first).remaining)
        self.assertEqual(Decimal("4"), plan.line_state(second).allocated)
        self.assertEqual(Decimal("0"), plan.line_state(second).remaining)
        self.assertEqual({"A1", "B2"}, {entry.chapter for entry in plan.allocations if entry.box_number == box.number})

    def test_unavailable_quantity_retains_same_line_identity_and_chapter(self):
        plan, first, _ = self.make_open_plan()
        plan = plan.mark_unavailable(first, Decimal("2"), "quality hold", expected_version=0)
        state = plan.line_state(first)

        self.assertEqual(Decimal("2"), state.unavailable)
        self.assertEqual(Decimal("8"), state.remaining)
        self.assertEqual(first, plan.unavailable[0].line.reference)
        self.assertEqual("A1", plan.unavailable[0].chapter)
        self.assertEqual("quality hold", plan.unavailable[0].reason)

    def test_invalid_quantity_unknown_line_cross_plan_box_and_incompatible_provenance_fail_closed(self):
        from apc_core.order_invoice_workspace import SourceLineReference
        from apc_core.packing_state import PackingLine, PackingPlan, PackingStateError

        plan, first, _ = self.make_open_plan()
        plan, box = plan.create_box(1, expected_version=0)
        unknown = SourceLineReference("source_order", "ORD/2026/001", "missing", "a" * 64)
        foreign = PackingPlan.open("other-plan", "a" * 64, (PackingLine(first, Decimal("10"), "A1"),))
        foreign, foreign_box = foreign.create_box(1, expected_version=0)
        incompatible = SourceLineReference("source_order", "ORD/2026/002", "001", "b" * 64)

        for line, target_box, quantity in (
            (first, box, Decimal("0")),
            (first, box, Decimal("-1")),
            (first, box, Decimal("11")),
            (unknown, box, Decimal("1")),
            (first, foreign_box, Decimal("1")),
        ):
            with self.subTest(line=line, box=target_box, quantity=quantity):
                with self.assertRaises(PackingStateError):
                    plan.allocate(line, target_box, quantity, expected_version=1)
        with self.assertRaises(PackingStateError):
            PackingPlan.open("incompatible", "a" * 64, (PackingLine(incompatible, Decimal("1"), ""),))

    def test_reviewer_regression_rejects_forged_and_same_id_cross_plan_boxes(self):
        from apc_core.packing_state import PackingBox, PackingStateError

        plan, first, _ = self.make_open_plan()
        plan, box = plan.create_box(1, expected_version=0)
        forged = PackingBox(plan.plan_id, box.number)
        same_id_plan, _, _ = self.make_open_plan()
        same_id_plan, same_id_box = same_id_plan.create_box(1, expected_version=0)
        self.assertIsNot(same_id_plan, plan)
        for candidate in (forged, same_id_box):
            with self.subTest(candidate=candidate):
                with self.assertRaises(PackingStateError):
                    plan.allocate(first, candidate, Decimal("1"), expected_version=1)

    def test_reviewer_regression_rejects_invalid_direct_plan_construction(self):
        from apc_core.packing_state import PackingPlan, PackingStateError, PlanStatus

        plan, _, _ = self.make_open_plan()
        with self.assertRaises(PackingStateError):
            PackingPlan("forged", "a" * 64, plan.lines, cast(PlanStatus, "OPEN"), 0, (), (), ())

    def test_stale_versions_and_non_open_statuses_reject_mutations(self):
        from apc_core.packing_state import PackingStateError, PlanStatus

        plan, first, _ = self.make_open_plan()
        plan, box = plan.create_box(1, expected_version=0)
        with self.assertRaises(PackingStateError):
            plan.allocate(first, box, Decimal("1"), expected_version=0)
        for status in (PlanStatus.FROZEN, PlanStatus.CLOSED, PlanStatus.VOIDED):
            with self.subTest(status=status):
                terminal = plan.transition(status, expected_version=1)
                with self.assertRaises(PackingStateError):
                    terminal.mark_unavailable(first, Decimal("1"), "reason", expected_version=2)

    def test_duplicate_membership_and_non_decimal_or_nonpositive_source_quantity_fail_closed(self):
        from apc_core.packing_state import PackingLine, PackingPlan, PackingStateError

        plan, first, _ = self.make_open_plan()
        duplicate = (PackingLine(first, Decimal("1"), ""), PackingLine(first, Decimal("1"), ""))
        with self.assertRaises(PackingStateError):
            PackingPlan.open("bad-plan", "a" * 64, duplicate)
        with self.assertRaises(PackingStateError):
            PackingLine(first, Decimal("0"), "")
        with self.assertRaises(PackingStateError):
            PackingLine(first, cast(Decimal, "1"), "")

    def test_phase_b_module_has_no_io_persistence_or_runtime_capability(self):
        tree = ast.parse(self.module_path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertTrue(imported.issubset({"__future__", "dataclasses", "decimal", "enum", "typing"}), imported)
        source = self.module_path.read_text(encoding="utf-8").casefold()
        for forbidden in ("sqlite", "pathlib", "socket", "urllib", "http", "service", "print(", "awb", "invoice", "stock"):
            self.assertNotIn(forbidden, source)
