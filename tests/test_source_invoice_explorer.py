import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestSourceInvoiceExplorerContract(unittest.TestCase):
    module_path = Path(__file__).parents[1] / "apc_core" / "source_invoice_explorer.py"

    def make_snapshot(self, root: Path, *, missing_table: str | None = None, missing_column: bool = False) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "accepted-invoice.sqlite"
        con = sqlite3.connect(source)
        tables = {
            "MainDB__INVOICE": '"Inv No" TEXT, "Cust ID" TEXT, "Date" TEXT, "AWB" TEXT, "ShipBy" TEXT, "ShipBy2" TEXT, "Box" TEXT, "Total Amt" TEXT, "Total Qty" TEXT, "Total QtyTC" TEXT, "Total QtyCHV" TEXT, "XRate" TEXT, "Consignee" TEXT, "Province" TEXT, "Country" TEXT, "Time" TEXT, "Time2" TEXT, "Broker" TEXT',
            "MainDB__INV_ITEM": '"Inv No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Description" TEXT, "Qty" TEXT, "Price" TEXT, "Amount" TEXT, "SubCust" TEXT',
            "MainDB__CUST": '"Cust ID" TEXT, "Price Type" TEXT, "Name" TEXT',
            "MainDB__ITEM": '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT',
        }
        if missing_column:
            tables["MainDB__INVOICE"] = '"Inv No" TEXT, "Cust ID" TEXT, "Date" TEXT'
        for table, definition in tables.items():
            if table != missing_table:
                con.execute(f'CREATE TABLE "{table}" ({definition})')
        if missing_table is None and not missing_column:
            con.executemany('INSERT INTO "MainDB__INVOICE" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                ("C//2026/001", "C/001", "2026-08-29", "DO-NOT-EXPOSE", "", "", "", "", "", "", "", "", "", "", "", "", "", ""),
                ("C//2026/002", "C/001", "2026-08-30", "DO-NOT-EXPOSE", "", "", "", "", "", "", "", "", "", "", "", "", "", ""),
                ("C/2026/003", "C/002", "2026-08-31", "DO-NOT-EXPOSE", "", "", "", "", "", "", "", "", "", "", "", "", "", ""),
            ])
            con.executemany('INSERT INTO "MainDB__INV_ITEM" VALUES (?, ?, ?, ?, ?, ?, ?, ?)', [
                ("C//2026/001", "10", "I/10", "source ten", "1", "2", "2", ""),
                ("C//2026/001", "2", "I/2", "source two", "3", "4", "12", "SC/2"),
                ("C//2026/001", "3", "I/3", "source three", "5", "6", "30", ""),
            ])
            con.executemany('INSERT INTO "MainDB__CUST" VALUES (?, ?, ?)', [
                ("C/001", "PT", "Customer One"), ("C/002", "PT", "Customer Two"),
            ])
            con.executemany('INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?)', [
                ("I/10", "catalog ten", "สิบ"), ("I/2", "catalog two", "สอง"), ("I/3", "catalog three", "สาม"),
            ])
        con.commit()
        con.close()
        return source

    def explorer_class(self):
        from apc_core.source_invoice_explorer import SourceInvoiceExplorer
        return SourceInvoiceExplorer

    def test_00_source_invoice_explorer_module_is_required(self):
        self.assertTrue(self.module_path.is_file(), "apc_core/source_invoice_explorer.py must exist")

    def test_constructor_rejects_missing_required_table_or_column_with_typed_error(self):
        from apc_core.source_invoice_explorer import ReadOnlySourceInvoiceError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for source in (self.make_snapshot(root / "missing-table", missing_table="MainDB__INV_ITEM"), self.make_snapshot(root / "missing-column", missing_column=True)):
                with self.assertRaises(ReadOnlySourceInvoiceError):
                    self.explorer_class()(source)

    def test_constructor_normalizes_malformed_sqlite_initialization_to_typed_error(self):
        from apc_core.source_invoice_explorer import ReadOnlySourceInvoiceError
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "malformed.sqlite"
            source.write_bytes(b"not a SQLite database")
            with self.assertRaises(ReadOnlySourceInvoiceError):
                self.explorer_class()(source)

    def test_constructor_rejects_symlink_source_without_following_or_creating_target(self):
        from apc_core.source_invoice_explorer import ReadOnlySourceInvoiceError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_target = root / "missing.sqlite"
            source = root / "source-link.sqlite"
            source.symlink_to(missing_target)
            with self.assertRaises(ReadOnlySourceInvoiceError):
                self.explorer_class()(source)
            self.assertTrue(source.is_symlink())
            self.assertFalse(missing_target.exists())

    def test_constructor_rejects_missing_source_without_creating_it(self):
        from apc_core.source_invoice_explorer import ReadOnlySourceInvoiceError
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "missing.sqlite"
            with self.assertRaises(ReadOnlySourceInvoiceError):
                self.explorer_class()(source)
            self.assertFalse(source.exists())

    def test_descriptor_reader_uses_immutable_proc_fd_uri_and_preserves_bytes_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_snapshot(Path(tmp))
            before = source.read_bytes()
            digest = hashlib.sha256(before).hexdigest()
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                explorer = self.explorer_class().from_open_descriptor(descriptor, source)
                self.assertEqual(f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", explorer._source_uri)
                self.assertEqual(digest, explorer.source_sha256)
                self.assertEqual(1, explorer._connection.execute("PRAGMA query_only").fetchone()[0])
                self.assertEqual("source_invoice", explorer.open_invoice("C//2026/001")["source_type"])
                explorer.close()
            finally:
                os.close(descriptor)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_search_supports_exact_id_and_escaped_prefix_with_bounded_page_metadata_and_closed_dtos(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            exact = explorer.search_invoices(invoice_id="C//2026/001", limit=999, offset=-3)
            self.assertEqual({"total", "limit", "offset", "has_more", "next_offset", "invoices"}, set(exact))
            self.assertEqual(1, exact["total"])
            self.assertEqual(250, exact["limit"])
            self.assertEqual(0, exact["offset"])
            self.assertEqual({"source_type", "invoice_id", "invoice_date", "customer_id", "customer_name", "slash_family"}, set(exact["invoices"][0]))
            self.assertEqual("source_invoice", exact["invoices"][0]["source_type"])
            self.assertEqual("C//2026/001", exact["invoices"][0]["invoice_id"])
            self.assertEqual("repeated_slash", exact["invoices"][0]["slash_family"])
            prefix = explorer.search_invoices(prefix="C//2026/", limit=1, offset=1)
            self.assertEqual(2, prefix["total"])
            self.assertEqual(1, prefix["limit"])
            self.assertEqual(1, prefix["offset"])
            self.assertTrue(prefix["has_more"] is False)
            self.assertIsNone(prefix["next_offset"])
            self.assertEqual(["C//2026/002"], [row["invoice_id"] for row in prefix["invoices"]])
            self.assertEqual([], explorer.search_invoices(prefix="C%", limit=1)["invoices"])
            explorer.close()

    def test_open_invoice_is_exact_preserves_slash_heavy_id_and_paginates_numeric_lines_without_inference_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            page = explorer.open_invoice("C//2026/001", limit=2, offset=0)
            self.assertEqual({"source_sha256", "source_type", "invoice_id", "slash_family", "header", "total", "limit", "offset", "has_more", "next_offset", "lines"}, set(page))
            self.assertEqual("C//2026/001", page["invoice_id"])
            self.assertEqual("repeated_slash", page["slash_family"])
            self.assertEqual({"invoice_date", "customer_id", "customer_name"}, set(page["header"]))
            self.assertEqual(["2", "3"], [line["line_no"] for line in page["lines"]])
            self.assertEqual({"line_no", "item_id", "description", "qty", "price", "amount", "sub_customer"}, set(page["lines"][0]))
            self.assertEqual("source two", page["lines"][0]["description"])
            self.assertTrue(page["has_more"])
            self.assertEqual(2, page["next_offset"])
            last = explorer.open_invoice("C//2026/001", limit=2, offset=2)
            self.assertEqual(["10"], [line["line_no"] for line in last["lines"]])
            self.assertFalse(last["has_more"])
            self.assertIsNone(last["next_offset"])
            self.assertIsNone(explorer.open_invoice("C/2026/001"))
            self.assertEqual("single_slash", explorer.open_invoice("C/2026/003")["slash_family"])
            rendered = repr(page)
            for forbidden in ("AWB", "DO-NOT-EXPOSE", "order_id", "candidate", "link", "derived", "inference"):
                self.assertNotIn(forbidden, rendered)
            explorer.close()

    def test_page_arguments_reject_booleans_before_they_reach_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            with self.assertRaises(ValueError):
                explorer.search_invoices(limit=True)
            with self.assertRaises(ValueError):
                explorer.open_invoice("C//2026/001", offset=False)
            explorer.close()

    def test_variable_size_search_and_detail_queries_have_sql_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            statements = []
            explorer._connection.set_trace_callback(statements.append)
            explorer.search_invoices(prefix="C", limit=1)
            explorer.open_invoice("C//2026/001", limit=1)
            explorer._connection.set_trace_callback(None)
            collections = [
                statement.upper() for statement in statements
                if ((('FROM "MAINDB__INVOICE"' in statement.upper()) and ('SELECT' in statement.upper())
                     and ('COUNT' not in statement.upper()))
                    or (('FROM "MAINDB__INV_ITEM"' in statement.upper()) and ('COUNT' not in statement.upper())))
            ]
            self.assertTrue(collections)
            self.assertTrue(all(" LIMIT " in statement for statement in collections), collections)
            explorer.close()
