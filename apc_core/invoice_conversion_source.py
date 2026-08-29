"""Bounded read-only conversion evidence from an accepted SQLite snapshot."""

import hashlib
import os
import sqlite3
import stat
import threading
from pathlib import Path


_REQUIRED_COLUMNS = {
    "MainDB__ORDER": ("Order No", "Order Date", "Cust ID"),
    "MainDB__ORDER_ITEM": ("Order No", "Line No", "Item ID", "Qty"),
    "MainDB__CUST": ("Cust ID", "Name"),
}
_MAX_ROWS = 250


class ReadOnlyInvoiceSourceError(ValueError):
    """The source does not meet the closed invoice conversion read contract."""


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _line_key(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


class InvoiceConversionSource:
    """Expose allowlisted, non-mutating invoice-conversion source evidence only."""

    def __init__(self, source_path: Path, *, current_price_lookup=None):
        self.source_path = Path(source_path)
        self._price_lookup = current_price_lookup
        descriptor = os.open(self.source_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self._initialize(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def from_open_descriptor(cls, descriptor: int, artifact_path: Path, *, current_price_lookup=None):
        """Construct from a duplicate of an already validated, open artifact FD."""
        if type(descriptor) is not int:
            raise ReadOnlyInvoiceSourceError("invalid accepted artifact descriptor")
        instance = cls.__new__(cls)
        instance.source_path = Path(artifact_path)
        instance._price_lookup = current_price_lookup
        duplicate = os.dup(descriptor)
        try:
            instance._initialize(duplicate)
        finally:
            os.close(duplicate)
        return instance

    def _initialize(self, descriptor: int) -> None:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReadOnlyInvoiceSourceError("invoice source must be a regular SQLite file")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        try:
            self._connection.execute("PRAGMA query_only = ON")
            self._columns = self._validate_schema()
            self.source_sha256 = self._hash_descriptor(descriptor)
        except Exception:
            self._connection.close()
            raise

    @staticmethod
    def _hash_descriptor(descriptor: int) -> str:
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            handle.seek(0)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _validate_schema(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for table, required in _REQUIRED_COLUMNS.items():
            exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if exists is None:
                raise ReadOnlyInvoiceSourceError(f"invoice source lacks {table}")
            columns = {str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({_quoted(table)})")}
            if not set(required).issubset(columns):
                raise ReadOnlyInvoiceSourceError(f"invoice source lacks required {table} columns")
            result[table] = {column.casefold(): column for column in columns}
        return result

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _limit(value: object) -> int:
        try:
            return max(1, min(int(value), _MAX_ROWS))
        except (TypeError, ValueError):
            raise ValueError("invalid candidate limit") from None

    def _field(self, table: str, names: tuple[str, ...], alias: str) -> str:
        columns = self._columns[table]
        for name in names:
            actual = columns.get(name.casefold())
            if actual is not None:
                return f"{alias}.{_quoted(actual)}"
        return "NULL"

    @staticmethod
    def _metadata(values: list[str]) -> dict[str, object]:
        distinct = sorted({value for value in values if value})
        if not distinct:
            return {"status": "MISSING", "values": []}
        if len(distinct) == 1:
            return {"status": "UNANIMOUS", "values": distinct}
        return {"status": "CONFLICTING", "values": distinct}

    def _current_price(self, customer_id: str, item_id: str) -> dict[str, str]:
        if not customer_id or not item_id or self._price_lookup is None:
            return {"status": "UNKNOWN", "value": ""}
        try:
            result = self._price_lookup(customer_id, item_id)
        except Exception:
            return {"status": "UNKNOWN", "value": ""}
        if not isinstance(result, dict):
            return {"status": "UNKNOWN", "value": ""}
        status = result.get("status")
        value = result.get("value")
        if type(status) is not str or type(value) is not str:
            return {"status": "UNKNOWN", "value": ""}
        return {"status": status, "value": value}

    def read_order(self, order_id: object) -> dict[str, object] | None:
        if type(order_id) is not str:
            return None
        header_time = self._field("MainDB__ORDER", ("Order Time",), "o")
        header_shipment = self._field("MainDB__ORDER", ("Shipment Date",), "o")
        header_awb = self._field("MainDB__ORDER", ("AWB",), "o")
        item_description = self._field("MainDB__ORDER_ITEM", ("Description",), "oi")
        item_price = self._field("MainDB__ORDER_ITEM", ("Unit Price", "Price"), "oi")
        item_note = self._field("MainDB__ORDER_ITEM", ("Note",), "oi")
        item_shipment = self._field("MainDB__ORDER_ITEM", ("Shipment Date",), "oi")
        item_awb = self._field("MainDB__ORDER_ITEM", ("AWB",), "oi")
        with self._lock:
            header = self._connection.execute(
                'SELECT o."Order No", o."Order Date", o."Cust ID", c."Name", '
                f"{header_time}, {header_shipment}, {header_awb} "
                'FROM "MainDB__ORDER" AS o LEFT JOIN "MainDB__CUST" AS c ON c."Cust ID" = o."Cust ID" '
                'WHERE o."Order No" = ? LIMIT 1',
                (order_id,),
            ).fetchone()
            if header is None:
                return None
            rows = self._connection.execute(
                'SELECT oi."Line No", oi."Item ID", oi."Qty", '
                f"{item_description}, {item_price}, {item_note}, {item_shipment}, {item_awb} "
                'FROM "MainDB__ORDER_ITEM" AS oi WHERE oi."Order No" = ? LIMIT ?',
                (order_id, _MAX_ROWS + 1),
            ).fetchall()
            if len(rows) > _MAX_ROWS:
                raise ReadOnlyInvoiceSourceError("invoice source order exceeds maximum line count")
        lines = []
        shipment_dates = [_text(header[5])]
        awbs = [_text(header[6])]
        for row in rows:
            item_id = _text(row[1])
            note = _text(row[5])
            lines.append(
                {
                    "line_id": _text(row[0]),
                    "item_id": item_id,
                    "quantity": _text(row[2]),
                    "description": _text(row[3]),
                    "source_unit_price": _text(row[4]),
                    "is_annotation": not item_id and bool(note),
                    "annotation_text": note if not item_id else "",
                    "current_price": self._current_price(_text(header[2]), item_id),
                }
            )
            shipment_dates.append(_text(row[6]))
            awbs.append(_text(row[7]))
        lines.sort(key=lambda line: _line_key(line["line_id"]))
        return {
            "source_sha256": self.source_sha256,
            "order_id": _text(header[0]),
            "order_date": _text(header[1]),
            "order_time_text": _text(header[4]),
            "customer_id": _text(header[2]),
            "customer_name": _text(header[3]),
            "lines": lines,
            "shipment_metadata": {
                "shipment_date": self._metadata(shipment_dates),
                "awb": self._metadata(awbs),
            },
        }

    def discover_legacy_candidates(
        self, customer_id: object, shipment_date: object, *, limit: object = 50
    ) -> dict[str, object]:
        page_limit = self._limit(limit)
        if type(customer_id) is not str or type(shipment_date) is not str:
            return {"kind": "display_legacy_candidate_set", "limit": page_limit, "candidates": []}
        shipment = self._field("MainDB__ORDER", ("Shipment Date",), "o")
        awb = self._field("MainDB__ORDER", ("AWB",), "o")
        if shipment == "NULL":
            return {"kind": "display_legacy_candidate_set", "limit": page_limit, "candidates": []}
        with self._lock:
            rows = self._connection.execute(
                'SELECT o."Order No", o."Order Date", '
                f"{shipment}, {awb} FROM \"MainDB__ORDER\" AS o "
                'WHERE o."Cust ID" = ? AND ' + shipment + ' = ? '
                'ORDER BY o."Order No" LIMIT ?',
                (customer_id, shipment_date, page_limit),
            ).fetchall()
        return {
            "kind": "display_legacy_candidate_set",
            "limit": page_limit,
            "candidates": [
                {
                    "order_id": _text(row[0]),
                    "order_date": _text(row[1]),
                    "shipment_date": _text(row[2]),
                    "awb": _text(row[3]),
                }
                for row in rows
            ],
        }
