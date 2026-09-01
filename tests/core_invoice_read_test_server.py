from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from apc_core.core_invoice_read_page import make_core_invoice_read_handler


class CoreInvoiceReadTestServer:
    """Own one loopback-only test server for the unmounted invoice-read handler."""

    def __init__(self, database_path: Path, *, active_staff: tuple[tuple[str, str], ...]):
        handler_class = make_core_invoice_read_handler(database_path, active_staff=active_staff)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self._worker = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> CoreInvoiceReadTestServer:
        self._worker.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._worker.join(timeout=3)


def core_invoice_read_test_server(
    database_path: Path, *, active_staff: tuple[tuple[str, str], ...]
) -> CoreInvoiceReadTestServer:
    return CoreInvoiceReadTestServer(database_path, active_staff=active_staff)
