"""Unmounted, loopback-only GET page for P5 Core invoice read evidence.

This local factory is deliberately not imported by the runtime server. A later,
separately approved composition gate owns any route or service integration.
"""
from __future__ import annotations

import ipaddress
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from apc_core.core_invoice_read_connection import (
    CoreInvoiceReadConnectionError,
    open_core_invoice_read_connection,
)
from apc_core.invoice_read_projection import project_invoice_list
from apc_core.invoice_workflow_ui import invoice_list_html
from apc_core.item_explorer import _staff_identity_shell


_PAGE_PATH = "/private-invoice-read/"


def _active_staff(staff: object) -> frozenset[str]:
    if not isinstance(staff, tuple):
        raise ValueError("active staff must be a tuple")
    names: list[str] = []
    for entry in staff:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError("active staff entry is invalid")
        name, role = entry
        if type(name) is not str or not name or type(role) is not str or not role:
            raise ValueError("active staff entry is invalid")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("active staff entries must be unique")
    return frozenset(names)


def _list_projection(database_path: Path) -> list[dict[str, object]]:
    """Read only the P5 fields needed by the existing invoice list adapter."""
    boundary = open_core_invoice_read_connection(database_path)
    try:
        rows = boundary.connection.execute(
            "SELECT d.invoice_id,d.state,d.permanent_number,d.created_by,d.created_at,"
            "c.temporary_reference,c.consignee,c.delivery_reference,"
            "(SELECT MIN(source.document_id) FROM core_invoice_document_lines dl "
            "JOIN core_invoice_lines il ON il.invoice_line_id=dl.core_invoice_line_id "
            "JOIN core_order_lines ol ON ol.line_id=il.order_line_id "
            "JOIN core_source_rows source ON source.snapshot_sha256=ol.snapshot_sha256 "
            "AND source.source_table=ol.source_table AND source.source_rowid=ol.source_rowid "
            "WHERE dl.invoice_id=d.invoice_id) AS order_number "
            "FROM core_invoice_documents d "
            "JOIN core_invoice_document_context c ON c.invoice_id=d.invoice_id "
            "ORDER BY d.created_at,d.invoice_id"
        ).fetchall()
        return [project_invoice_list({
            "receipt": {
                "invoice_id": row["invoice_id"], "state": row["state"], "version": 1,
                "permanent_number": row["permanent_number"],
                "temporary_reference": row["temporary_reference"], "consignee": row["consignee"],
                "delivery_reference": row["delivery_reference"],
            },
            "created_by": row["created_by"], "created_at": row["created_at"],
            "customer": {"customer_code": row["temporary_reference"].split("-T", 1)[0], "approved_name": None},
            "evidence_reference": "Core source evidence",
            "order_number": row["order_number"],
            "lines": (),
        }) for row in rows]
    finally:
        boundary.close()


def _page_html(records: list[dict[str, object]]) -> str:
    html = invoice_list_html(records).replace("Fixture display · read only", "Core invoice read · display only", 1)
    return _staff_identity_shell(html)


def make_core_invoice_read_handler(database_path: Path, *, active_staff: tuple[tuple[str, str], ...]):
    """Create a non-mounted HTTP handler for a local invoice-read test harness."""
    database_path = Path(database_path)
    permitted_staff = _active_staff(active_staff)

    class CoreInvoiceReadHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, status: HTTPStatus, payload: dict[str, str]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _valid_loopback_actor(self) -> bool:
            try:
                loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "loopback access required"})
                return False
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            actors = query.get("actor", [])
            if set(query) != {"actor"} or len(actors) != 1 or actors[0] not in permitted_staff:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "active staff attribution required"})
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != _PAGE_PATH:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._valid_loopback_actor():
                return
            try:
                self._send_html(_page_html(_list_projection(database_path)))
            except CoreInvoiceReadConnectionError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "invoice read data unavailable"})
            except (KeyError, TypeError, ValueError):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "invoice read data unavailable"})

        def _method_not_allowed(self) -> None:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "GET required"})

        do_POST = _method_not_allowed
        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_HEAD = _method_not_allowed
        do_OPTIONS = _method_not_allowed
        do_TRACE = _method_not_allowed
        do_CONNECT = _method_not_allowed

    return CoreInvoiceReadHandler
