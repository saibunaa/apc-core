import inspect
import subprocess
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

    def test_workspace_contract_renders_only_the_current_page_and_has_display_only_language_toggle(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        for marker in (
            'id="order-invoice-page-jump"',
            'id="order-invoice-next-page"',
            'id="order-invoice-prev-page"',
            'id="order-invoice-line-window"',
            "api/source-orders/",
            "openSourceOrder",
            "renderLinePage",
            "function render(payload){results.replaceChildren();selected=null;linePage=null;lineWindow.replaceChildren();detail.textContent=''",
            "event.key===' '",
            'event.preventDefault()',
            "toggleLanguage()",
            "currentOffset",
            "next_offset",
        ):
            self.assertIn(marker, html)
        self.assertNotIn('innerHTML', html)
        self.assertNotIn("method:'POST'", html)
        self.assertNotIn("method:'PUT'", html)
        self.assertNotIn("method:'PATCH'", html)
        self.assertNotIn("method:'DELETE'", html)

    def test_default_workspace_excludes_fixture_packing_drawer(self):
        from apc_core.order_invoice_ui import order_invoice_html

        self.assertNotIn('id="open-packing-drawer"', order_invoice_html())
        self.assertIn('id="open-packing-drawer"', order_invoice_html(include_fixture_drawer=True))

    def test_selecting_a_new_browse_result_clears_the_prior_line_page_before_selection(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn("function clearLinePage()", html)
        selection_start = html.index("button.addEventListener('click'", html.index('function render(payload)'))
        selection_end = html.index('});row.append(button)', selection_start)
        selection = html[selection_start:selection_end]
        self.assertLess(selection.index('clearLinePage()'), selection.index('selected=record'))

    def test_clearing_a_prior_line_page_resets_the_line_pager_before_a_new_selection(self):
        source_path = Path(__file__).parents[1] / "apc_core" / "order_invoice_ui.py"
        harness = r'''
const fs=require('fs');
const source=fs.readFileSync(process.argv[1], 'utf8');
const match=source.match(/function clearLinePage\(\)\{([^}]*)\}/);
if(!match) throw new Error('clearLinePage missing');
let linePage={offset:250};
let currentOffset=250;
const lineWindow={cleared:0,replaceChildren(){this.cleared+=1}};
const detail={textContent:'Opened prior lines'};
const pageJump={value:'2'};
const previousPage={disabled:false};
const nextPage={disabled:false};
const clearLinePage=eval('()=>{'+match[1]+'}');
clearLinePage();
if(linePage!==null || currentOffset!==0 || pageJump.value!=='1' || !previousPage.disabled || !nextPage.disabled || lineWindow.cleared!==1 || detail.textContent!=='') throw new Error('stale line pager state remained');
'''
        subprocess.run(["node", "-e", harness, str(source_path)], check=True, capture_output=True, text=True)

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
