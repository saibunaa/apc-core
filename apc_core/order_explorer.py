"""Read-only Order Explorer for an accepted SQLite order snapshot.

The source schema is deliberately closed.  Only the columns listed in
``_REQUIRED_COLUMNS`` are read, and returned dictionaries are allowlisted DTOs.
"""

import hashlib
import os
import sqlite3
import stat
import threading
from pathlib import Path


_REQUIRED_COLUMNS = {
    "MainDB__ORDER": ("Order No", "Order Date", "Cust ID", "Customer Name"),
    "MainDB__ORDER_ITEM": ("Order No", "Line No", "Item ID", "Qty"),
    "MainDB__CUST": ("Cust ID", "Name"),
    "MainDB__CUST_CON": ("Cust ID", "Order Config", "Invoice Config"),
    "MainDB__CUST_CONSIGNEE": ("Cust ID", "Consignee"),
    "MainDB__CUST_NOTE": ("Cust ID", "Order", "Invoice"),
    "MainDB__ITEM": ("Item ID", "Description", "Description TH"),
}
_MAX_DETAIL_ROWS = 250
_MAX_TEMPLATE_ROWS = 250


class ReadOnlySourceContractError(ValueError):
    """The accepted source is not the closed, read-only Order Explorer schema."""


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _line_sort_key(line_no: str) -> tuple[int, int | str]:
    try:
        return (0, int(line_no))
    except ValueError:
        return (1, line_no)


class OrderExplorer:
    """Bounded, immutable reader for accepted Order data only."""

    def __init__(self, source_path: Path):
        self.source_path = Path(source_path)
        descriptor = os.open(self.source_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self._initialize_from_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def from_open_descriptor(cls, descriptor: int, source_path: Path) -> "OrderExplorer":
        explorer = cls.__new__(cls)
        explorer.source_path = Path(source_path)
        explorer._initialize_from_descriptor(descriptor)
        return explorer

    def _initialize_from_descriptor(self, descriptor: int) -> None:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReadOnlySourceContractError("order explorer source must be a regular SQLite file")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        try:
            self._connection.execute("PRAGMA query_only = ON")
            self._validate_schema()
            self.source_sha256 = self._hash_descriptor(descriptor)
        except Exception:
            self._connection.close()
            raise

    def _validate_schema(self) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if exists is None:
                raise ReadOnlySourceContractError(f"order explorer source lacks {table}")
            columns = {str(row[1]) for row in self._connection.execute(f'PRAGMA table_info("{table}")')}
            if not set(required).issubset(columns):
                raise ReadOnlySourceContractError(f"order explorer source lacks required {table} columns")

    @staticmethod
    def _hash_descriptor(descriptor: int) -> str:
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as source:
            source.seek(0)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return digest.hexdigest()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _page(limit: object, offset: object) -> tuple[int, int]:
        try:
            return max(1, min(int(limit), 250)), max(0, int(offset))
        except (TypeError, ValueError):
            raise ValueError("invalid order page") from None

    def search_orders(
        self,
        customer: object = "",
        date_from: object = "",
        date_to: object = "",
        limit: object = 50,
        offset: object = 0,
    ) -> dict[str, object]:
        page_limit, page_offset = self._page(limit, offset)
        if any(type(value) is not str for value in (customer, date_from, date_to)):
            raise ValueError("invalid order filter")
        clauses: list[str] = []
        parameters: list[str] = []
        if customer:
            clauses.append('("Cust ID" = ? OR "Customer Name" = ?)')
            parameters.extend((customer, customer))
        if date_from:
            clauses.append('"Order Date" >= ?')
            parameters.append(date_from)
        if date_to:
            clauses.append('"Order Date" <= ?')
            parameters.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        count_query = 'SELECT COUNT(*) FROM "MainDB__ORDER"' + where
        query = (
            'SELECT "Order No", "Order Date", "Cust ID", "Customer Name" '
            'FROM "MainDB__ORDER"' + where + ' ORDER BY "Order Date" DESC, "Order No" LIMIT ? OFFSET ?'
        )
        with self._lock:
            total = int(self._connection.execute(count_query, parameters).fetchone()[0])
            rows = self._connection.execute(query, [*parameters, page_limit, page_offset]).fetchall()
        orders = [
            {"order_id": _text(row[0]), "order_date": _text(row[1]), "customer_id": _text(row[2]), "customer_name": _text(row[3])}
            for row in rows
        ]
        next_offset = page_offset + page_limit
        return {
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None,
            "orders": orders,
        }

    def open_order(self, order_id: object) -> dict[str, object] | None:
        if type(order_id) is not str:
            return None
        with self._lock:
            order = self._connection.execute(
                'SELECT "Order No", "Order Date", "Cust ID", "Customer Name" '
                'FROM "MainDB__ORDER" WHERE "Order No" = ? LIMIT 1',
                (order_id,),
            ).fetchone()
            if order is None:
                return None
            rows = self._connection.execute(
                'SELECT oi."Line No", oi."Item ID", item."Description", item."Description TH", oi."Qty" '
                'FROM "MainDB__ORDER_ITEM" AS oi '
                'LEFT JOIN "MainDB__ITEM" AS item ON item."Item ID" = oi."Item ID" '
                'WHERE oi."Order No" = ? LIMIT ?',
                (order_id, _MAX_DETAIL_ROWS),
            ).fetchall()
        lines = [
            {
                "line_no": _text(row[0]),
                "item_id": _text(row[1]),
                "item_description": _text(row[2]),
                "item_description_th": _text(row[3]),
                "qty": _text(row[4]),
            }
            for row in rows
        ]
        lines.sort(key=lambda line: _line_sort_key(line["line_no"]))
        return {
            "order_id": _text(order[0]),
            "order_date": _text(order[1]),
            "customer_id": _text(order[2]),
            "customer_name": _text(order[3]),
            "lines": lines,
        }

    def customer_template(self, customer_id: object) -> dict[str, object] | None:
        if type(customer_id) is not str or not customer_id:
            return None
        prefix = customer_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            customer = self._connection.execute(
                'SELECT "Cust ID", "Name" FROM "MainDB__CUST" '
                'WHERE LOWER("Cust ID") = LOWER(?) OR LOWER("Cust ID") LIKE LOWER(?) || \'%\' ESCAPE \'\\\' '
                'ORDER BY CASE WHEN LOWER("Cust ID") = LOWER(?) THEN 0 ELSE 1 END, "Cust ID" COLLATE NOCASE, "Cust ID" LIMIT 1',
                (customer_id, prefix, customer_id),
            ).fetchone()
            if customer is None:
                return None
            canonical_customer_id = _text(customer[0])
            config = self._connection.execute(
                'SELECT "Order Config", "Invoice Config" FROM "MainDB__CUST_CON" WHERE "Cust ID" = ? LIMIT 1', (canonical_customer_id,)
            ).fetchone()
            consignees = self._connection.execute(
                'SELECT "Consignee" FROM "MainDB__CUST_CONSIGNEE" WHERE "Cust ID" = ? ORDER BY "Consignee" LIMIT ?',
                (canonical_customer_id, _MAX_TEMPLATE_ROWS),
            ).fetchall()
            notes = self._connection.execute(
                'SELECT "Order", "Invoice" FROM "MainDB__CUST_NOTE" WHERE "Cust ID" = ? LIMIT ?',
                (canonical_customer_id, _MAX_TEMPLATE_ROWS),
            ).fetchall()
        return {
            "customer_id": _text(customer[0]),
            "customer_name": _text(customer[1]),
            "consignee_candidates": [_text(row[0]) for row in consignees],
            "order_config": _text(config[0]) if config is not None else "",
            "invoice_config": _text(config[1]) if config is not None else "",
            "order_notes": [_text(row[0]) for row in notes if _text(row[0])],
            "invoice_notes": [_text(row[1]) for row in notes if _text(row[1])],
        }
