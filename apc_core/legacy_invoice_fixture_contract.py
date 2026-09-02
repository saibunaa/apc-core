"""Fixture-only, read-only legacy invoice contract with a fixed SQLite child."""
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


_FIXED_CHILD = "legacy-invoice-fixture.sqlite"
_MAX_ROWS = 250
_REQUIRED_COLUMNS = {
    "MainDB__INVOICE": ("Inv No", "Cust ID", "Date"),
    "MainDB__CUST": ("Cust ID", "Name"),
}


@dataclass(frozen=True, slots=True)
class ImportedLegacyInvoice:
    invoice_id: str
    invoice_date: str
    customer_id: str
    customer_name: str


class LegacyInvoiceFixtureContract:
    def __init__(self, owner: object):
        if type(owner) is not tempfile.TemporaryDirectory:
            raise ValueError("legacy invoice fixture owner is invalid")
        self._owner = owner
        self._root = Path(owner.name)
        self._root_identity = self._directory_identity()
        self._child = self._root / _FIXED_CHILD
        self._child_identity = self._file_identity()

    def _directory_identity(self) -> tuple[int, int]:
        try:
            metadata = os.lstat(self._root)
        except OSError as error:
            raise ValueError("legacy invoice fixture root is unavailable") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("legacy invoice fixture root is invalid")
        return metadata.st_dev, metadata.st_ino

    def _file_identity(self) -> tuple[int, int]:
        try:
            metadata = os.lstat(self._child)
        except OSError as error:
            raise ValueError("legacy invoice fixture is unavailable") from error
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("legacy invoice fixture is invalid")
        return metadata.st_dev, metadata.st_ino

    def _assert_live(self) -> None:
        if Path(self._owner.name) != self._root:
            raise ValueError("legacy invoice fixture owner changed")
        if self._directory_identity() != self._root_identity or self._file_identity() != self._child_identity:
            raise ValueError("legacy invoice fixture changed")

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            found = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if found is None:
                raise ValueError("legacy invoice fixture schema is invalid")
            columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if not set(required).issubset(columns):
                raise ValueError("legacy invoice fixture schema is invalid")

    @staticmethod
    def _authorizer(action: int, _one: str | None, _two: str | None, _three: str | None, origin: str | None) -> int:
        if origin is None and action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _open_read_connection(self) -> sqlite3.Connection:
        self._assert_live()
        try:
            descriptor = os.open(self._child, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise ValueError("legacy invoice fixture cannot be opened") from error
        try:
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != self._child_identity or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("legacy invoice fixture changed")
            connection = sqlite3.connect(f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True)
        except sqlite3.Error as error:
            raise ValueError("legacy invoice fixture is not readable") from error
        finally:
            os.close(descriptor)
        try:
            connection.execute("PRAGMA query_only = ON")
            self._validate_schema(connection)
            connection.set_authorizer(self._authorizer)
            return connection
        except Exception:
            connection.close()
            raise

    def list_imported_invoices(self) -> tuple[ImportedLegacyInvoice, ...]:
        connection = self._open_read_connection()
        try:
            rows = connection.execute(
                'SELECT i."Inv No", i."Date", i."Cust ID", c."Name" '
                'FROM "MainDB__INVOICE" AS i LEFT JOIN "MainDB__CUST" AS c '
                'ON c."Cust ID"=i."Cust ID" ORDER BY i."Date", i."Inv No" LIMIT ?',
                (_MAX_ROWS,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            ImportedLegacyInvoice(
                invoice_id="" if row[0] is None else str(row[0]),
                invoice_date="" if row[1] is None else str(row[1]),
                customer_id="" if row[2] is None else str(row[2]),
                customer_name="" if row[3] is None else str(row[3]),
            )
            for row in rows
        )
