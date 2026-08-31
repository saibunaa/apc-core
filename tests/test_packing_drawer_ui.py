import unittest
from pathlib import Path


class TestPackingDrawerUi(unittest.TestCase):
    def test_phase_d_packing_drawer_module_exists(self):
        source_path = Path(__file__).parents[1] / "apc_core" / "packing_drawer_ui.py"
        self.assertTrue(source_path.exists(), "Phase D requires a static packing drawer renderer")

    def test_fixture_drawer_renders_reconciliation_evidence_and_safe_copy(self):
        from apc_core.packing_drawer_ui import packing_drawer_html

        html = packing_drawer_html()
        for marker in (
            'id="packing-drawer"',
            'id="open-packing-drawer"',
            'id="close-packing-drawer"',
            'role="dialog"',
            'aria-modal="true"',
            'data-plan-provenance=',
            'OPEN · fixture plan',
            'Source line identity',
            'Captured source order',
            'Allocated',
            'Unavailable',
            'Remaining',
            'Correction history',
            'SubCust A1',
            'ORD//2026/001',
            'line 0007',
            'id="packing-box-target"',
            'id="packing-quantity"',
            'id="packing-validation"',
            'Review allocation',
            "event.key===' '",
            'event.preventDefault()',
            'document.activeElement',
            'focus()',
        ):
            self.assertIn(marker, html)
        for forbidden in (
            'fetch(',
            'XMLHttpRequest',
            'innerHTML',
            'delete',
            'reprice',
            'reallocate',
            'Invoice issued',
            'Stock changed',
            'Print packing',
            'Physical completion',
        ):
            self.assertNotIn(forbidden, html)

    def test_fixture_drawer_has_closed_keyboard_and_validation_transition_contract(self):
        from apc_core.packing_drawer_ui import packing_drawer_html

        html = packing_drawer_html()
        for marker in (
            "lineList.addEventListener('keydown'",
            "event.key===' '",
            "document.body.dataset.scrollLocked='true'",
            "boxTarget.addEventListener('change'",
            "reviewButton.addEventListener('click'",
            "validation.textContent='Enter a positive quantity.'",
            "quantity.focus()",
            "validation.textContent='Select a target box.'",
            "boxTarget.focus()",
            "validation.textContent='Ready for fixture review only; no allocation was recorded.'",
        ):
            self.assertIn(marker, html)

    def test_shared_order_invoice_workspace_embeds_the_fixture_drawer_without_new_routes(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html(include_fixture_drawer=True)
        self.assertIn('id="open-packing-drawer"', html)
        self.assertIn('id="packing-drawer"', html)
        self.assertIn('fixture-provenance-2026-08-30', html)
        self.assertNotIn('fetch(', html[html.index('id="open-packing-drawer"'):])


if __name__ == "__main__":
    unittest.main()
