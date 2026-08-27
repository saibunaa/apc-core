import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from apc_core.item_explorer import ItemExplorer, _customer_explorer_html, _item_explorer_html, _menu_html, make_handler


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
            self.assertNotIn('html.apc-core-known-user #identity-confirm{display:none}', html)
            self.assertIn('window.dispatchEvent(new CustomEvent("apc-core-identity",{detail:value||""}))', html)

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


if __name__ == "__main__":
    unittest.main()
