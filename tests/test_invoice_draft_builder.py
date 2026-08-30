import ast
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "apc_core" / "invoice_draft_builder.py"


def source_provenance():
    return {"accepted_snapshot_sha256": "a" * 64, "source_revision": "snapshot-17"}


def order(order_id, customer_id="CUST-1", family="commercial", lines=None, **extra):
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "document_family": family,
        "lines": lines
        or [
            {
                "line_ref": f"{order_id}:1",
                "item_id": f"ITEM-{order_id}",
                "quantity": "2.00",
                "unit_price": "10.50",
                "source_annotation": "original label",
            }
        ],
        **extra,
    }


def build(*args, **kwargs):
    from apc_core.invoice_draft_builder import build_invoice_draft

    return build_invoice_draft(*args, **kwargs)


class InvoiceDraftBuilderTests(unittest.TestCase):
    def test_inv_1c_module_exists_before_behavior_contracts(self):
        self.assertTrue(MODULE_PATH.is_file())

    def test_single_order_preview_freezes_selected_source_values_and_annotations(self):
        orders = [order("ORD-2"), order("ORD-1")]
        preview = build(source_provenance(), orders, ["ORD-1"], [])

        self.assertEqual(preview["selected_order_ids"], ("ORD-1",))
        self.assertEqual(preview["customer_id"], "CUST-1")
        self.assertEqual(
            preview["lines"],
            (
                {
                    "order_id": "ORD-1",
                    "line_ref": "ORD-1:1",
                    "item_id": "ITEM-ORD-1",
                    "quantity": "2.00",
                    "unit_price": "10.50",
                    "source_annotation": "original label",
                },
            ),
        )
        self.assertEqual(preview["annotations"], ({"order_id": "ORD-1", "line_ref": "ORD-1:1", "value": "original label"},))
        self.assertTrue(preview["ready_to_save"])
        self.assertEqual(preview["unresolved"], ())

    def test_multi_order_preview_preserves_supplied_order_and_line_order(self):
        orders = [
            order("ORD-B", lines=[{"line_ref": "1", "item_id": "B2", "quantity": "1"}, {"line_ref": "2", "item_id": "B1", "quantity": "3"}]),
            order("ORD-A", lines=[{"line_ref": "1", "item_id": "A1", "quantity": "4"}]),
        ]
        preview = build(source_provenance(), orders, ["ORD-A", "ORD-B"], [])

        self.assertEqual(preview["selected_order_ids"], ("ORD-A", "ORD-B"))
        self.assertEqual([(line["order_id"], line["line_ref"]) for line in preview["lines"]], [("ORD-B", "1"), ("ORD-B", "2"), ("ORD-A", "1")])

    def test_source_provenance_requires_64_character_ascii_hex_snapshot_digest(self):
        for snapshot_digest in ("g" * 64, "a" * 63, "a" * 65):
            with self.subTest(snapshot_digest=snapshot_digest), self.assertRaisesRegex(ValueError, "source provenance"):
                build({"accepted_snapshot_sha256": snapshot_digest}, [order("ORD-1")], ["ORD-1"], [])

    def test_empty_selection_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "explicit selection"):
            build(source_provenance(), [order("ORD-1")], [], [])

    def test_unknown_selection_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unknown selected order"):
            build(source_provenance(), [order("ORD-1")], ["MISSING"], [])

    def test_duplicate_selection_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "duplicate selected order"):
            build(source_provenance(), [order("ORD-1")], ["ORD-1", "ORD-1"], [])

    def test_empty_selected_order_fails_closed(self):
        empty = {"order_id": "ORD-1", "customer_id": "CUST-1", "document_family": "commercial", "lines": []}
        with self.assertRaisesRegex(ValueError, "empty selected order"):
            build(source_provenance(), [empty], ["ORD-1"], [])

    def test_rejects_mixed_customers_document_families_and_duplicate_line_refs(self):
        with self.assertRaisesRegex(ValueError, "mixed customers"):
            build(source_provenance(), [order("ORD-1"), order("ORD-2", customer_id="CUST-2")], ["ORD-1", "ORD-2"], [])
        with self.assertRaisesRegex(ValueError, "document-family"):
            build(source_provenance(), [order("ORD-1"), order("ORD-2", family="proforma")], ["ORD-1", "ORD-2"], [])
        with self.assertRaisesRegex(ValueError, "duplicate selected line ref"):
            build(source_provenance(), [order("ORD-1", lines=[{"line_ref": "same", "item_id": "A", "quantity": "1"}, {"line_ref": "same", "item_id": "B", "quantity": "1"}])], ["ORD-1"], [])

    def test_required_conflicts_need_explicit_complete_decisions_and_manual_rationale(self):
        conflicted = order("ORD-1", shipment_conflicts=[{"conflict_id": "ship-to", "required": True, "existing_values": [{"value": "BKK", "source": "ORD-1"}]}])
        preview = build(source_provenance(), [conflicted], ["ORD-1"], [])
        self.assertFalse(preview["ready_to_save"])
        self.assertEqual(preview["unresolved"], ({"conflict_id": "ship-to", "reason": "required shipment conflict unresolved"},))

        with self.assertRaisesRegex(ValueError, "rationale"):
            build(source_provenance(), [conflicted], ["ORD-1"], [{"conflict_id": "ship-to", "manual_value": "CNX"}])

        resolved = build(source_provenance(), [conflicted], ["ORD-1"], [{"conflict_id": "ship-to", "chosen_existing_value": "BKK", "chosen_existing_source": "ORD-1"}])
        self.assertTrue(resolved["ready_to_save"])
        self.assertEqual(resolved["decisions"], ({"conflict_id": "ship-to", "chosen_existing_value": "BKK", "chosen_existing_source": "ORD-1"},))

    def test_unknown_price_evidence_is_explicitly_unresolved_not_silently_priced(self):
        proposal = build(
            {"accepted_snapshot_sha256": "a" * 64},
            [{"order_id": "O-1", "customer_id": "C-1", "document_family": "legacy-order", "lines": [{"line_ref": "1", "item_id": "I-1", "quantity": "1", "source_unit_price": "", "current_price": {"status": "UNKNOWN", "value": ""}}], "shipment_conflicts": []}],
            ["O-1"], [],
        )
        self.assertFalse(proposal["ready_to_save"])
        self.assertIn("source/current price unresolved", {entry["reason"] for entry in proposal["unresolved"]})

    def test_unsupported_pricing_is_unresolved_without_inventing_financial_values(self):
        preview = build(source_provenance(), [order("ORD-1", pricing_rule="tiered-discount")], ["ORD-1"], [])
        self.assertFalse(preview["ready_to_save"])
        self.assertEqual(preview["unresolved"], ({"reason": "unsupported pricing rule", "order_id": "ORD-1", "pricing_rule": "tiered-discount"},))
        self.assertEqual(preview["lines"][0]["unit_price"], "10.50")
        self.assertNotIn("tax", preview)
        self.assertNotIn("discount", preview)
        self.assertNotIn("invoice_number", preview)

    def test_idempotency_is_deterministic_and_independent_of_awb_changes(self):
        first = order("ORD-1", awb="AWB-OLD")
        second = order("ORD-1", awb="AWB-NEW")
        preview_a = build(source_provenance(), [first], ["ORD-1"], [])
        preview_b = build(source_provenance(), [second], ["ORD-1"], [])

        self.assertEqual(preview_a["idempotency_material"], preview_b["idempotency_material"])
        self.assertEqual(preview_a, build(source_provenance(), [first], ["ORD-1"], []))

    def test_static_pure_capability_absence_and_no_mutation_of_input_dtos(self):
        original = order("ORD-1")
        build(source_provenance(), [original], ["ORD-1"], [])
        self.assertEqual(original["lines"][0]["source_annotation"], "original label")

        tree = ast.parse(MODULE_PATH.read_text())
        imported_roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            for alias in (node.names if isinstance(node, ast.Import) else [node])
            if isinstance(node, (ast.Import, ast.ImportFrom))
        }
        self.assertFalse(imported_roots & {"sqlite3", "socket", "requests", "urllib", "http", "pathlib", "os", "time", "datetime", "subprocess"})
        source = MODULE_PATH.read_text().lower()
        self.assertNotIn("awb", source)
        self.assertNotIn("open(", source)
        self.assertNotIn("connect(", source)


if __name__ == "__main__":
    unittest.main()
