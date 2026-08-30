import inspect
import unittest
from pathlib import Path


class TestOrderInvoiceWorkspaceUi(unittest.TestCase):
    def test_workspace_shell_is_a_safe_keyboard_first_read_only_browser(self):
        source_path = Path(__file__).parents[1] / "apc_core" / "order_invoice_ui.py"
        self.assertTrue(source_path.exists(), "Task 4 workspace module must exist")

        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        for marker in (
            'id="order-invoice-workspace"',
            'id="open-order-invoice"',
            'id="order-invoice-modal"',
            'role="dialog"',
            'aria-modal="true"',
            'Source Orders',
            'Source Invoices',
            'Core Drafts',
            'SOURCE ORDER · READ-ONLY',
            'SOURCE INVOICE · READ-ONLY',
            'CORE DRAFT · LOCAL',
            'api/browse?type=',
            'textContent=',
            'replaceChildren()',
            "event.key==='Escape'",
            "event.key==='Tab'",
            'invoker.focus()',
            "event.key==='ArrowRight'",
            "event.key==='ArrowLeft'",
            "event.key==='Enter'",
            '--cream:#eadbc8',
            '--accent:#1d6b57',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('innerHTML', html)
        self.assertNotIn('method:', html)
        self.assertNotIn("fetch('/order-invoice/api/browse", html)
        self.assertIn("fetch('api/browse?type='", html)
        self.assertIn("event.target.click();openSelected()", html)
        for forbidden in ('Save', 'Issue', 'Print', 'Export', 'AWB Save', 'legacy-write'):
            self.assertNotIn(forbidden, html)

    def test_handler_serves_shared_workspace_without_adding_a_mutation_route(self):
        from apc_core.item_explorer import make_handler

        source = inspect.getsource(make_handler)
        self.assertIn('parsed.path == "/order-invoice/"', source)
        self.assertIn('_order_invoice_html()', source)
        for method_name in ('do_POST', 'do_PUT', 'do_PATCH', 'do_DELETE'):
            handler_class = make_handler(object(), {})
            self.assertNotIn('/order-invoice/', inspect.getsource(getattr(handler_class, method_name)))


if __name__ == "__main__":
    unittest.main()
