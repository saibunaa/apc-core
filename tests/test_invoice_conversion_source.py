import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apc_core.invoice_conversion_source import (
    InvoiceConversionSource,
    ReadOnlyInvoiceSourceError,
)


class InvoiceConversionSourceTests(unittest.TestCase):
    def make_source(self, root: Path, *, include_schema: bool = True) -> Path:
        source = root / "accepted.sqlite"
        connection = sqlite3.connect(source)
        try:
            if not include_schema:
                connection.execute("CREATE TABLE unrelated (value TEXT)")
                return source
            connection.execute(
                'CREATE TABLE "MainDB__ORDER" ('
                '"Order No" TEXT, "Order Date" TEXT, "Order Time" TEXT, '
                '"Cust ID" TEXT, "Shipment Date" TEXT, "AWB" TEXT)'
            )
            connection.execute(
                'CREATE TABLE "MainDB__ORDER_ITEM" ('
                '"Order No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Qty" TEXT, '
                '"Description" TEXT, "Unit Price" TEXT, "Note" TEXT, '
                '"Shipment Date" TEXT, "AWB" TEXT)'
            )
            connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
            connection.execute(
                'INSERT INTO "MainDB__ORDER" VALUES (?, ?, ?, ?, ?, ?)',
                ("ORD/2026/01", "2026-08-01", "08:15", "C/1", "2026-08-02", "AWB/REUSED"),
            )
            connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?)', ("C/1", "Customer One"))
            connection.executemany(
                'INSERT INTO "MainDB__ORDER_ITEM" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    ("ORD/2026/01", "10", "ITEM/KNOWN", "2", "Known", "12.50", "", "2026-08-02", "AWB/REUSED"),
                    ("ORD/2026/01", "20", "", "", "", "", "packing note", "", "AWB/OTHER"),
                    ("ORD/2026/01", "30", "ITEM/UNKNOWN", "1", "Unknown", "", "", "2026-08-03", "AWB/REUSED"),
                ],
            )
            connection.execute(
                'INSERT INTO "MainDB__ORDER" VALUES (?, ?, ?, ?, ?, ?)',
                ("ORD/OTHER", "2026-08-01", "09:00", "C/1", "2026-08-02", "AWB/REUSED"),
            )
            connection.execute(
                'INSERT INTO "MainDB__ORDER" VALUES (?, ?, ?, ?, ?, ?)',
                ("ORD/DIFFERENT", "2026-08-01", "09:00", "C/2", "2026-08-02", "AWB/REUSED"),
            )
            connection.commit()
        finally:
            connection.close()
        return source

    def test_read_order_preserves_source_evidence_and_hash_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_source(Path(temporary))
            original = source.read_bytes()
            lookups = []

            def current_price(customer_id, item_id):
                self.assertEqual("C/1", customer_id)
                lookups.append(item_id)
                return {"status": "FOUND", "value": "13.75"} if item_id == "ITEM/KNOWN" else None

            reader = InvoiceConversionSource(source, current_price_lookup=current_price)
            result = reader.read_order("ORD/2026/01")
            reader.close()

            self.assertEqual(hashlib.sha256(original).hexdigest(), result["source_sha256"])
            self.assertEqual("ORD/2026/01", result["order_id"])
            self.assertEqual("08:15", result["order_time_text"])
            self.assertEqual("Customer One", result["customer_name"])
            self.assertEqual(["ITEM/KNOWN", "ITEM/UNKNOWN"], lookups)
            self.assertEqual(["10", "20", "30"], [line["line_id"] for line in result["lines"]])
            self.assertEqual("2", result["lines"][0]["quantity"])
            self.assertEqual("12.50", result["lines"][0]["source_unit_price"])
            self.assertEqual({"status": "FOUND", "value": "13.75"}, result["lines"][0]["current_price"])
            self.assertTrue(result["lines"][1]["is_annotation"])
            self.assertEqual("packing note", result["lines"][1]["annotation_text"])
            self.assertEqual({"status": "UNKNOWN", "value": ""}, result["lines"][2]["current_price"])
            self.assertEqual(original, source.read_bytes())

    def test_read_order_contains_current_price_callback_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_source(Path(temporary))

            def current_price(customer_id, item_id):
                self.assertEqual("C/1", customer_id)
                if item_id == "ITEM/UNKNOWN":
                    raise RuntimeError("lookup unavailable")
                return {"status": "FOUND", "value": "13.75"}

            reader = InvoiceConversionSource(source, current_price_lookup=current_price)
            try:
                result = reader.read_order("ORD/2026/01")
            finally:
                reader.close()

            self.assertEqual("ORD/2026/01", result["order_id"])
            self.assertEqual(
                {"status": "UNKNOWN", "value": ""}, result["lines"][2]["current_price"]
            )

    def test_shipment_metadata_never_selects_first_nonblank_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = InvoiceConversionSource(self.make_source(Path(temporary)))
            result = reader.read_order("ORD/2026/01")
            reader.close()

            self.assertEqual("CONFLICTING", result["shipment_metadata"]["shipment_date"]["status"])
            self.assertEqual(["2026-08-02", "2026-08-03"], result["shipment_metadata"]["shipment_date"]["values"])
            self.assertEqual("CONFLICTING", result["shipment_metadata"]["awb"]["status"])
            self.assertEqual(["AWB/OTHER", "AWB/REUSED"], result["shipment_metadata"]["awb"]["values"])
            self.assertNotIn("value", result["shipment_metadata"]["awb"])

    def test_exact_customer_shipment_candidates_are_display_only_and_do_not_group_by_awb(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = InvoiceConversionSource(self.make_source(Path(temporary)))
            result = reader.discover_legacy_candidates("C/1", "2026-08-02")
            reader.close()

            self.assertEqual(["ORD/2026/01", "ORD/OTHER"], [row["order_id"] for row in result["candidates"]])
            self.assertEqual(["AWB/REUSED", "AWB/REUSED"], [row["awb"] for row in result["candidates"]])
            self.assertEqual("display_legacy_candidate_set", result["kind"])
            self.assertNotIn("selected", result)
            self.assertNotIn("group", result)

    def test_missing_schema_malformed_input_and_bounds_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ReadOnlyInvoiceSourceError):
                InvoiceConversionSource(self.make_source(root, include_schema=False))
            reader = InvoiceConversionSource(self.make_source(root))
            self.assertIsNone(reader.read_order(None))
            self.assertEqual([], reader.discover_legacy_candidates("C/1", None)["candidates"])
            bounded = reader.discover_legacy_candidates("C/1", "2026-08-02", limit=9999)
            self.assertEqual(250, bounded["limit"])
            self.assertEqual(2, len(bounded["candidates"]))
            with self.assertRaises(ValueError):
                reader.discover_legacy_candidates("C/1", "2026-08-02", limit="bad")
            reader.close()

    def test_from_open_descriptor_is_pinned_when_pathname_is_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            descriptor = __import__("os").open(source, __import__("os").O_RDONLY)
            replacement = root / "replacement.sqlite"
            self.make_source(root / "replacement-root") if False else None
            connection = sqlite3.connect(replacement)
            connection.execute('CREATE TABLE "MainDB__ORDER" ("Order No" TEXT, "Order Date" TEXT, "Cust ID" TEXT)')
            connection.execute('CREATE TABLE "MainDB__ORDER_ITEM" ("Order No" TEXT, "Line No" TEXT, "Item ID" TEXT, "Qty" TEXT)')
            connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
            connection.execute('INSERT INTO "MainDB__ORDER" VALUES (?,?,?)', ("REPLACED", "2026-01-01", "C-X"))
            connection.commit(); connection.close()
            held_original = root / "held-original.sqlite"
            __import__("os").replace(source, held_original)
            __import__("os").replace(replacement, source)
            try:
                reader = InvoiceConversionSource.from_open_descriptor(descriptor, source)
                self.assertEqual("ORD/2026/01", reader.read_order("ORD/2026/01")["order_id"])
                self.assertIsNone(reader.read_order("REPLACED"))
                reader.close()
            finally:
                __import__("os").close(descriptor)

    def test_initialization_failure_closes_opened_source_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_source(Path(temporary), include_schema=False)
            with patch("apc_core.invoice_conversion_source.os.close", wraps=__import__("os").close) as close:
                with self.assertRaises(ReadOnlyInvoiceSourceError):
                    InvoiceConversionSource(source)
            close.assert_called_once()
            self.assertIsInstance(close.call_args.args[0], int)

    def test_read_order_rejects_orders_with_more_than_maximum_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_source(Path(temporary))
            connection = sqlite3.connect(source)
            try:
                connection.executemany(
                    'INSERT INTO "MainDB__ORDER_ITEM" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [
                        ("ORD/2026/01", str(index + 100), f"ITEM/{index}", "1", "Extra", "", "", "", "")
                        for index in range(248)
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            reader = InvoiceConversionSource(source)
            try:
                with self.assertRaises(ReadOnlyInvoiceSourceError):
                    reader.read_order("ORD/2026/01")
            finally:
                reader.close()

    def test_adapter_surface_has_no_draft_or_legacy_awb_dependency(self):
        module_path = Path(__file__).parents[1] / "apc_core" / "invoice_conversion_source.py"
        source = module_path.read_text(encoding="utf-8").casefold()
        self.assertNotIn("legacy_awb", source)
        self.assertNotIn("create_draft", source)
        self.assertNotIn("persist", source)
        self.assertNotIn("insert into", source)
        self.assertNotIn("update ", source)


if __name__ == "__main__":
    unittest.main()
