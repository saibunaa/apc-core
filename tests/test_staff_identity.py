import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from apc_core.item_explorer import ItemExplorer, make_handler


class StaffIdentityFoundationTests(unittest.TestCase):
    def make_snapshot(self, root: Path) -> Path:
        source = root / "latest.sqlite"
        connection = sqlite3.connect(source)
        connection.execute(
            'CREATE TABLE "MainDB__ITEM" ('
            '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, '
            '"Type" TEXT, "Family" TEXT)'
        )
        connection.execute(
            'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?)',
            ("IT-001", "Neon tetra", "นีออนเตตร้า", "Fish", "Tropical"),
        )
        connection.commit()
        connection.close()
        return source

    def test_store_migrates_and_idempotently_seeds_exact_active_staff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            explorer = ItemExplorer(source, data_dir=root / "state")
            store = explorer._local_store()

            self.assertEqual(
                [("BIAS", "Admin"), ("BON", "Editor"), ("DERRICK", "Admin"),
                 ("WAT", "Editor"), ("YA", "Editor"), ("YIM", "Editor")],
                store.active_staff(),
            )
            self.assertIn("actor_username", {row[1] for row in store.connection.execute("PRAGMA table_info(activity)")})
            explorer.close()
            reopened = ItemExplorer(source, data_dir=root / "state")
            self.assertEqual(6, reopened._local_store().connection.execute("SELECT COUNT(*) FROM core_users").fetchone()[0])

    def test_item_mutations_require_exact_active_staff_and_attribute_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "state")

            for actor in (None, "yim", "UNKNOWN", "YIM ", "YIM" * 100):
                with self.assertRaises(ValueError):
                    explorer.edit("IT-001", {"description": "Edited"}, actor)
            updated = explorer.edit("IT-001", {"description": "Edited"}, "YIM")
            duplicate = explorer.duplicate("IT-001", "BIAS")

            self.assertEqual("Edited", updated["description"])
            self.assertEqual("IT-001", duplicate["original_item_id"])
            self.assertEqual(
                [("IT-001", "YIM")],
                explorer._local_store().connection.execute(
                    "SELECT item_id, actor_username FROM activity ORDER BY id"
                ).fetchall(),
            )

    def test_mutation_api_requires_actor_and_html_has_confirmed_nonsecurity_picker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "state")
            handler = make_handler(explorer, {"accepted": True})
            from http.server import ThreadingHTTPServer
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                for payload, expected in (({"description": "Edited"}, 400), ({"description": "Edited", "actor": "UNKNOWN"}, 400), ({"actor": "YIM"}, 400)):
                    conn = HTTPConnection(host, port, timeout=3)
                    conn.request("POST", "/api/items/IT-001/duplicate", json.dumps(payload), {"Content-Type": "application/json"})
                    response = conn.getresponse()
                    response.read()
                    conn.close()
                    self.assertEqual(expected, response.status)
            finally:
                server.shutdown()
                server.server_close()

        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()
        for marker in ("identity-tiles", "identity-tile", "identity-change-user", "localStorage", "not security", "identity-confirm", "api/staff"):
            self.assertIn(marker, html)
        self.assertNotIn('id="confirm-user"', html)


if __name__ == "__main__":
    unittest.main()
