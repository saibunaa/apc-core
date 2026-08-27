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
    def test_all_program_pages_share_the_browser_scoped_identity_contract(self):
        pages = (_menu_html(), _item_explorer_html(), _customer_explorer_html())
        for html in pages:
            for marker in (
                'id="identity-confirm"',
                'Choose user',
                'Continue',
                'Change user',
                'attribution only',
                'not security, authentication, or authorization',
                'fetch("api/staff")',
                'window.apcCoreActiveStaff',
            ):
                self.assertIn(marker, html)
            self.assertIn('const key="apc-core-identity"', html)
            self.assertIn('localStorage.setItem(key,value)', html)
            self.assertNotIn('href="/items/"', html)
            self.assertNotIn('href="/customers/"', html)

        self.assertTrue(all(html.count('const key="apc-core-identity"') == 1 for html in pages))
        self.assertTrue(all(html.count('Change user') == 1 for html in pages))
        self.assertTrue(all('class="identity-card"' in html for html in pages))
        self.assertTrue(all('apc-core-known-user' in html for html in pages))
        self.assertTrue(all('localStorage.getItem("apc-core-identity")' in html for html in pages))
        item_html = _item_explorer_html()
        self.assertNotIn("$('#change-user')", item_html)
        self.assertNotIn("$('#change-user').onclick", item_html)

    def test_change_user_revokes_the_shared_identity_before_reconfirmation(self):
        for html in (_menu_html(), _item_explorer_html(), _customer_explorer_html()):
            self.assertIn('function choose(){apply("");picker.hidden=false;content.hidden=true;select.focus()}', html)
            self.assertIn('localStorage.removeItem(key)', html)
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
        self.assertIn("actor=window.apcCoreActiveStaff", item_html)
        self.assertIn("function activeStaff(){return window.apcCoreActiveStaff||''}", customer_html)
        for html in (item_html, customer_html):
            self.assertNotIn("fetch('/api/staff')", html)


if __name__ == "__main__":
    unittest.main()
