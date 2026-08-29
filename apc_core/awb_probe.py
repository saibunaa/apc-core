"""Read-only schema probe for the shipment/AWB tables.

Answers the open schema question without waiting on anyone: does the accepted
snapshot carry MainDB__AWB / MainDB__INVOICE / MainDB__FREIGHT, does
MainDB__CUST_CON still carry Charges, and are XRate and exRate two columns
or one?

Prints column *names* only.  No row counts, no values, no writes.  Opens the
artifact through the same fd + ``mode=ro&immutable=1`` + ``query_only=ON`` path
every other reader uses, so it cannot mutate the snapshot even by accident.
"""

import argparse
import os
import sqlite3
import stat
import sys
from pathlib import Path


PROBE_TABLES = (
    "MainDB__AWB",
    "MainDB__INVOICE",
    "MainDB__FREIGHT",
    "MainDB__CUST",
    "MainDB__CUST_CON",
)
# Columns whose presence or absence changes a design decision.
WATCHED = {
    "MainDB__AWB": ("Inv No", "AWB", "AWB Date", "shipby", "AWB Box", "Province",
                    "Weight", "RATE", "Agent", "Carrier", "Total THB", "Total US", "exRate", "XRate"),
    "MainDB__CUST_CON": ("Charges", "RATE", "Formula Type"),
}


def probe(source_path: Path) -> dict[str, list[str] | None]:
    descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("probe source must be a regular SQLite file")
        connection = sqlite3.connect(f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            report: dict[str, list[str] | None] = {}
            for table in PROBE_TABLES:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()
                report[table] = None if exists is None else [
                    str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
            return report
        finally:
            connection.close()
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only AWB schema probe (column names only)")
    parser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = probe(args.snapshot)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"probe failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    for table, columns in report.items():
        if columns is None:
            print(f"{table}: ABSENT")
            continue
        print(f"{table}: {len(columns)} columns")
        for column in columns:
            print(f"    {column}")
        watched = WATCHED.get(table)
        if watched:
            folded = {column.casefold() for column in columns}
            for name in watched:
                print(f"  [{'x' if name.casefold() in folded else ' '}] {name}")


if __name__ == "__main__":
    main()
