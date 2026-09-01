"""Per-request, local-only read boundary for already-migrated Core invoices."""
from __future__ import annotations

import sqlite3
from pathlib import Path


_REQUIRED_MIGRATION_VERSION = 5


class CoreInvoiceReadConnectionError(ValueError):
    """A Core invoice database cannot be safely opened for read-only access."""


class CoreInvoiceReadConnection:
    """One closeable read-only SQLite connection for a single Core request."""

    def __init__(self, database_path: Path) -> None:
        path = Path(database_path)
        if not path.is_file():
            raise CoreInvoiceReadConnectionError("Core invoice database is missing")
        try:
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
        except sqlite3.Error as error:
            raise CoreInvoiceReadConnectionError("Core invoice database cannot be opened") from error

        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            versions = {row[0] for row in connection.execute("SELECT version FROM core_schema_migrations")}
            if _REQUIRED_MIGRATION_VERSION not in versions:
                raise CoreInvoiceReadConnectionError("Core invoice migrations have not been applied")
        except sqlite3.Error as error:
            connection.close()
            raise CoreInvoiceReadConnectionError("Core invoice migrations have not been applied") from error
        except Exception:
            connection.close()
            raise

        self.connection = connection

    def close(self) -> None:
        self.connection.close()


def open_core_invoice_read_connection(database_path: Path) -> CoreInvoiceReadConnection:
    """Open a fresh, closeable read-only boundary for an existing P5 Core DB."""
    return CoreInvoiceReadConnection(database_path)
