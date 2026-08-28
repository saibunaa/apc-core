import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from apc_core.item_explorer import ItemExplorer, _customer_explorer_html, _item_explorer_html, _menu_html, _menu_html_body, make_handler


def _snapshot(root: Path) -> Path:
    source = root / "items.sqlite"
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
    connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-001")')
    connection.commit()
    connection.close()
    return source


class ProgramShellTests(unittest.TestCase):
    def test_all_program_surfaces_use_the_one_shared_active_staff_tile_shell(self):
        pages = (_menu_html(), _item_explorer_html(), _customer_explorer_html())
        for html in pages:
            for marker in (
                'id="identity-confirm"',
                'id="identity-picker"',
                'role="radiogroup"',
                'tile.setAttribute("role","radio")',
                'data-identity-username',
                'identity-tile',
                'id="identity-change-user"',
                'Change',
                'attribution only',
                'not security, authentication, or authorization',
                'fetch("api/staff")',
                'window.apcCoreActiveStaff',
            ):
                self.assertIn(marker, html)
            self.assertIn('const key="apc-core-identity"', html)
            self.assertIn('localStorage.setItem(key,value)', html)
            self.assertNotIn('id="identity-user"', html)
            self.assertNotIn('id="confirm-user"', html)
            self.assertNotIn('href="/items/"', html)
            self.assertNotIn('href="/customers/"', html)

        self.assertTrue(all(html.count('const key="apc-core-identity"') == 1 for html in pages))
        self.assertTrue(all(html.count('id="identity-change-user"') == 1 for html in pages))
        self.assertTrue(all('html.apc-core-known-user #identity-picker{display:none}' in html for html in pages))
        self.assertTrue(all('html.apc-core-known-user #identity-confirm{display:none}' not in html for html in pages))
        self.assertTrue(all('localStorage.getItem("apc-core-identity")' in html for html in pages))

        import inspect
        handler_source = inspect.getsource(make_handler)
        self.assertIn('body = _staff_identity_shell(customer_price_module.html()).encode("utf-8")', handler_source)

    def test_identity_picker_uses_large_colored_rounded_square_staff_tiles_without_changing_attribution_contract(self):
        for html in (_menu_html(), _item_explorer_html(), _customer_explorer_html()):
            for marker in (
                "Who’s using APC Program?",
                ".identity-picker-screen",
                "grid-template-columns:repeat(auto-fit,minmax(150px,1fr))",
                "aspect-ratio:1",
                ".identity-tile:nth-child(4n+1)",
                "box-shadow:6px 6px 0",
                "border-radius:22px",
                "@media(max-width:620px)",
            ):
                self.assertIn(marker, html)
            self.assertIn("Activity attribution only", html)
            self.assertIn("not security, authentication, or authorization", html)

    def test_identity_tiles_activate_only_on_click_or_enter_space_and_roam_with_arrows(self):
        html = _item_explorer_html()
        for marker in (
            'tile.addEventListener("click",()=>apply(person.username))',
            'if(event.key==="Enter"||event.key===" "){event.preventDefault();apply(person.username);return}',
            'if(event.key==="ArrowRight"||event.key==="ArrowDown")',
            'if(event.key==="ArrowLeft"||event.key==="ArrowUp")',
            'if(event.key==="Home")',
            'if(event.key==="End")',
            'tile.tabIndex=index===activeIndex?0:-1',
        ):
            self.assertIn(marker, html)
        self.assertIn('tile.setAttribute("aria-checked","false")', html)
        self.assertIn('tile.setAttribute("aria-checked",String(tile.dataset.identityUsername===value))', html)
        self.assertNotIn('tile.addEventListener("focus",()=>apply(', html)
        self.assertNotIn('tile.addEventListener("mouseenter",()=>apply(', html)

    def test_remembered_user_hides_only_the_picker_and_change_chip_remains_reachable(self):
        for html in (_menu_html(), _item_explorer_html(), _customer_explorer_html()):
            self.assertIn('html.apc-core-known-user #identity-picker{display:none}', html)
            self.assertIn('if(window.apcCoreKnownUser)document.getElementById("identity-change-user").hidden=false', html)
            self.assertIn('id="identity-change-user"', html)
            self.assertIn('change.hidden=!value', html)
            self.assertIn('function choose(){apply("");picker.hidden=false;content.hidden=true;', html)
            self.assertIn('html.apc-core-known-user #identity-confirm{background:transparent;pointer-events:none}', html)
            self.assertIn('.identity-action{pointer-events:auto;position:fixed;', html)
            self.assertNotIn('html.apc-core-known-user #identity-confirm{display:none}', html)
            self.assertIn('window.dispatchEvent(new CustomEvent("apc-core-identity",{detail:value||""}))', html)

    def test_customer_workspace_keeps_profile_visible_and_does_not_force_page_scroll(self):
        html = _customer_explorer_html()
        for marker in (".profile{position:sticky", ".back{position:sticky", 'class="back" href="../"', "@media(max-width:760px){.shell{padding:12px}.workspace{grid-template-columns:1fr}.profile{position:static;align-self:auto;border:0;border-top:1px solid var(--line)}", "← APC Core"):
            self.assertIn(marker, html)
        self.assertNotIn("scrollIntoView", html)
        self.assertNotIn("window.scrollTo", html)

    def test_program_prefix_is_a_first_class_route_with_relative_module_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(_snapshot(root), data_dir=root / "state")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                port = int(server.server_address[1])
                for path in ("/program/", "/program/items/"):
                    connection = HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    html = response.read().decode("utf-8")
                    connection.close()
                    self.assertEqual(response.status, 200)
                    self.assertNotIn('href="/items/"', html)
                    self.assertNotIn('href="/customers/"', html)
            finally:
                server.shutdown()
                server.server_close()

    def test_shared_staff_endpoint_and_mutation_actor_seam_are_relative_and_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(_snapshot(root), data_dir=root / "state")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                port = int(server.server_address[1])
                for path in ("/api/staff", "/items/api/staff"):
                    connection = HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        payload["staff"],
                        [{"username": username, "role": role} for username, role in explorer._local_store().active_staff()],
                    )
            finally:
                server.shutdown()
                server.server_close()

        item_html, customer_html = _item_explorer_html(), _customer_explorer_html()
        self.assertIn("--list-alt:#f1ede4", item_html)
        self.assertIn("--list-alt:#f1ede4", customer_html)
        self.assertIn("actor=window.apcCoreActiveStaff", item_html)
        self.assertIn("function activeStaff(){return window.apcCoreActiveStaff||''}", customer_html)
        for html in (item_html, customer_html):
            self.assertNotIn("fetch('/api/staff')", html)

    def test_main_menu_shares_the_warm_cream_shell_background(self):
        html = _menu_html_body()
        self.assertIn("--canvas:#faf7f2", html)
        self.assertNotIn("#f5f5f7", html)
        # Readability, opaque cards, module geometry, links, and focus-ring behavior are unchanged.
        self.assertIn("--paper:#fff", html)
        self.assertIn(".card{min-height:156px;border:1px solid var(--line);border-radius:20px;background:var(--paper)", html)
        self.assertIn(".card:hover,.card:focus-visible{border-color:#a9cfee", html)
        self.assertIn('href="customer-prices/"', html)
        self.assertIn("<h2>Customer Prices</h2><p>Search and safely edit imported customer-item price rows.</p></div><span class=\"open\">Open Customer Price →</span>", html)

    def test_main_menu_decoration_is_inert_low_contrast_and_confined_to_the_menu(self):
        html = _menu_html_body()
        # Inert: aria-hidden, non-focusable, pointer-events:none, never in tab order.
        self.assertIn('<div class="menu-decor" aria-hidden="true">', html)
        self.assertIn('focusable="false"', html)
        self.assertNotIn("tabindex", html)
        self.assertIn(".menu-decor{", html)
        self.assertIn("pointer-events:none", html)
        # 3-4 static, flat, low-contrast aquatic/botanical silhouette paths; no network assets.
        shape_count = html.count('<path class="decor-shape"')
        self.assertGreaterEqual(shape_count, 3)
        self.assertLessEqual(shape_count, 4)
        self.assertNotIn("url(http", html)
        self.assertNotIn("<image", html)
        # Fixed low-opacity token in the muted range, same-hue tint (existing accent, no new saturated color).
        self.assertIn("--decor-opacity:.06", html)
        self.assertIn("opacity:var(--decor-opacity)", html)
        self.assertIn(".decor-shape{fill:var(--blue)}", html)
        # Behind the module grid / in the outer gutters, never inside a card hit area; cards stay opaque and on top.
        decor_rule = ".menu-decor{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:var(--decor-opacity);overflow:hidden;mask-image:radial-gradient(circle at 50% 45%,transparent 0,transparent 38%,#000 70%);-webkit-mask-image:radial-gradient(circle at 50% 45%,transparent 0,transparent 38%,#000 70%)}"
        self.assertIn(decor_rule, html)
        self.assertIn(".shell{max-width:900px;margin:auto;padding:56px 24px;position:relative;z-index:1}", html)
        self.assertIn("background:var(--paper)", html)
        # Bleeds only from the bottom/side edges; the content center stays clear.
        self.assertIn("mask-image:radial-gradient(circle at 50% 45%,transparent 0,transparent 38%", html)
        # No shadows, animation, hover behavior, or cursor changes on the decoration
        # (the exact decor_rule match above already excludes any extra declarations).
        self.assertNotIn(".menu-decor:hover", html)
        self.assertNotIn(".decor-shape:hover", html)
        self.assertNotIn("@keyframes", html)
        # Hidden at narrow mobile widths and for forced-colors / prefers-contrast:more users.
        self.assertIn("@media(max-width:620px){.grid{grid-template-columns:1fr}.menu-decor{display:none}}", html)
        self.assertIn("@media(forced-colors:active),(prefers-contrast:more){.menu-decor{display:none}}", html)
        # Confined to the Main Menu route only.
        self.assertNotIn("menu-decor", _item_explorer_html())
        self.assertNotIn("menu-decor", _customer_explorer_html())
        # No Customer Price card/link/content change.
        self.assertIn('href="customer-prices/"', html)
        self.assertIn("<h2>Customer Prices</h2><p>Search and safely edit imported customer-item price rows.</p></div><span class=\"open\">Open Customer Price →</span>", html)


if __name__ == "__main__":
    unittest.main()
