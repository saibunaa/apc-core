import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "apc_core" / "invoice_draft_ui.py"


class TestInvoiceDraftUi(unittest.TestCase):
    """INV-2B staff UI is a safe handoff to the existing opaque-preview API."""

    def test_00_invoice_draft_ui_module_is_required(self):
        self.assertTrue(MODULE_PATH.is_file(), "INV-2B requires an isolated invoice draft UI module")

    def test_safe_keyboard_first_review_ui_uses_only_opaque_preview_save(self):
        from apc_core.invoice_draft_ui import invoice_draft_html

        html = invoice_draft_html()
        for marker in (
            'id="opened-order"',
            'id="start-draft"',
            'id="add-to-draft"',
            'id="draft-review"',
            'id="review-confirmation"',
            'id="save-draft"',
            'id="active-actor"',
            'not used for automatic linking',
            'document.createElement',
            '.textContent=',
            'manual_value',
            'rationale',
            'preview_ref',
        ):
            self.assertIn(marker, html)
        self.assertIn("request('api/previews'", html)
        self.assertIn("request('api/drafts'", html)
        self.assertIn("JSON.stringify({preview_ref:previewRef,actor})", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("New Invoice", html)
        self.assertNotIn("Issue", html)
        self.assertNotIn("Print", html)
        self.assertNotIn("Export", html)
        self.assertNotIn("AWB Save", html)
        self.assertNotIn("/api/issue", html)
        self.assertNotIn("/api/print", html)
        self.assertNotIn("/api/export", html)
        self.assertNotIn("/api/sync", html)
        self.assertNotIn("/api/awb", html)

    def test_order_handoff_is_only_available_after_an_opened_order(self):
        from apc_core.order_explorer import invoice_draft_handoff_html

        script = invoice_draft_handoff_html()
        self.assertIn("openedOrderId", script)
        self.assertIn("start-invoice-draft", script)
        self.assertIn("disabled", script)
        self.assertIn("sessionStorage.setItem('apc-core-invoice-handoff',openedOrderId)", script)
        self.assertIn("location.assign('../drafts/')", script)
        self.assertNotIn("location.assign('../invoices/')", script)
        import apc_core.item_explorer as module
        self.assertIn("start-invoice-draft", module._order_explorer_html(invoice_available=True))
        self.assertIn("apc-core-opened-order", module._order_explorer_html(invoice_available=True))

    def test_handoff_needs_the_opened_order_screen_and_order_list_never_defaults_a_candidate(self):
        from apc_core.invoice_draft_ui import invoice_draft_html
        from apc_core.order_explorer import invoice_draft_handoff_html
        import apc_core.item_explorer as module

        html = invoice_draft_html()
        handoff = invoice_draft_handoff_html()
        order_html = module._order_explorer_html(invoice_available=True)
        self.assertIn("sessionStorage", handoff)
        self.assertIn("apc-core-invoice-handoff", handoff)
        self.assertIn("sessionStorage", html)
        self.assertNotIn("new URLSearchParams(location.search)", html)
        self.assertNotIn("if(rows.length)choose(rows[0],data.orders[0])", order_html)


if __name__ == "__main__":
    unittest.main()
