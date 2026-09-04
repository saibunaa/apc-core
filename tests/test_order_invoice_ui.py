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
            'Legacy source · Read only',
            'SOURCE ORDER · READ-ONLY',
            "single_slash:'Real Invoice · Legacy source · Read only'",
            "repeated_slash:'Temporary / Proforma · Legacy source · Read only'",
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
        self.assertIn("button.setAttribute('aria-pressed','true')", html)
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
            "api/source-invoices/",
            "openSourceOrder",
            "openSourceInvoice",
            "openLinePage",
            "renderLinePage",
            "function clearLinePage()",
            'id="order-invoice-language-toggle"',
            "languageToggle.addEventListener('click',toggleLanguage)",
            "browseOffset",
            "lineOffset",
            "next_offset",
        ):
            self.assertIn(marker, html)
        self.assertNotIn('innerHTML', html)
        self.assertNotIn("method:'POST'", html)
        self.assertNotIn("method:'PUT'", html)
        self.assertNotIn("method:'PATCH'", html)
        self.assertNotIn("method:'DELETE'", html)

    def test_opening_the_workspace_browses_the_latest_seven_calendar_days_without_search(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn('id="order-invoice-date-from"', html)
        self.assertIn('id="order-invoice-date-to"', html)
        self.assertIn("function recentCalendarWindow()", html)
        self.assertIn("browse(0)", html[html.index("function open()"):html.index("function textFor")])
        self.assertIn("date_from", html)
        self.assertIn("date_to", html)

    def test_source_invoice_family_labels_are_staff_identity_gated_display_only_copy(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn("single_slash:'Real Invoice · Legacy source · Read only'", html)
        self.assertIn("repeated_slash:'Temporary / Proforma · Legacy source · Read only'", html)
        self.assertIn("X-APC-Core-Staff", html)
        self.assertIn("window.apcCoreActiveStaff", html)
        self.assertIn('slash_family', html)
        self.assertNotIn('Legacy Invoices · Read-only', html)
        for forbidden in ('Save invoice', 'Issue invoice', 'Print invoice', 'Export invoice', 'AWB link'):
            self.assertNotIn(forbidden, html)

    def test_default_workspace_hides_core_drafts_until_explicitly_enabled(self):
        from apc_core.order_invoice_ui import order_invoice_html

        self.assertNotIn('Core Drafts', order_invoice_html())
        self.assertNotIn('local draft review', order_invoice_html())
        self.assertIn('Core Drafts', order_invoice_html(include_core_drafts=True))

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

    def test_clearing_a_prior_line_page_resets_only_line_state_before_a_new_selection(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn("function clearLinePage(){linePage=null;lineOffset=0;lineWindow.replaceChildren();detail.textContent='';languageToggle.disabled=true}", html)
        self.assertIn('browseOffset=0;browseHasNext=false;clearLinePage()', html)

    def test_workspace_uses_accessible_tabs_safe_arrow_handling_and_visible_language_toggle(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn('id="order-invoice-tab-source-order"', html)
        self.assertIn('aria-controls="order-invoice-tabpanel"', html)
        self.assertIn('id="order-invoice-tabpanel"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn('id="order-invoice-language-toggle"', html)
        self.assertIn('aria-pressed="false"', html)
        self.assertIn("event.target.getAttribute('role')==='tab'", html)

    def test_workspace_uses_pressed_result_state_compact_announcements_and_distinct_pagers(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        self.assertIn("button.setAttribute('aria-pressed','false')", html)
        self.assertIn("button.setAttribute('aria-pressed','true')", html)
        self.assertNotIn('id="order-invoice-line-window" class="detail" aria-live="polite"', html)
        self.assertIn('id="order-invoice-status" class="meta" aria-live="polite"', html)
        self.assertIn('browseOffset', html)
        self.assertIn('lineOffset', html)
        self.assertIn('browseHasNext', html)
        self.assertIn('nextPage.disabled=lineMode?linePage.next_offset===null:!browseHasNext', html)
        self.assertNotIn('currentOffset', html)

    def test_handler_serves_shared_workspace_without_adding_a_mutation_route(self):
        from apc_core.item_explorer import make_handler

        source = inspect.getsource(make_handler)
        self.assertIn('parsed.path == "/order-invoice/"', source)
        self.assertIn('_order_invoice_html(include_core_drafts=invoice_draft_service is not None)', source)
        for method_name in ('do_POST', 'do_PUT', 'do_PATCH', 'do_DELETE'):
            handler_class = make_handler(object(), {})
            self.assertNotIn('/order-invoice/', inspect.getsource(getattr(handler_class, method_name)))

    def test_mobile_header_leaves_shared_identity_and_main_navigation_in_the_shared_fixed_shell(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        for marker in (
            'id="order-invoice-mobile-header"',
            'class="order-invoice-mobile-header"',
            'data-apc-mobile-identity-header="true"',
            '@media(max-width:768px){.shell{padding:16px}.order-invoice-mobile-header{position:sticky;top:0;',
        ):
            self.assertIn(marker, html)
        self.assertNotIn("identityChange=document.getElementById('identity-change-user')", html)
        self.assertNotIn('mobileHeader.append(identityChange)', html)
        self.assertNotIn('#order-invoice-mobile-header .identity-action,#order-invoice-mobile-header .back{position:static;', html)

    def test_mobile_header_reserves_space_and_preserves_visible_keyboard_focus(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html()
        for marker in (
            '.order-invoice-mobile-header{margin-bottom:12px;',
            '#order-invoice-workspace .card{scroll-margin-top:76px}',
            '.order-invoice-mobile-header .back:focus-visible,.order-invoice-mobile-header .identity-action:focus-visible{outline:3px solid var(--accent);',
            '.modal{position:fixed;inset:0;z-index:20;',
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
