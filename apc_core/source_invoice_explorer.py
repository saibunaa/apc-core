"""Descriptor-pinned, read-only explorer for accepted source invoice snapshots."""

import hashlib
import os
import sqlite3
import stat
import threading
from pathlib import Path


_REQUIRED_COLUMNS = {
    "MainDB__INVOICE": (
        "Inv No", "Cust ID", "Date", "AWB", "ShipBy", "ShipBy2", "Box",
        "Total Amt", "Total Qty", "Total QtyTC", "Total QtyCHV", "XRate",
        "Consignee", "Province", "Country", "Time", "Time2", "Broker",
    ),
    "MainDB__INV_ITEM": ("Inv No", "Line No", "Item ID", "Description", "Qty", "Price", "Amount", "SubCust"),
    "MainDB__CUST": ("Cust ID", "Price Type", "Name"),
    "MainDB__ITEM": ("Item ID", "Description", "Description TH"),
}
_MAX_PAGE = 250


class ReadOnlySourceInvoiceError(ValueError):
    """The source is not the accepted, closed invoice-reader schema."""


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _slash_family(invoice_id: str) -> str:
    """Display-only classification; the original identifier is never normalized."""
    if "//" in invoice_id:
        return "repeated_slash"
    if "/" in invoice_id:
        return "single_slash"
    return "no_slash"


def _escape_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SourceInvoiceExplorer:
    """Bounded, immutable adapter exposing only source-invoice DTOs."""

    def __init__(self, source_path: Path):
        self.source_path = Path(source_path)
        try:
            descriptor = os.open(self.source_path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise ReadOnlySourceInvoiceError("invoice source cannot be opened read-only") from error
        try:
            self._initialize_from_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def from_open_descriptor(cls, descriptor: int, source_path: Path) -> "SourceInvoiceExplorer":
        if type(descriptor) is not int:
            raise ReadOnlySourceInvoiceError("invalid invoice source descriptor")
        explorer = cls.__new__(cls)
        explorer.source_path = Path(source_path)
        explorer._initialize_from_descriptor(descriptor)
        return explorer

    def _initialize_from_descriptor(self, descriptor: int) -> None:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReadOnlySourceInvoiceError("invoice source must be a regular SQLite file")
        self._lock = threading.RLock()
        self._source_uri = f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1"
        try:
            self._connection = sqlite3.connect(self._source_uri, uri=True, check_same_thread=False)
        except sqlite3.Error as error:
            raise ReadOnlySourceInvoiceError("invoice source is not a readable SQLite database") from error
        try:
            self._connection.execute("PRAGMA query_only = ON")
            self._validate_schema()
            self.source_sha256 = self._hash_descriptor(descriptor)
        except sqlite3.Error as error:
            self._connection.close()
            raise ReadOnlySourceInvoiceError("invoice source is not a readable SQLite database") from error
        except Exception:
            self._connection.close()
            raise

    def _validate_schema(self) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if exists is None:
                raise ReadOnlySourceInvoiceError(f"invoice source lacks {table}")
            columns = {str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({_quote(table)})")}
            if not set(required).issubset(columns):
                raise ReadOnlySourceInvoiceError(f"invoice source lacks required {table} columns")

    @staticmethod
    def _hash_descriptor(descriptor: int) -> str:
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            handle.seek(0)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _page(limit: object, offset: object) -> tuple[int, int]:
        if type(limit) is not int or type(offset) is not int:
            raise ValueError("invalid invoice page")
        return max(1, min(limit, _MAX_PAGE)), max(0, offset)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def search_invoices(
        self, invoice_id: object = "", *, prefix: object = "", limit: object = 50, offset: object = 0
    ) -> dict[str, object]:
        page_limit, page_offset = self._page(limit, offset)
        if type(invoice_id) is not str or type(prefix) is not str:
            raise ValueError("invalid invoice filter")
        if invoice_id and prefix:
            raise ValueError("invoice filters are mutually exclusive")
        source = ' FROM "MainDB__INVOICE" AS i LEFT JOIN "MainDB__CUST" AS c ON c."Cust ID" = i."Cust ID"'
        if invoice_id:
            where, parameters = ' WHERE i."Inv No" = ?', [invoice_id]
        elif prefix:
            where, parameters = ' WHERE i."Inv No" LIKE ? ESCAPE \'\\\'', [_escape_prefix(prefix)]
        else:
            where, parameters = "", []
        with self._lock:
            total = int(self._connection.execute("SELECT COUNT(*)" + source + where, parameters).fetchone()[0])
            rows = self._connection.execute(
                'SELECT i."Inv No", i."Date", i."Cust ID", c."Name"' + source + where
                + ' ORDER BY i."Date", i."Inv No" LIMIT ? OFFSET ?',
                [*parameters, page_limit, page_offset],
            ).fetchall()
        invoices = [
            {"source_type": "source_invoice", "invoice_id": _text(row[0]), "invoice_date": _text(row[1]), "customer_id": _text(row[2]),
             "customer_name": _text(row[3]), "slash_family": _slash_family(_text(row[0]))}
            for row in rows
        ]
        next_offset = page_offset + page_limit
        return {"total": total, "limit": page_limit, "offset": page_offset,
                "has_more": next_offset < total, "next_offset": next_offset if next_offset < total else None,
                "invoices": invoices}

    def open_invoice(self, invoice_id: object, *, limit: object = 50, offset: object = 0) -> dict[str, object] | None:
        page_limit, page_offset = self._page(limit, offset)
        if type(invoice_id) is not str:
            return None
        with self._lock:
            header = self._connection.execute(
                'SELECT i."Inv No", i."Date", i."Cust ID", c."Name" '
                'FROM "MainDB__INVOICE" AS i LEFT JOIN "MainDB__CUST" AS c ON c."Cust ID" = i."Cust ID" '
                'WHERE i."Inv No" = ? LIMIT 1', (invoice_id,),
            ).fetchone()
            if header is None:
                return None
            total = int(self._connection.execute(
                'SELECT COUNT(*) FROM "MainDB__INV_ITEM" WHERE "Inv No" = ?', (invoice_id,)
            ).fetchone()[0])
            rows = self._connection.execute(
                'SELECT "Line No", "Item ID", "Description", "Qty", "Price", "Amount", "SubCust" '
                'FROM "MainDB__INV_ITEM" WHERE "Inv No" = ? '
                'ORDER BY CAST("Line No" AS INTEGER), "Line No" LIMIT ? OFFSET ?',
                (invoice_id, page_limit, page_offset),
            ).fetchall()
        next_offset = page_offset + page_limit
        return {
            "source_sha256": self.source_sha256,
            "source_type": "source_invoice",
            "invoice_id": _text(header[0]),
            "slash_family": _slash_family(_text(header[0])),
            "header": {"invoice_date": _text(header[1]), "customer_id": _text(header[2]), "customer_name": _text(header[3])},
            "total": total, "limit": page_limit, "offset": page_offset,
            "has_more": next_offset < total, "next_offset": next_offset if next_offset < total else None,
            "lines": [
                {"line_no": _text(row[0]), "item_id": _text(row[1]), "description": _text(row[2]),
                 "qty": _text(row[3]), "price": _text(row[4]), "amount": _text(row[5]), "sub_customer": _text(row[6])}
                for row in rows
            ],
        }
