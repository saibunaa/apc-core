import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path


class CustomerPriceModuleContractTests(unittest.TestCase):
    def make_snapshot(self, root: Path) -> Path:
        source = root / "accepted-prices.sqlite"
        connection = sqlite3.connect(source)
        connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT, "Price Type" TEXT)')
        connection.executemany(
            'INSERT INTO "MainDB__CUST" VALUES (?, ?, ?)',
            [("C-001", "Pacific Plants", "EU"), ("C-002", "Thai Plants", "TH")],
        )
        connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT)')
        connection.executemany(
            'INSERT INTO "MainDB__ITEM" VALUES (?, ?)',
            [("IT-001", "Anubias"), ("IT-002", "Cryptocoryne")],
        )
        connection.execute('CREATE TABLE "MainDB__CUST_PRC" ("Cust ID" TEXT, "Item ID" TEXT, "Price" TEXT)')
        connection.executemany(
            'INSERT INTO "MainDB__CUST_PRC" VALUES (?, ?, ?)',
            [
                ("C-001", "IT-001", "12.50"),
                ("C-001", "IT-002", "20"),
                ("C-002", "IT-001", "30"),
                # Source duplicates must never be picked implicitly.
                ("C-002", "IT-001", "31"),
                # Unknown source relationships are never promoted to Core rows.
                ("C-404", "IT-001", "40"),
                ("C-001", "IT-404", "50"),
            ],
        )
        connection.commit()
        connection.close()
        return source

    def test_imports_readonly_snapshot_prices_by_customer_item_natural_key_with_provenance_and_quarantine(self):
        from apc_core.customer_price_module import CustomerPriceModule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            source_before = source.read_bytes()
            prices = CustomerPriceModule(source, data_dir=root / "state")

            summary = prices.import_from_snapshot()
            result = prices.search("C-001", query="anub")

            self.assertEqual({"accepted": 2, "duplicate": 2, "unknown": 2, "preserved": 0}, summary)
            self.assertEqual("C-001", result["customer_code"])
            self.assertEqual(["IT-001"], [row["item_id"] for row in result["rows"]])
            self.assertEqual("12.50", result["rows"][0]["price"])
            self.assertEqual(hashlib.sha256(source_before).hexdigest(), result["rows"][0]["source_artifact_sha256"])
            self.assertEqual("snapshot", result["rows"][0]["provenance"])
            self.assertNotIn("price_type", result)
            self.assertEqual(source_before, source.read_bytes())
            self.assertEqual(
                {("C-002", "IT-001", "duplicate_natural_key"), ("C-404", "IT-001", "unknown_customer"), ("C-001", "IT-404", "unknown_item")},
                {(entry["customer_code"], entry["item_id"], entry["reason"]) for entry in prices.quarantine()},
            )

    def test_later_snapshot_retires_missing_price_rows_so_they_are_not_searchable_or_editable(self):
        from apc_core.customer_price_module import CustomerPriceModule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.make_snapshot(root)
            second = root / "later.sqlite"
            connection = sqlite3.connect(second)
            connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
            connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?)', ("C-001", "Pacific Plants"))
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES (?, ?)', ("IT-001", "Anubias"))
            connection.execute('CREATE TABLE "MainDB__CUST_PRC" ("Cust ID" TEXT, "Item ID" TEXT, "Price" TEXT)')
            connection.execute('INSERT INTO "MainDB__CUST_PRC" VALUES (?, ?, ?)', ("C-001", "IT-001", "13"))
            connection.commit(); connection.close()
            state = root / "state"

            old = CustomerPriceModule(first, data_dir=state)
            old.import_from_snapshot(); old.close()
            current = CustomerPriceModule(second, data_dir=state)
            current.import_from_snapshot()

            self.assertEqual(["IT-001"], [row["item_id"] for row in current.search("C-001")["rows"]])
            self.assertEqual("13", current.search("C-001")["rows"][0]["price"])
            with self.assertRaises(ValueError):
                current.edit("C-001", "IT-002", "22", "YIM")

    def test_individual_edit_requires_selected_actor_preserves_raw_price_type_and_audits_before_after(self):
        from apc_core.customer_price_module import CustomerPriceModule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = CustomerPriceModule(self.make_snapshot(root), data_dir=root / "state")
            prices.import_from_snapshot()

            with self.assertRaises(ValueError):
                prices.edit("C-001", "IT-001", "13.75", None)
            edited = prices.edit("C-001", "IT-001", "13.75", "YIM")

            self.assertEqual("13.75", edited["price"])
            self.assertEqual("core_override", edited["provenance"])
            self.assertEqual(
                [{
                    "customer_code": "C-001", "item_id": "IT-001", "action": "price_edited",
                    "before": {"price": "12.50"}, "after": {"price": "13.75"}, "actor_username": "YIM",
                }],
                prices.activity("C-001"),
            )
            self.assertNotIn("Price Type", repr(prices.activity("C-001")))

    def test_tsv_preview_accepts_item_id_price_rows_without_a_header_and_ignores_a_conventional_header(self):
        from apc_core.customer_price_module import CustomerPriceModule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = CustomerPriceModule(self.make_snapshot(root), data_dir=root / "state")
            prices.import_from_snapshot()

            headerless = prices.preview_tsv("C-001", "IT-001\t14.00\nIT-002\t22\n")
            spaced = prices.preview_tsv("C-001", "IT-001 14.00\nIT-002 22\n")
            headed = prices.preview_tsv("C-001", " item id \t PRICE \nIT-001\t14.00\n")

            self.assertEqual(["IT-001", "IT-002"], [row["item_id"] for row in headerless["valid"]])
            self.assertEqual(["IT-001", "IT-002"], [row["item_id"] for row in spaced["valid"]])
            self.assertEqual([1, 2], [row["line"] for row in headerless["valid"]])
            self.assertEqual(["IT-001"], [row["item_id"] for row in headed["valid"]])
            self.assertEqual([2], [row["line"] for row in headed["valid"]])

    def test_customer_price_html_is_keyboard_first_with_autocomplete_edit_mode_and_modal_bulk_paste(self):
        from apc_core.customer_price_module import CustomerPriceModule

        with tempfile.TemporaryDirectory() as tmp:
            prices = CustomerPriceModule(self.make_snapshot(Path(tmp)), data_dir=Path(tmp) / "state")
            html = prices.html()

        for marker in (
            'role="combobox"', 'aria-autocomplete="list"', 'list="customer-options"',
            'id="customer-options"', 'Edit prices', 'Bulk edit', 'role="dialog"',
            'aria-modal="true"', 'header row is optional', 'addEventListener("keydown"',
            'Escape', 'editMode', 'bulk-dialog', 'commitCustomer', 'e.key==="Tab"', 'lastPage', 'const code=selected()', 'if(selected()===code)render(p)', '.rows tbody tr:nth-child(even)',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('class="paste', html)

    def test_tsv_preview_classifies_valid_invalid_unknown_duplicate_and_changes_without_mutating_until_apply(self):
        from apc_core.customer_price_module import CustomerPriceModule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = CustomerPriceModule(self.make_snapshot(root), data_dir=root / "state")
            prices.import_from_snapshot()
            paste = "Item ID\tPrice\nIT-001\t14.00\nIT-404\t11\nIT-002\tnot-a-number\nIT-001\t15\n"

            preview = prices.preview_tsv("C-001", paste)

            self.assertEqual(["IT-001"], [row["item_id"] for row in preview["valid"]])
            self.assertEqual(["IT-404"], [row["item_id"] for row in preview["unknown"]])
            self.assertEqual(["IT-002"], [row["item_id"] for row in preview["invalid"]])
            self.assertEqual(["IT-001"], [row["item_id"] for row in preview["duplicate"]])
            self.assertEqual(
                [{"customer_code": "C-001", "item_id": "IT-001", "before": "12.50", "after": "14.00"}],
                preview["changes"],
            )
            self.assertEqual("12.50", prices.search("C-001")["rows"][0]["price"])
            with self.assertRaises(ValueError):
                prices.apply_preview_id("C-001", preview["preview_id"], "YIM")
            self.assertEqual("12.50", prices.search("C-001")["rows"][0]["price"])

            clean_preview = prices.preview_tsv("C-001", "Item ID\tPrice\nIT-001\t14.00\nIT-002\t22\n")
            applied = prices.apply_preview_id("C-001", clean_preview["preview_id"], "YIM")
            self.assertEqual(2, applied["applied"])
            self.assertEqual(["14.00", "22"], [row["price"] for row in prices.search("C-001")["rows"]])
            self.assertEqual(["price_bulk_applied", "price_bulk_applied"], [row["action"] for row in prices.activity("C-001")])

    def test_apply_requires_a_server_issued_preview_id_and_rejects_reuse(self):
        from apc_core.customer_price_module import CustomerPriceModule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = CustomerPriceModule(self.make_snapshot(root), data_dir=root / "state")
            prices.import_from_snapshot()
            preview = prices.preview_tsv("C-001", "Item ID\tPrice\nIT-001\t14\n")

            self.assertTrue(preview["preview_id"])
            self.assertEqual({"applied": 1}, prices.apply_preview_id("C-001", preview["preview_id"], "YIM"))
            with self.assertRaises(ValueError):
                prices.apply_preview_id("C-001", preview["preview_id"], "YIM")
            with self.assertRaises(ValueError):
                prices.apply_preview_id("C-001", "not-a-preview", "YIM")

    def test_loopback_api_and_apple_calm_panel_require_selected_actor_and_explicit_clean_apply(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import io
        import json
        import threading
        from apc_core.customer_price_module import CustomerPriceModule
        from apc_core.item_explorer import ItemExplorer, _customer_client_allowed, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            prices = CustomerPriceModule(source, data_dir=root / "state")
            prices.import_from_snapshot()
            items = ItemExplorer(source, data_dir=root / "state")
            handler_class = make_handler(items, {"accepted": True}, customer_price_module=prices)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                handler_class,
            )
            denied = object.__new__(handler_class)
            denied.client_address = ("100.69.141.75", 1)
            denied.path = "/customer-prices/api/staff"
            denied.wfile = io.BytesIO()
            denied_status = []
            denied.send_response = denied_status.append
            denied.send_header = lambda *_: None
            denied.end_headers = lambda: None
            handler_class.do_GET(denied)
            self.assertEqual([403], denied_status)

            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customer-prices/")
                response = connection.getresponse()
                html = response.read().decode("utf-8")
                connection.close()
                self.assertEqual(200, response.status)
                for marker in (
                    "Customer Price", "Customer Code", "Search item-price rows", "Bulk edit prices",
                    "Preview", "Apply", "textContent", "window.apcCoreActiveStaff",
                    "preview_id", "tsv.oninput", "r.before", "r.after",
                ):
                    self.assertIn(marker, html)
                self.assertNotIn("Price Type", html)
                self.assertFalse(_customer_client_allowed("100.69.141.75", False))
                self.assertFalse(_customer_client_allowed("192.168.1.246", False))
                self.assertTrue(_customer_client_allowed("192.168.1.246", True))
                items._local_store().connection.execute("UPDATE core_users SET active=0 WHERE username='YIM'")
                items._local_store().connection.commit()

                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customer-prices/api/staff")
                response = connection.getresponse()
                staff_payload = json.loads(response.read())
                connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual(["BIAS", "BON", "DERRICK", "WAT", "YA"], [row["username"] for row in staff_payload["staff"]])
                items._local_store().connection.execute("UPDATE core_users SET active=1 WHERE username='YIM'")
                items._local_store().connection.commit()

                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customer-prices/api/customers/C-001?q=anub")
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual(["IT-001"], [row["item_id"] for row in payload["rows"]])

                connection = HTTPConnection(host, port, timeout=3)
                connection.request(
                    "POST", "/customer-prices/api/customers/C-001/items/IT-001",
                    json.dumps({"price": "16", "actor": "YIM"}),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                response.read()
                connection.close()

                invalid_tsv = "Item ID\tPrice\nIT-001\t17\nIT-001\t18\n"
                connection = HTTPConnection(host, port, timeout=3)
                connection.request(
                    "POST", "/customer-prices/api/customers/C-001/paste/preview",
                    json.dumps({"tsv": invalid_tsv}),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                preview = json.loads(response.read())
                connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual(1, len(preview["duplicate"]))

                connection = HTTPConnection(host, port, timeout=3)
                connection.request(
                    "POST", "/customer-prices/api/customers/C-001/paste/apply",
                    json.dumps({"preview_id": preview["preview_id"], "actor": "YIM"}),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(400, response.status)
                self.assertEqual("16", prices.search("C-001", query="IT-001")["rows"][0]["price"])

                clean_tsv = "Item ID\tPrice\nIT-001\t17\n"
                connection = HTTPConnection(host, port, timeout=3)
                connection.request(
                    "POST", "/customer-prices/api/customers/C-001/paste/preview",
                    json.dumps({"tsv": clean_tsv}),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                clean_preview = json.loads(response.read())
                connection.close()
                self.assertEqual(200, response.status)
                connection = HTTPConnection(host, port, timeout=3)
                connection.request(
                    "POST", "/customer-prices/api/customers/C-001/paste/apply",
                    json.dumps({"preview_id": clean_preview["preview_id"], "actor": "YIM"}),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual(1, payload["applied"])
                self.assertEqual("17", prices.search("C-001", query="IT-001")["rows"][0]["price"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
