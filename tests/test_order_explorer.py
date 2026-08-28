import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestOrderExplorerContract(unittest.TestCase):
    """Fixture schema is the accepted read-only Order Explorer source contract.

    MainDB__ORDER: Order No, Order Date, Cust ID
    MainDB__ORDER_ITEM: Order No, Line No, Item ID, Qty
    MainDB__CUST: Cust ID, Name, Inv Type
    MainDB__CUST_CON: Cust ID, Com Code
    MainDB__CUST_CONSIGNEE: Cust ID, Consignee
    MainDB__CUST_NOTE: Cust ID, Order, Invoice
    MainDB__ITEM: Item ID, Description, Description TH
    """

    module_path = Path(__file__).parents[1] / "apc_core" / "order_explorer.py"

    def make_snapshot(self, root: Path, *, missing_table: str | None = None, missing_column: bool = False) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "accepted-order.sqlite"
        con = sqlite3.connect(source)
        tables = {
            "MainDB__ORDER": '"Order No" TEXT, "Order Date" TEXT, "Cust ID" TEXT',
            "MainDB__ORDER_ITEM": '"Order No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Qty" TEXT',
            "MainDB__CUST": '"Cust ID" TEXT, "Name" TEXT, "Inv Type" TEXT',
            "MainDB__CUST_CON": '"Cust ID" TEXT, "Com Code" TEXT',
            "MainDB__CUST_CONSIGNEE": '"Cust ID" TEXT, "Consignee" TEXT',
            "MainDB__CUST_NOTE": '"Cust ID" TEXT, "Order" TEXT, "Invoice" TEXT',
            "MainDB__ITEM": '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT',
        }
        if missing_column:
            tables["MainDB__ORDER"] = '"Order No" TEXT, "Cust ID" TEXT'
        for table, definition in tables.items():
            if table != missing_table:
                con.execute(f'CREATE TABLE "{table}" ({definition})')
        if missing_table is None and not missing_column:
            con.executemany(
                'INSERT INTO "MainDB__ORDER" VALUES (?, ?, ?)',
                [
                    ("ORD/2026/001", "2026-08-29", "C/001"),
                    ("ORD/2026/001-X", "2026-08-30", "C/002"),
                ],
            )
            con.executemany(
                'INSERT INTO "MainDB__ORDER_ITEM" VALUES (?, ?, ?, ?)',
                [
                    ("ORD/2026/001", "10", "IT/DUP", ""),
                    ("ORD/2026/001", "2", "IT/DUP", "5"),
                    ("ORD/2026/001", "3", "IT/TH", "๑๒"),
                    ("ORD/2026/001-X", "1", "IT/OTHER", "1"),
                ],
            )
            con.executemany(
                'INSERT INTO "MainDB__CUST" VALUES (?, ?, ?)',
                [("C/001", "บริษัท ไทย <script>alert(1)</script>", "invoice <b>config</b>"), ("C/002", "Other customer", "")],
            )
            con.executemany(
                'INSERT INTO "MainDB__CUST_CON" VALUES (?, ?)',
                [("C/001", "order <b>config</b>"), ("C/002", "")],
            )
            con.executemany(
                'INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?)',
                [("C/001", "กรุงเทพฯ & <unsafe>"), ("C/001", "Tokyo")],
            )
            con.executemany(
                'INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)',
                [("C/001", "order note <img src=x>", "invoice note <svg onload=1>"), ("C/002", "", "")],
            )
            con.executemany(
                'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?)',
                [("IT/DUP", "Duplicate <b>item</b>", "สินค้าซ้ำ"), ("IT/TH", "Thai item", "สินค้าไทย"), ("IT/OTHER", "Other", "อื่น")],
            )
        con.commit()
        con.close()
        return source

    def explorer_class(self):
        from apc_core.order_explorer import OrderExplorer
        return OrderExplorer

    def test_00_order_explorer_source_module_is_required(self):
        self.assertTrue(self.module_path.is_file(), "apc_core/order_explorer.py must exist")

    def test_constructor_rejects_missing_required_table_or_column_with_typed_readonly_contract_error(self):
        from apc_core.order_explorer import ReadOnlySourceContractError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for source in (
                self.make_snapshot(root / "missing-table", missing_table="MainDB__ORDER_ITEM"),
                self.make_snapshot(root / "missing-column", missing_column=True),
            ):
                with self.assertRaises(ReadOnlySourceContractError):
                    self.explorer_class()(source)

    def test_readonly_descriptor_source_hash_and_bytes_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_snapshot(Path(tmp))
            before = source.read_bytes()
            before_hash = hashlib.sha256(before).hexdigest()
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                explorer = self.explorer_class().from_open_descriptor(descriptor, source)
                self.assertEqual(before_hash, explorer.source_sha256)
                self.assertEqual("ORD/2026/001", explorer.open_order("ORD/2026/001")["order_id"])
                explorer.close()
            finally:
                os.close(descriptor)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(before_hash, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_search_orders_filters_dates_customer_and_bounded_pages_with_allowlisted_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            page = explorer.search_orders(customer="C/001", date_from="2026-08-29", date_to="2026-08-29", limit=999, offset=-5)
            self.assertEqual(1, page["total"])
            self.assertEqual(250, page["limit"])
            self.assertEqual(0, page["offset"])
            self.assertEqual({"total", "limit", "offset", "has_more", "next_offset", "orders"}, set(page))
            self.assertEqual({"order_id", "order_date", "customer_id", "customer_name"}, set(page["orders"][0]))
            self.assertEqual("ORD/2026/001", page["orders"][0]["order_id"])
            self.assertEqual("บริษัท ไทย <script>alert(1)</script>", page["orders"][0]["customer_name"])
            self.assertEqual([], explorer.search_orders(customer="<script>", limit=1)["orders"])
            explorer.close()

    def test_open_order_is_exact_preserves_slashes_duplicate_lines_blank_qty_and_numeric_line_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            order = explorer.open_order("ORD/2026/001")
            self.assertEqual({"order_id", "order_date", "customer_id", "customer_name", "lines"}, set(order))
            self.assertEqual(["2", "3", "10"], [line["line_no"] for line in order["lines"]])
            self.assertEqual(["IT/DUP", "IT/TH", "IT/DUP"], [line["item_id"] for line in order["lines"]])
            self.assertEqual("", order["lines"][2]["qty"])
            self.assertEqual("สินค้าซ้ำ", order["lines"][0]["item_description_th"])
            self.assertEqual({"line_no", "item_id", "item_description", "item_description_th", "qty"}, set(order["lines"][0]))
            self.assertIsNone(explorer.open_order("ORD/2026/001/"))
            self.assertIsNone(explorer.open_order("ORD/2026/00"))
            self.assertIsNone(explorer.open_order("ORD/2026/001-X/extra"))
            self.assertIsNone(explorer.open_order(42))
            explorer.close()

    def test_customer_template_returns_separated_allowlisted_customer_configuration_candidates_and_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            template = explorer.customer_template("C/001")
            self.assertEqual(
                {"customer_id", "customer_name", "consignee_candidates", "order_config", "invoice_config", "order_notes", "invoice_notes"},
                set(template),
            )
            self.assertEqual("C/001", template["customer_id"])
            self.assertEqual("บริษัท ไทย <script>alert(1)</script>", template["customer_name"])
            self.assertEqual(["Tokyo", "กรุงเทพฯ & <unsafe>"], template["consignee_candidates"])
            self.assertEqual("order <b>config</b>", template["order_config"])
            self.assertEqual("invoice <b>config</b>", template["invoice_config"])
            self.assertEqual(["order note <img src=x>"], template["order_notes"])
            self.assertEqual(["invoice note <svg onload=1>"], template["invoice_notes"])
            self.assertIsNone(explorer.customer_template("C/001/"))
            explorer.close()

    def test_customer_template_resolves_any_customer_case_insensitive_exact_then_deterministic_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_snapshot(Path(tmp))
            connection = sqlite3.connect(source)
            # These accepted customers deliberately have no MainDB__ORDER row.
            connection.executemany(
                'INSERT INTO "MainDB__CUST" VALUES (?, ?, ?)',
                [("C/NO-ORDER", "No order customer", "resolved invoice config"), ("C/NO-OTHER", "Another no order customer", "")],
            )
            connection.execute(
                'INSERT INTO "MainDB__CUST_CON" VALUES (?, ?)',
                ("C/NO-ORDER", "resolved order config"),
            )
            connection.execute(
                'INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?)',
                ("C/NO-ORDER", "Resolved consignee"),
            )
            connection.execute(
                'INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)',
                ("C/NO-ORDER", "resolved order note", "resolved invoice note"),
            )
            connection.commit()
            connection.close()
            explorer = self.explorer_class()(source)
            exact = explorer.customer_template("c/no-order")
            prefix = explorer.customer_template("c/no-")
            self.assertIsNotNone(exact)
            self.assertIsNotNone(prefix)
            for template in (exact, prefix):
                self.assertEqual("C/NO-ORDER", template["customer_id"])
                self.assertEqual("resolved order config", template["order_config"])
                self.assertEqual("resolved invoice config", template["invoice_config"])
                self.assertEqual(["Resolved consignee"], template["consignee_candidates"])
                self.assertEqual(["resolved order note"], template["order_notes"])
                self.assertEqual(["resolved invoice note"], template["invoice_notes"])
            self.assertIsNone(explorer.customer_template("C/MISSING"))
            explorer.close()

    def test_snapshot_collection_queries_have_sql_limits_before_rows_are_materialized(self):
        """Every variable-size snapshot collection is bounded by SQL, not Python slicing."""
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer_class()(self.make_snapshot(Path(tmp)))
            statements: list[str] = []
            explorer._connection.set_trace_callback(statements.append)
            explorer.search_orders(limit=1)
            explorer.open_order("ORD/2026/001")
            explorer.customer_template("C/001")
            explorer._connection.set_trace_callback(None)
            collection_queries = [
                statement.upper() for statement in statements
                if ("FROM \"MAINDB__ORDER\"" in statement.upper() and "SELECT \"ORDER NO\"" in statement.upper())
                or "FROM \"MAINDB__ORDER_ITEM\"" in statement.upper()
                or "FROM \"MAINDB__CUST_CONSIGNEE\"" in statement.upper()
                or "FROM \"MAINDB__CUST_NOTE\"" in statement.upper()
            ]
            self.assertTrue(collection_queries)
            self.assertTrue(all(" LIMIT " in statement for statement in collection_queries), collection_queries)
            explorer.close()

    def test_detail_and_template_collections_are_capped_without_changing_dto_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_snapshot(Path(tmp))
            connection = sqlite3.connect(source)
            connection.executemany(
                'INSERT INTO "MainDB__ORDER_ITEM" VALUES (?, ?, ?, ?)',
                [("ORD/2026/001", str(index + 100), f"CAP-{index}", "1") for index in range(300)],
            )
            connection.executemany(
                'INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?)',
                [("C/001", f"Consignee {index:03d}") for index in range(300)],
            )
            connection.executemany(
                'INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)',
                [("C/001", f"order {index}", f"invoice {index}") for index in range(300)],
            )
            connection.commit()
            connection.close()
            explorer = self.explorer_class()(source)
            self.assertLessEqual(len(explorer.open_order("ORD/2026/001")["lines"]), 250)
            template = explorer.customer_template("C/001")
            self.assertLessEqual(len(template["consignee_candidates"]), 250)
            self.assertLessEqual(len(template["order_notes"]), 250)
            self.assertLessEqual(len(template["invoice_notes"]), 250)
            explorer.close()
