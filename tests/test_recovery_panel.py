import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from apc_core.item_explorer import CoreStore, ItemExplorer, make_handler
from apc_core.recovery import RecoveryAuthorizer, RecoveryService


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _core_db(path: Path, marker: str) -> None:
    build_dir = path.parent / f".build-{path.stem}"
    store = CoreStore(build_dir)
    store.connection.execute("CREATE TABLE IF NOT EXISTS state_marker (value TEXT NOT NULL)")
    store.connection.execute("DELETE FROM state_marker")
    store.connection.execute("INSERT INTO state_marker VALUES (?)", (marker,))
    store.connection.commit()
    store.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((build_dir / "apc_core.sqlite").read_bytes())


def _snapshot(root: Path) -> Path:
    source = root / "items.sqlite"
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
    connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-001")')
    connection.commit()
    connection.close()
    return source


class RecoveryPanelAuthTests(unittest.TestCase):
    def test_recovery_panel_is_absent_by_default_and_pin_session_is_required_when_test_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(_snapshot(root), data_dir=root / "state")
            disabled = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(explorer, {"accepted": True}))
            disabled_worker = threading.Thread(target=disabled.serve_forever, daemon=True)
            disabled_worker.start()
            try:
                conn = HTTPConnection(*disabled.server_address, timeout=3)
                conn.request("GET", "/admin/recovery/")
                self.assertEqual(404, conn.getresponse().status)
                conn.close()
            finally:
                disabled.shutdown(); disabled.server_close()

            authorizer = RecoveryAuthorizer.from_test_pin("123456")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(explorer, {"accepted": True}, recovery_authorizer=authorizer)
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/admin/recovery/")
                response = conn.getresponse()
                login_html = response.read().decode("utf-8")
                conn.close()
                self.assertEqual(401, response.status)
                self.assertIn('name="pin"', login_html)
                self.assertNotIn("accepted snapshot", login_html.lower())

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/admin/recovery/login", json.dumps({"pin": "123456"}), {"Content-Type": "application/json"})
                response = conn.getresponse()
                response.read()
                cookie = response.getheader("Set-Cookie")
                conn.close()
                self.assertEqual(204, response.status)
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=Strict", cookie)
                self.assertIn("Path=/", cookie)
                self.assertNotIn("123456", cookie)

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/program/admin/recovery/", headers={"Cookie": cookie.split(";", 1)[0]})
                response = conn.getresponse()
                self.assertEqual(200, response.status)
                response.read()
                conn.close()

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/admin/recovery/", headers={"Cookie": cookie.split(";", 1)[0]})
                response = conn.getresponse()
                panel_html = response.read().decode("utf-8")
                conn.close()
                self.assertEqual(200, response.status)
                self.assertIn("Admin panel", panel_html)
                self.assertIn("saved safe copies", panel_html)
            finally:
                server.shutdown(); server.server_close()
    def test_pin_authorized_restore_rejects_non_admin_actor_and_audits_admin_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _snapshot(root)
            explorer = ItemExplorer(source, data_dir=root / "state")
            service = RecoveryService(data_dir=root / "state")
            accepted = root / "accepted.sqlite"
            _core_db(accepted, "recovered")
            service.register_accepted_snapshot(
                snapshot_id="accepted-1", artifact_path=accepted, sha256=_sha256(accepted), provenance="isolated fixture"
            )
            authorizer = RecoveryAuthorizer.from_test_pin("123456")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(
                    explorer, {"accepted": True}, recovery_authorizer=authorizer, recovery_service=service,
                    recovery_maintenance=explorer.close,
                )
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                payload = {"snapshot_id": "accepted-1", "actor": "BIAS", "reason": "test reset", "confirmation": "accepted-1"}
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/admin/recovery/restore", json.dumps(payload), {"Content-Type": "application/json"})
                self.assertEqual(401, conn.getresponse().status)
                conn.close()

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/admin/recovery/login", json.dumps({"pin": "123456"}), {"Content-Type": "application/json"})
                response = conn.getresponse(); response.read()
                cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                conn.close()

                payload["actor"] = "YIM"
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/admin/recovery/restore", json.dumps(payload), {"Content-Type": "application/json", "Cookie": cookie})
                self.assertEqual(403, conn.getresponse().status)
                conn.close()

                payload["actor"] = "BIAS"
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/admin/recovery/restore", json.dumps(payload), {"Content-Type": "application/json", "Cookie": cookie})
                response = conn.getresponse()
                result = json.loads(response.read())
                conn.close()
                self.assertEqual(200, response.status)
                self.assertEqual("passed", result["validation_result"])
                self.assertEqual("BIAS", service.audit_entries()[0]["actor"])
            finally:
                server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
