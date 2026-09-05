import base64
import hashlib
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


class _CdpClient:
    """Minimal disposable Chromium CDP client for actual browser regressions."""

    def __init__(self, websocket_url: str):
        parsed = urlparse(websocket_url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://127.0.0.1:{parsed.port}\r\n\r\n"
        ).encode("ascii")
        self.socket.sendall(request)
        response = self.socket.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {response!r}")
        self._next_id = 1

    def close(self):
        self.socket.close()

    def _read_exact(self, size: int) -> bytes:
        chunks = []
        while size:
            chunk = self.socket.recv(size)
            if not chunk:
                raise RuntimeError("CDP websocket closed")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def _receive(self) -> dict:
        first, second = self._read_exact(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self._read_exact(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self._read_exact(8))[0]
        if second & 0x80:
            mask = self._read_exact(4)
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(self._read_exact(size)))
        else:
            payload = self._read_exact(size)
        if first & 0x0F != 1:
            return self._receive()
        return json.loads(payload)

    def call(self, method: str, params: dict | None = None) -> dict:
        message_id = self._next_id
        self._next_id += 1
        body = json.dumps({"id": message_id, "method": method, "params": params or {}}).encode("utf-8")
        mask = os.urandom(4)
        size = len(body)
        header = bytes([0x81])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
        self.socket.sendall(header + mask + masked)
        while True:
            response = self._receive()
            if response.get("id") == message_id:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response["result"]

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")


class TestOrderInvoiceBrowserBrowse(unittest.TestCase):
    def _item_snapshot(self, root: Path) -> Path:
        source = root / "items.sqlite"
        connection = sqlite3.connect(source)
        connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
        connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-001")')
        connection.commit()
        connection.close()
        return source

    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _wait_for(self, predicate, timeout: float = 10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = predicate()
            except OSError:
                value = None
            if value:
                return value
            time.sleep(0.05)
        self.fail("timed out waiting for Chromium")

    def test_actual_browser_click_renders_source_order_and_legacy_invoice_rows(self):
        """Production make_handler + real Chromium: click flow, not a direct API substitute."""
        from apc_core.active_staff_provider import ActiveStaffProvider
        from apc_core.item_explorer import ItemExplorer, make_handler
        from apc_core.order_explorer import OrderExplorer
        from apc_core.source_invoice_explorer import SourceInvoiceExplorer
        from tests.test_order_explorer import TestOrderExplorerContract
        from tests.test_source_invoice_explorer import TestSourceInvoiceExplorerContract

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = ItemExplorer(self._item_snapshot(root), data_dir=root / "state")
            orders = OrderExplorer(TestOrderExplorerContract().make_snapshot(root / "orders"))
            invoices = SourceInvoiceExplorer(TestSourceInvoiceExplorerContract().make_snapshot(root / "invoices"))
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(
                    items, {"accepted": True}, order_explorer=orders,
                    source_invoice_explorer=invoices,
                    identity_staff_provider=ActiveStaffProvider((("TESTER", "Fixture"),)),
                ),
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            browser = None
            cdp = None
            try:
                chromium = Path("/snap/bin/chromium")
                if not chromium.is_file():
                    self.skipTest("real Chromium regression requires local /snap/bin/chromium")
                port = int(server.server_address[1])
                cdp_port = self._free_port()
                profile = root / "chromium-profile"
                browser = subprocess.Popen(
                    [
                        "/snap/bin/chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                        f"--user-data-dir={profile}", f"--remote-debugging-port={cdp_port}",
                        f"--remote-allow-origins=http://127.0.0.1:{cdp_port}",
                        f"http://127.0.0.1:{port}/order-invoice/",
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                targets = self._wait_for(lambda: json.loads(urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1).read()))
                page = next(target for target in targets if target.get("type") == "page")
                cdp = _CdpClient(page["webSocketDebuggerUrl"])
                self._wait_for(lambda: cdp.evaluate("document.querySelectorAll('.identity-tile').length === 1"))
                cdp.evaluate("document.querySelector('.identity-tile').click()")
                self._wait_for(lambda: cdp.evaluate("window.apcCoreActiveStaff === 'TESTER'"))
                cdp.evaluate("document.querySelector('#open-order-invoice').click()")

                for tab_id, expected_type in (
                    ("order-invoice-tab-source-order", "source_order"),
                    ("order-invoice-tab-source-invoice", "source_invoice"),
                ):
                    cdp.evaluate(
                        "document.querySelector('#%s').click();"
                        "document.querySelector('#order-invoice-date-from').value='2026-08-01';"
                        "document.querySelector('#order-invoice-date-to').value='2026-08-31';"
                        "document.querySelector('#browse-order-invoice').click()" % tab_id
                    )
                    state = self._wait_for(lambda: cdp.evaluate(
                        "new Promise(resolve=>setTimeout(()=>resolve(JSON.stringify({"
                        "status:document.querySelector('#order-invoice-status').textContent,"
                        "rows:document.querySelectorAll('#order-invoice-results .result').length,"
                        "text:[...document.querySelectorAll('#order-invoice-results .result')].map(node=>node.textContent)"
                        "})),150))"
                    ))
                    state = json.loads(state)
                    self.assertNotEqual("Browse unavailable for the selected records.", state["status"])
                    self.assertGreaterEqual(state["rows"], 1, (expected_type, state))
                    self.assertTrue(
                        any(re.search(r"\b\d{2}/08/2026\b", text) for text in state["text"]),
                        (expected_type, state),
                    )
            finally:
                if cdp is not None:
                    cdp.close()
                if browser is not None:
                    browser.terminate()
                    browser.wait(timeout=5)
                server.shutdown()
                server.server_close()
                invoices.close()
                orders.close()
                items.close()


if __name__ == "__main__":
    unittest.main()
