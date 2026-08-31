import subprocess
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

    def test_fixture_review_denies_quantity_above_selected_remaining_and_keeps_original_quantity_visible(self):
        from apc_core.packing_drawer_ui import packing_drawer_html

        html = packing_drawer_html()
        self.assertIn('data-remaining="4"', html)
        self.assertIn('data-original-quantity="10"', html)
        self.assertIn('Original quantity', html)
        self.assertIn("validation.textContent='Quantity exceeds remaining fixture quantity. Original quantity: '+selected.dataset.originalQuantity+' · Remaining: '+selected.dataset.remaining+'.'", html)

    def test_fixture_review_behavior_rejects_over_remaining_without_recording_an_allocation(self):
        source_path = Path(__file__).parents[1] / "apc_core" / "packing_drawer_ui.py"
        harness = r'''
const fs=require('fs');
const source=fs.readFileSync(process.argv[1], 'utf8');
const match=source.match(/reviewButton\.addEventListener\('click',\(\)=>\{([\s\S]*?)\}\);document\.addEventListener/);
if(!match) throw new Error('fixture review handler missing');
let selected={dataset:{remaining:'4',originalQuantity:'10'}};
let quantityFocused=0;
const quantity={value:'5',focus(){quantityFocused+=1}};
const boxTarget={value:'1',focus(){throw new Error('must not focus when box is selected')}};
const validation={textContent:''};
const review=eval('()=>{'+match[1]+'}');
review();
if(validation.textContent!=='Quantity exceeds remaining fixture quantity. Original quantity: 10 · Remaining: 4.') throw new Error(validation.textContent);
if(quantityFocused!==1) throw new Error('over-remaining quantity must return focus to the quantity field');
'''
        subprocess.run(["node", "-e", harness, str(source_path)], check=True, capture_output=True, text=True)

    def test_fixture_drawer_uses_native_buttons_and_traps_modal_focus_with_global_escape(self):
        from apc_core.packing_drawer_ui import packing_drawer_html

        html = packing_drawer_html()
        self.assertNotIn('role="listbox"', html)
        self.assertNotIn('role="option"', html)
        self.assertIn('function focusable()', html)
        self.assertIn('function trap(event)', html)
        self.assertIn("document.addEventListener('keydown'", html)
        self.assertIn("event.key==='Escape'&&!drawer.hidden", html)

    def test_shared_order_invoice_workspace_embeds_the_fixture_drawer_without_new_routes(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html(include_fixture_drawer=True)
        self.assertIn('id="open-packing-drawer"', html)
        self.assertIn('id="packing-drawer"', html)
        self.assertIn('fixture-provenance-2026-08-30', html)
        self.assertNotIn('fetch(', html[html.index('id="open-packing-drawer"'):])


if __name__ == "__main__":
    unittest.main()
