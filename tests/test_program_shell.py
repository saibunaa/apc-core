import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from apc_core.item_explorer import ItemExplorer, _customer_explorer_html, _item_explorer_html, _menu_html, _menu_html_body, make_handler
from apc_core.order_explorer import OrderExplorer


def _snapshot(root: Path) -> Path:
    source = root / "items.sqlite"
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
    connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-001")')
    connection.commit()
    connection.close()
    return source


def _order_snapshot(root: Path) -> Path:
    source = root / "orders.sqlite"
    connection = sqlite3.connect(source)
    for table, definition in {
        "MainDB__ORDER": '"Order No" TEXT, "Order Date" TEXT, "Cust ID" TEXT',
        "MainDB__ORDER_ITEM": '"Order No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Qty" TEXT',
        "MainDB__CUST": '"Cust ID" TEXT, "Name" TEXT, "Inv Type" TEXT',
        "MainDB__CUST_CON": '"Cust ID" TEXT, "Com Code" TEXT',
        "MainDB__CUST_CONSIGNEE": '"Cust ID" TEXT, "Consignee" TEXT',
        "MainDB__CUST_NOTE": '"Cust ID" TEXT, "Order" TEXT, "Invoice" TEXT',
        "MainDB__ITEM": '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT',
    }.items():
        connection.execute(f'CREATE TABLE "{table}" ({definition})')
    connection.execute('INSERT INTO "MainDB__ORDER" VALUES (?, ?, ?)', ("ORD/1", "2026-08-29", "C/1"))
    connection.execute('INSERT INTO "MainDB__ORDER_ITEM" VALUES (?, ?, ?, ?)', ("ORD/1", "1", "I/1", "1"))
    connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?, ?)', ("C/1", "Customer One", "invoice config"))
    connection.execute('INSERT INTO "MainDB__CUST_CON" VALUES (?, ?)', ("C/1", "order config"))
    connection.execute('INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?)', ("C/1", "Bangkok"))
    connection.execute('INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)', ("C/1", "order note", "invoice note"))
    connection.execute('INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?)', ("IT-1", "Item one", "สินค้า"))
    connection.commit(); connection.close()
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
                ".identity-tone-1{background:#f7c948}",
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
            self.assertIn('.identity-action,.back{pointer-events:auto;position:fixed;top:16px;z-index:11;display:inline-flex;align-items:center;', html)
            self.assertNotIn('html.apc-core-known-user #identity-confirm{display:none}', html)
            self.assertIn('window.dispatchEvent(new CustomEvent("apc-core-identity",{detail:value||""}))', html)

    def test_navigation_and_identity_polish_lift_on_hover_and_rotate_tile_colors_per_visit(self):
        pages = (_menu_html(), _item_explorer_html(), _customer_explorer_html())
        for html in pages:
            self.assertIn('body{background:#eadbc8', html)
            self.assertIn('.identity-picker-screen{position:fixed', html)
            self.assertIn('transparent 30%),#eadbc8', html)
            self.assertIn('.identity-card{width:min(980px,100%);padding:clamp(22px,4vw,46px);border:3px solid #24272b;border-radius:22px;background:#fffdfa', html)
            self.assertIn('.identity-tile:hover{transform:translate(-2px,-2px);box-shadow:8px 8px 0 #24272b;outline:none}', html)
            self.assertIn('.identity-action:hover,.back:hover{transform:translate(-1px,-1px);box-shadow:0 4px 10px #24272b33}', html)
            self.assertIn('.identity-tile:focus-visible{transform:translate(-2px,-2px);box-shadow:9px 9px 0 #24272b;outline:3px solid #174d3e;outline-offset:3px}', html)
            self.assertIn('colorVisitKey="apc-core-identity-color-visit"', html)
            self.assertIn('if(!Number.isFinite(colorVisit))colorVisit=0', html)
            self.assertIn('tile.classList.add("identity-tone-"+((index+colorVisit)%4+1))', html)
            self.assertIn('.identity-tone-1{background:#f7c948}', html)
            self.assertIn('.identity-tone-4{background:#a9c9f4}', html)
            self.assertIn('.identity-action,.back{pointer-events:auto;position:fixed;top:16px;z-index:11;display:inline-flex;align-items:center;min-height:42px;border:1px solid var(--line);border-radius:14px;padding:0 14px;background:var(--paper);color:var(--accent);font-weight:700;box-shadow:0 2px 6px #24272b26;', html)
            self.assertIn('.identity-action[hidden]{display:none}', html)
            self.assertIn('.identity-action{right:16px}', html)
            self.assertIn('.back{left:16px}', html)
            self.assertIn('.back+h1,.back+.top{margin-top:74px}', html)

        for html in (_item_explorer_html(), _customer_explorer_html()):
            self.assertIn('class="back" href="../">Main menu</a>', html)
            self.assertNotIn('← APC Core', html)

    def test_customer_workspace_keeps_profile_visible_and_does_not_force_page_scroll(self):
        html = _customer_explorer_html()
        for marker in (".profile{position:sticky", ".back{left:16px}", 'class="back" href="../"', "@media(max-width:760px){.shell{padding:12px}.workspace{grid-template-columns:1fr}.profile{position:static;align-self:auto;border:0;border-top:1px solid var(--line)}", "Main menu"):
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
        self.assertIn("--canvas:#eadbc8", html)
        self.assertNotIn("#f5f5f7", html)
        # Clickable modules are fixed, low-saturation soft bricks for spatial memory.
        self.assertIn("--paper:#fff", html)
        self.assertIn(".card{min-height:156px;border:2px solid var(--line);border-radius:20px;background:var(--paper)", html)
        self.assertIn(".card.mint{background:var(--mint-tint);border-color:var(--mint-mid)}", html)
        self.assertIn(".card.pink{background:var(--pink-tint);border-color:var(--pink-mid)}", html)
        self.assertIn(".card.blue{background:var(--blue-tint);border-color:var(--blue-mid)}", html)
        self.assertIn(".card.mint:hover,.card.mint:focus-visible{transform:translate(-2px,-2px);box-shadow:6px 6px 0 rgba(95,184,144,.45)}", html)
        self.assertIn('class="card mint" href="items/"', html)
        self.assertIn('class="card pink" href="customers/"', html)
        self.assertIn('class="card blue" href="customer-prices/"', html)
        self.assertIn(".card.soon{background:#fbf9f5;border:2px dashed var(--line);box-shadow:none;opacity:.75}", html)
        self.assertIn("--accent:#1d6b57", html)
        self.assertIn(".open{font-weight:600;color:var(--accent)}", html)
        self.assertNotIn("--blue:", html)
        self.assertIn('href="customer-prices/"', html)
        self.assertIn("<h2>Customer Prices</h2><p>Search and safely edit imported customer-item price rows.</p></div><span class=\"open\">Open Customer Price →</span>", html)

    def test_main_menu_has_no_botanical_or_plant_silhouette_background(self):
        html = _menu_html_body()
        self.assertNotIn("menu-decor", html)
        self.assertNotIn("decor-shape", html)
        self.assertNotIn("<svg", html)
        self.assertNotIn("<path", html)
        self.assertIn("--canvas:#eadbc8", html)
        self.assertIn(".shell{max-width:900px;margin:auto;padding:56px 24px;position:relative;z-index:1}", html)
        self.assertIn("background:var(--paper)", html)
        self.assertIn('href="customer-prices/"', html)
        self.assertIn("<h2>Customer Prices</h2><p>Search and safely edit imported customer-item price rows.</p></div><span class=\"open\">Open Customer Price →</span>", html)

    def test_order_forms_are_canonical_get_only_routes_with_no_mutation_fallthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(_snapshot(root), data_dir=root / "state")
            orders = OrderExplorer(_order_snapshot(root))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}, order_explorer=orders))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                port = int(server.server_address[1])
                cases = (("/program/orders/", 200), ("/program/orders/api/orders", 200), ("/program/orders/api/orders/ORD%2F1", 200), ("/program/orders/api/customer-template/C%2F1", 200))
                for path, expected in cases:
                    connection = HTTPConnection("127.0.0.1", port, timeout=3); connection.request("GET", path)
                    response = connection.getresponse(); payload = response.read(); connection.close()
                    self.assertEqual(expected, response.status, path)
                    self.assertTrue(payload)
                for method in ("POST", "PUT", "PATCH", "DELETE"):
                    connection = HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request(method, "/program/orders/api/orders", b"{}", {"Content-Type": "application/json"})
                    response = connection.getresponse(); response.read(); connection.close()
                    self.assertEqual(405, response.status, method)
            finally:
                server.shutdown(); server.server_close(); explorer.close(); orders.close()

    def test_order_forms_route_uses_the_shared_staff_identity_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(_snapshot(root), data_dir=root / "state")
            orders = OrderExplorer(_order_snapshot(root))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}, order_explorer=orders))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                connection = HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=3)
                connection.request("GET", "/program/orders/")
                response = connection.getresponse(); html = response.read().decode("utf-8"); connection.close()
                self.assertEqual(200, response.status)
                for marker in ('id="identity-confirm"', 'id="identity-picker"', 'data-identity-username', 'window.apcCoreActiveStaff'):
                    self.assertIn(marker, html)
            finally:
                server.shutdown(); server.server_close(); explorer.close(); orders.close()

    def test_order_template_endpoint_resolves_no_order_customer_exact_and_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _order_snapshot(root)
            connection = sqlite3.connect(source)
            connection.executemany(
                'INSERT INTO "MainDB__CUST" VALUES (?, ?, ?)',
                [("C/NO-ORDER", "No order customer", "invoice config"), ("C/NO-OTHER", "Another no order customer", "")],
            )
            connection.commit(); connection.close()
            explorer = ItemExplorer(_snapshot(root), data_dir=root / "state")
            orders = OrderExplorer(source)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}, order_explorer=orders))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                port = int(server.server_address[1])
                for code in ("c%2Fno-order", "c%2Fno-"):
                    connection = HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", "/program/orders/api/customer-template/" + code)
                    response = connection.getresponse(); payload = json.loads(response.read()); connection.close()
                    self.assertEqual(200, response.status)
                    self.assertEqual("C/NO-ORDER", payload["customer_id"])
                connection = HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", "/program/orders/api/customer-template/C%2FMISSING")
                response = connection.getresponse(); response.read(); connection.close()
                self.assertEqual(404, response.status)
            finally:
                server.shutdown(); server.server_close(); explorer.close(); orders.close()

    def test_order_forms_modal_ui_is_safe_readonly_and_keyboard_operable(self):
        import apc_core.item_explorer as module
        html = module._order_explorer_html()
        for marker in (
            'id="frmOrderForm"', 'id="open-order-forms"', 'id="frmOrderFormList"', 'role="dialog"',
            'Date', 'Cust', 'Country', 'AWB', 'Order No.', 'B/I/M/P/W/U/T', 'function loadOrder(',
            "event.key==='Escape'", 'openButton.focus()', "credentials:'same-origin'", "cache:'no-store'",
            'id="customer-code-options"', 'function commitCustomerCode(', 'textContent=', 'preview-only',
        ):
            self.assertIn(marker, html)
        self.assertIn('href="orders/"', _menu_html_body())
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("fetch('/", html)
        self.assertNotRegex(html, r"fetch\([^)]*method\s*:")

    def test_order_forms_customer_template_ui_hydrates_the_typed_code_not_only_loaded_orders(self):
        import apc_core.item_explorer as module
        html = module._order_explorer_html()
        self.assertIn("templateFor(typed)", html)
        self.assertIn("$('#customer-code').value=data.customer_id", html)


if __name__ == "__main__":
    unittest.main()
