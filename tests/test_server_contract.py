import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apc_core.server import RuntimeContractError, load_accepted_runtime
from apc_core.snapshot_contract import certify_snapshot


class ServerContractTests(unittest.TestCase):
    def make_snapshot(self, path: Path, item_id: str) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            'CREATE TABLE "MainDB__ITEM" ('
            '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, '
            '"Type" TEXT, "Family" TEXT)'
        )
        connection.execute(
            'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?)',
            (item_id, "Item", "สินค้า", "Fish", "Tropical"),
        )
        connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
        connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?)', ("C-001", "Customer"))
        connection.execute('CREATE TABLE "MainDB__CUST_PRC" ("Cust ID" TEXT, "Item ID" TEXT, "Price" TEXT)')
        connection.execute('INSERT INTO "MainDB__CUST_PRC" VALUES (?, ?, ?)', ("C-001", item_id, "12"))
        connection.commit()
        connection.close()

    def certify(self, root: Path) -> tuple[Path, Path, dict]:
        source = root / "source.sqlite"
        self.make_snapshot(source, "IT-001")
        manifest_path = root / "state" / "accepted_snapshot.json"
        return source, manifest_path, certify_snapshot(source, manifest_path, "2026-08-25T13:00:00Z")

    def test_customer_runtime_requires_customer_ready_manifest_and_reconciles_before_serving(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "customer-source.sqlite"
            self.make_snapshot(source, "IT-001")
            manifest_path = root / "state" / "accepted_snapshot.json"
            manifest = certify_snapshot(source, manifest_path, "2026-08-25T13:00:00Z", customer_ready=True)

            from apc_core import server
            self.assertTrue(hasattr(server, "load_accepted_customer_runtime"))
            item_explorer, customer_explorer, loaded = server.load_accepted_customer_runtime(manifest_path, data_dir=root / "core-state")

            self.assertEqual(manifest, loaded)
            self.assertEqual("IT-001", item_explorer.search()["items"][0]["item_id"])
            self.assertEqual("C-001", customer_explorer.search()["customers"][0]["customer_id"])

    def test_customer_price_runtime_uses_the_validated_accepted_descriptor_and_imports_only_snapshot_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "price-source.sqlite"
            self.make_snapshot(source, "IT-001")
            manifest_path = root / "state" / "accepted_snapshot.json"
            manifest = certify_snapshot(source, manifest_path, "2026-08-25T13:00:00Z", customer_ready=True)

            from apc_core import server
            self.assertTrue(hasattr(server, "load_accepted_customer_price_runtime"))
            items, customers, prices, loaded = server.load_accepted_customer_price_runtime(manifest_path, data_dir=root / "core-state")

            self.assertEqual(manifest, loaded)
            self.assertEqual("IT-001", items.search()["items"][0]["item_id"])
            self.assertEqual("C-001", customers.search()["customers"][0]["customer_id"])
            self.assertEqual("12", prices.search("C-001")["rows"][0]["price"])

    def test_customer_price_order_runtime_constructs_order_explorer_from_the_same_validated_descriptor(self):
        """Order runtime has no caller source path and receives only the accepted descriptor/path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "accepted.sqlite"
            accepted.write_bytes(b"accepted artifact")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {
                "customer_ready": True,
                "required_customer_columns": ["Cust ID", "Name"],
                "accepted_artifact_sha256": "accepted-hash",
                "capabilities": {name: {"ready": True, "status": "verified"} for name in ("customers", "customer_prices", "orders")},
            }
            items = Mock()
            customers = Mock()
            customers.reconciliation_status.return_value = {"source_sha256": "accepted-hash", "state": "ready"}
            prices = Mock()
            prices.reconciliation_status.return_value = {"state": "ready"}
            orders = Mock()
            from apc_core import server
            self.assertTrue(hasattr(server, "load_accepted_customer_price_order_runtime"))
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                 patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items) as item_factory, \
                 patch.object(server, "CustomerExplorer", return_value=customers), \
                 patch.object(server.CustomerPriceModule, "from_open_descriptor", return_value=prices) as price_factory, \
                 patch.object(server.OrderExplorer, "from_open_descriptor", return_value=orders) as order_factory:
                loaded_items, loaded_customers, loaded_prices, loaded_orders, loaded_awb, loaded_manifest = server.load_accepted_customer_price_order_runtime(root / "substituted.sqlite")

            self.assertEqual((items, customers, prices, orders, manifest), (loaded_items, loaded_customers, loaded_prices, loaded_orders, loaded_manifest))
            item_factory.assert_called_once_with(descriptor, accepted, data_dir=None)
            price_factory.assert_called_once_with(descriptor, accepted, data_dir=None)
            order_factory.assert_called_once_with(descriptor, accepted)

    def test_invoice_runtime_constructs_source_from_validated_open_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); accepted = root / "accepted.sqlite"; accepted.write_bytes(b"accepted")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {"accepted_artifact_sha256": "a" * 64, "capabilities": {}}
            items, source, service = Mock(), Mock(), Mock()
            source.source_sha256 = "a" * 64
            from apc_core import server
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                 patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items), \
                 patch.object(server.InvoiceConversionSource, "from_open_descriptor", return_value=source) as source_factory, \
                 patch.object(server, "InvoiceDraftService", return_value=service):
                result = server.load_accepted_customer_price_order_runtime(root / "ignored", data_dir=root / "state", with_invoice_drafts=True)
            self.assertIs(source, result[5])
            source_factory.assert_called_once_with(descriptor, accepted, current_price_lookup=None)

    def test_customer_price_order_runtime_does_not_construct_awb_explorer_when_manifest_declares_awb_unavailable(self):
        """A valid source AWB table cannot override an unavailable accepted capability."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "accepted.sqlite"
            accepted.write_bytes(b"accepted artifact")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {
                "customer_ready": True,
                "required_customer_columns": ["Cust ID", "Name"],
                "accepted_artifact_sha256": "accepted-hash",
                "capabilities": {
                    "customers": {"ready": True, "status": "verified"},
                    "customer_prices": {"ready": True, "status": "verified"},
                    "orders": {"ready": True, "status": "verified"},
                    "awb_shipments": {"ready": False, "status": "unavailable"},
                },
            }
            items = Mock()
            customers = Mock()
            customers.reconciliation_status.return_value = {"source_sha256": "accepted-hash", "state": "ready"}
            prices = Mock()
            prices.reconciliation_status.return_value = {"state": "ready"}
            orders = Mock()
            from apc_core import server
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                 patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items), \
                 patch.object(server, "CustomerExplorer", return_value=customers), \
                 patch.object(server.CustomerPriceModule, "from_open_descriptor", return_value=prices), \
                 patch.object(server.OrderExplorer, "from_open_descriptor", return_value=orders), \
                 patch.object(server.AWBExplorer, "from_open_descriptor", return_value=Mock()) as awb_factory:
                _, _, _, _, loaded_awb, loaded_manifest = server.load_accepted_customer_price_order_runtime(root / "substituted.sqlite")

            self.assertIsNone(loaded_awb)
            self.assertEqual(manifest, loaded_manifest)
            awb_factory.assert_not_called()

    def test_customer_price_order_runtime_fails_closed_for_missing_or_malformed_awb_capability(self):
        """Legacy/malformed capability data leaves Orders and other valid modules available."""
        capability_cases = (
            None,
            {},
            {"awb_shipments": None},
            {"awb_shipments": {"ready": True}},
            {"awb_shipments": {"ready": 1, "status": "verified"}},
            {"awb_shipments": {"ready": True, "status": "unavailable"}},
        )
        for capabilities in capability_cases:
            with self.subTest(capabilities=capabilities), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                accepted = root / "accepted.sqlite"
                accepted.write_bytes(b"accepted artifact")
                descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
                manifest = {
                    "customer_ready": True,
                    "required_customer_columns": ["Cust ID", "Name"],
                    "accepted_artifact_sha256": "accepted-hash",
                    "capabilities": {name: {"ready": True, "status": "verified"} for name in ("customers", "customer_prices", "orders")},
                }
                if capabilities is not None:
                    manifest["capabilities"]["awb_shipments"] = capabilities.get("awb_shipments") if type(capabilities) is dict else None
                items = Mock()
                customers = Mock()
                customers.reconciliation_status.return_value = {"source_sha256": "accepted-hash", "state": "ready"}
                prices = Mock()
                prices.reconciliation_status.return_value = {"state": "ready"}
                orders = Mock()
                from apc_core import server
                with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                     patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items), \
                     patch.object(server, "CustomerExplorer", return_value=customers), \
                     patch.object(server.CustomerPriceModule, "from_open_descriptor", return_value=prices), \
                     patch.object(server.OrderExplorer, "from_open_descriptor", return_value=orders), \
                     patch.object(server.AWBExplorer, "from_open_descriptor", return_value=Mock()) as awb_factory:
                    loaded_items, loaded_customers, loaded_prices, loaded_orders, loaded_awb, loaded_manifest = server.load_accepted_customer_price_order_runtime(root / "substituted.sqlite")

                self.assertEqual((items, customers, prices, orders, manifest), (loaded_items, loaded_customers, loaded_prices, loaded_orders, loaded_manifest))
                self.assertIsNone(loaded_awb)
                awb_factory.assert_not_called()

    def test_customer_price_order_runtime_keeps_awb_source_schema_validation_after_verified_capability(self):
        """Manifest capability cannot override AWBExplorer's source-contract validation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "accepted.sqlite"
            accepted.write_bytes(b"accepted artifact")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {
                "customer_ready": True,
                "required_customer_columns": ["Cust ID", "Name"],
                "accepted_artifact_sha256": "accepted-hash",
                "capabilities": {
                    "customers": {"ready": True, "status": "verified"},
                    "customer_prices": {"ready": True, "status": "verified"},
                    "orders": {"ready": True, "status": "verified"},
                    "awb_shipments": {"ready": True, "status": "verified"},
                },
            }
            items = Mock()
            customers = Mock()
            customers.reconciliation_status.return_value = {"source_sha256": "accepted-hash", "state": "ready"}
            prices = Mock()
            prices.reconciliation_status.return_value = {"state": "ready"}
            orders = Mock()
            from apc_core import server
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                 patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items), \
                 patch.object(server, "CustomerExplorer", return_value=customers), \
                 patch.object(server.CustomerPriceModule, "from_open_descriptor", return_value=prices), \
                 patch.object(server.OrderExplorer, "from_open_descriptor", return_value=orders), \
                 patch.object(server.AWBExplorer, "from_open_descriptor", side_effect=server.AWBSourceContractError) as awb_factory:
                _, _, _, _, loaded_awb, _ = server.load_accepted_customer_price_order_runtime(root / "substituted.sqlite")

            self.assertIsNone(loaded_awb)
            awb_factory.assert_called_once_with(descriptor, accepted)

    def test_customer_price_order_runtime_keeps_items_available_when_optional_capabilities_are_missing(self):
        """Legacy customer_ready metadata cannot construct an optional module."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "accepted.sqlite"
            accepted.write_bytes(b"accepted artifact")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {
                "customer_ready": True,
                "required_customer_columns": ["Cust ID", "Name"],
                "accepted_artifact_sha256": "accepted-hash",
            }
            items = Mock()
            from apc_core import server
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                 patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items) as item_factory, \
                 patch.object(server, "CustomerExplorer", side_effect=AssertionError("customers must not construct")) as customer_factory, \
                 patch.object(server.CustomerPriceModule, "from_open_descriptor", side_effect=AssertionError("prices must not construct")) as price_factory, \
                 patch.object(server.OrderExplorer, "from_open_descriptor", side_effect=AssertionError("orders must not construct")) as order_factory, \
                 patch.object(server.AWBExplorer, "from_open_descriptor", side_effect=AssertionError("awb must not construct")) as awb_factory:
                loaded_items, loaded_customers, loaded_prices, loaded_orders, loaded_awb, loaded_manifest = server.load_accepted_customer_price_order_runtime(root / "substituted.sqlite")

            self.assertIs(items, loaded_items)
            self.assertEqual((None, None, None, None, manifest), (loaded_customers, loaded_prices, loaded_orders, loaded_awb, loaded_manifest))
            item_factory.assert_called_once_with(descriptor, accepted, data_dir=None)
            customer_factory.assert_not_called()
            price_factory.assert_not_called()
            order_factory.assert_not_called()
            awb_factory.assert_not_called()

    def test_optional_capabilities_are_independently_unavailable_without_constructing_that_module(self):
        positions = {"customers": 1, "customer_prices": 2, "orders": 3}
        for unavailable, position in positions.items():
            with self.subTest(unavailable=unavailable), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                accepted = root / "accepted.sqlite"
                accepted.write_bytes(b"accepted artifact")
                descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
                manifest = {"accepted_artifact_sha256": "accepted-hash", "capabilities": {
                    name: {"ready": name != unavailable, "status": "verified" if name != unavailable else "unavailable"}
                    for name in positions
                }}
                items, customers, prices, orders = Mock(), Mock(), Mock(), Mock()
                customers.reconciliation_status.return_value = {"source_sha256": "accepted-hash", "state": "ready"}
                prices.reconciliation_status.return_value = {"state": "ready"}
                from apc_core import server
                with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                     patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items), \
                     patch.object(server, "CustomerExplorer", return_value=customers) as customer_factory, \
                     patch.object(server.CustomerPriceModule, "from_open_descriptor", return_value=prices) as price_factory, \
                     patch.object(server.OrderExplorer, "from_open_descriptor", return_value=orders) as order_factory:
                    result = server.load_accepted_customer_price_order_runtime(root / "substituted.sqlite")

                self.assertIs(items, result[0])
                self.assertIsNone(result[position])
                {"customers": customer_factory, "customer_prices": price_factory, "orders": order_factory}[unavailable].assert_not_called()

    def test_verified_optional_source_failure_is_isolated_from_items_and_other_optional_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "accepted.sqlite"
            accepted.write_bytes(b"accepted artifact")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {"accepted_artifact_sha256": "accepted-hash", "capabilities": {
                name: {"ready": True, "status": "verified"} for name in ("customers", "customer_prices", "orders")
            }}
            items, prices, orders = Mock(), Mock(), Mock()
            prices.reconciliation_status.return_value = {"state": "ready"}
            from apc_core import server
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                 patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items), \
                 patch.object(server, "CustomerExplorer", side_effect=ValueError("invalid customer source")), \
                 patch.object(server.CustomerPriceModule, "from_open_descriptor", return_value=prices), \
                 patch.object(server.OrderExplorer, "from_open_descriptor", return_value=orders):
                result = server.load_accepted_customer_price_order_runtime(root / "substituted.sqlite")

            self.assertIs(items, result[0])
            self.assertIsNone(result[1])
            self.assertIs(prices, result[2])
            self.assertIs(orders, result[3])

    def test_main_passes_accepted_order_explorer_to_handler_and_closes_it_with_core_modules(self):
        """Production composition exposes Order Forms only through the accepted runtime loader."""
        import sys
        from apc_core import server
        items = Mock()
        customers = Mock()
        prices = Mock()
        orders = Mock()
        manifest = {"accepted": True, "accepted_artifact_sha256": "a" * 64}
        handler = object()
        created_servers = []

        class FakeServer:
            def __init__(self, address, supplied_handler):
                self.address = address
                self.supplied_handler = supplied_handler
                created_servers.append(self)

            def serve_forever(self):
                return None

        with patch.object(sys, "argv", ["server", "--manifest", "accepted.json"]), \
             patch.object(server, "load_accepted_customer_price_order_runtime", return_value=(items, customers, prices, orders, None, None, None, manifest)) as loader, \
             patch.object(server, "make_handler", return_value=handler) as handler_factory, \
             patch.object(server, "ThreadingHTTPServer", FakeServer):
            server.main()

        loader.assert_called_once_with(Path("accepted.json"), data_dir=None, with_invoice_drafts=True)
        handler_factory.assert_called_once_with(
            items, manifest, customers, prices, orders, None,
            invoice_source=None, invoice_draft_service=None, accepted_snapshot_sha256=manifest["accepted_artifact_sha256"],
            customer_lan_ingress=False, allowed_mutation_origins=None,
            recovery_authorizer=None, recovery_service=None, recovery_maintenance=None,
        )
        self.assertEqual(1, len(created_servers))
        self.assertEqual(("127.0.0.1", 8769), created_servers[0].address)
        for module in (orders, prices, customers, items):
            module.close.assert_called_once_with()

    def test_customer_price_runtime_startup_is_a_noop_for_an_already_reconciled_accepted_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "price-source.sqlite"
            self.make_snapshot(source, "IT-001")
            connection = sqlite3.connect(source)
            connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?)', ("C-DUP", "Duplicate"))
            connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?)', ("C-DUP", "Duplicate"))
            connection.execute('INSERT INTO "MainDB__CUST_PRC" VALUES (?, ?, ?)', ("C-001", "IT-UNKNOWN", "12"))
            connection.commit()
            connection.close()
            manifest_path = root / "state" / "accepted_snapshot.json"
            certify_snapshot(source, manifest_path, "2026-08-25T13:00:00Z", customer_ready=True)
            data_dir = root / "core-state"

            from apc_core import server
            items, customers, prices, _ = server.load_accepted_customer_price_runtime(manifest_path, data_dir=data_dir)
            self.assertEqual(1, customers.search()["total"])
            with self.assertRaises(ValueError):
                customers.edit("C-001", {}, None)
            items.close(); customers.close(); prices.close()
            first = sqlite3.connect(data_dir / "apc_core.sqlite")
            first_counts = {
                table: first.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("customer_quarantine", "customer_price_quarantine")
            }
            first.close()

            items, customers, prices, _ = server.load_accepted_customer_price_runtime(manifest_path, data_dir=data_dir)
            self.assertEqual(1, customers.search()["total"])
            with self.assertRaises(ValueError):
                customers.edit("C-001", {}, None)
            items.close(); customers.close(); prices.close()
            second = sqlite3.connect(data_dir / "apc_core.sqlite")
            second_counts = {
                table: second.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("customer_quarantine", "customer_price_quarantine")
            }
            second.close()

            self.assertEqual({"customer_quarantine": 2, "customer_price_quarantine": 1}, first_counts)
            self.assertEqual(first_counts, second_counts)

    def test_customer_price_runtime_adopts_verified_legacy_artifact_without_reimporting_quarantine_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "price-source.sqlite"
            self.make_snapshot(source, "IT-001")
            connection = sqlite3.connect(source)
            connection.execute('INSERT INTO "MainDB__CUST_PRC" VALUES (?, ?, ?)', ("C-001", "IT-UNKNOWN", "12"))
            connection.commit()
            connection.close()
            manifest_path = root / "state" / "accepted_snapshot.json"
            certify_snapshot(source, manifest_path, "2026-08-25T13:00:00Z", customer_ready=True)
            data_dir = root / "core-state"

            from apc_core import server
            items, customers, prices, _ = server.load_accepted_customer_price_runtime(manifest_path, data_dir=data_dir)
            items.close(); customers.close(); prices.close()
            with sqlite3.connect(data_dir / "apc_core.sqlite") as legacy:
                legacy.execute("DELETE FROM customer_price_reconciliation_state")
                before = legacy.execute("SELECT count(*) FROM customer_price_quarantine").fetchone()[0]

            items, customers, prices, _ = server.load_accepted_customer_price_runtime(manifest_path, data_dir=data_dir)
            self.assertEqual("ready", prices.reconciliation_status()["state"])
            items.close(); customers.close(); prices.close()
            with sqlite3.connect(data_dir / "apc_core.sqlite") as upgraded:
                after = upgraded.execute("SELECT count(*) FROM customer_price_quarantine").fetchone()[0]
                state = upgraded.execute("SELECT source_artifact_sha256 FROM customer_price_reconciliation_state WHERE singleton=1").fetchone()

            self.assertEqual(before, after)
            self.assertIsNotNone(state)

    def test_customer_runtime_rejects_forged_customer_ready_metadata_over_wrong_customer_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "wrong-customer-schema.sqlite"
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?)', ("IT-001", "Item", "สินค้า", "Fish", "Tropical"))
            connection.execute('CREATE TABLE "MainDB__CUST" ("wrong" TEXT)')
            connection.commit(); connection.close()
            manifest_path = root / "state" / "accepted_snapshot.json"
            manifest = certify_snapshot(source, manifest_path, "2026-08-25T13:00:00Z")
            manifest["customer_ready"] = True
            manifest["required_customer_columns"] = ["Cust ID", "Name"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            from apc_core import server
            with self.assertRaises(RuntimeContractError):
                server.load_accepted_customer_runtime(manifest_path, data_dir=root / "core-state")

    def test_customer_runtime_rejects_item_only_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest_path, _ = self.certify(root)
            from apc_core import server
            self.assertTrue(hasattr(server, "load_accepted_customer_runtime"))
            with self.assertRaises(RuntimeContractError):
                server.load_accepted_customer_runtime(manifest_path, data_dir=root / "core-state")

    def test_runtime_uses_only_accepted_artifact_not_caller_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, manifest_path, manifest = self.certify(root)
            replacement = root / "replacement.sqlite"
            self.make_snapshot(replacement, "IT-REPLACED")
            source.write_bytes(replacement.read_bytes())

            explorer, loaded_manifest = load_accepted_runtime(manifest_path, data_dir=root / "core-state")

            self.assertEqual(manifest, loaded_manifest)
            self.assertEqual("IT-001", explorer.search()["items"][0]["item_id"])

    def test_runtime_keeps_serving_loaded_accepted_sqlite_after_artifact_path_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest_path, manifest = self.certify(root)
            explorer, _ = load_accepted_runtime(manifest_path, data_dir=root / "core-state")
            accepted = Path(manifest["accepted_artifact_path"])
            replacement = root / "replacement.sqlite"
            self.make_snapshot(replacement, "IT-ALTERED")

            os.replace(replacement, accepted)

            result = explorer.search()
            self.assertEqual(1, result["total"])
            self.assertEqual("IT-001", result["items"][0]["item_id"])
            self.assertNotIn("IT-ALTERED", [item["item_id"] for item in result["items"]])

    def test_rejects_tampered_accepted_artifact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest_path, manifest = self.certify(root)
            accepted = Path(manifest["accepted_artifact_path"])
            accepted.chmod(0o644)
            with accepted.open("ab") as artifact:
                artifact.write(b"tampered")

            with self.assertRaises(RuntimeContractError):
                load_accepted_runtime(manifest_path)

    def test_rejects_malformed_missing_and_tampered_manifests_as_runtime_contract_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest_path, _ = self.certify(root)
            for content in ("{", json.dumps({"accepted": True})):
                manifest_path.write_text(content, encoding="utf-8")
                with self.assertRaises(RuntimeContractError):
                    load_accepted_runtime(manifest_path)
            manifest_path.unlink()
            with self.assertRaises(RuntimeContractError):
                load_accepted_runtime(manifest_path)

    def test_rejects_manifest_path_or_hash_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest_path, manifest = self.certify(root)
            manifest["accepted_artifact_path"] = str(root / "elsewhere.sqlite")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(RuntimeContractError):
                load_accepted_runtime(manifest_path)

    def test_server_cli_refuses_non_loopback_host(self):
        import sys
        from unittest.mock import patch
        from apc_core import server
        with patch.object(sys, "argv", ["server", "--manifest", "missing.json", "--host", "0.0.0.0"]):
            with self.assertRaises(SystemExit):
                server.main()

    def test_server_cli_allows_container_ingress_only_with_explicit_flag(self):
        import sys
        from unittest.mock import patch
        from apc_core import server
        with patch.dict(os.environ, {"APC_CORE_ALLOWED_MUTATION_ORIGINS": "http://192.168.1.246"}, clear=True):
            with patch.object(sys, "argv", ["server", "--manifest", "missing.json", "--host", "0.0.0.0", "--container-ingress"]):
                with self.assertRaises(RuntimeContractError):
                    server.main()

    def test_rejects_server_source_that_does_not_match_accepted_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted_source = root / "accepted.sqlite"
            substituted_source = root / "substituted.sqlite"
            self.make_snapshot(accepted_source, "IT-001")
            self.make_snapshot(substituted_source, "IT-002")
            manifest_path = root / "state" / "accepted_snapshot.json"
            certify_snapshot(accepted_source, manifest_path, "2026-08-25T13:00:00Z")

            with self.assertRaises(TypeError):
                load_accepted_runtime(substituted_source, manifest_path)
    def test_server_cli_refuses_recovery_test_pin_with_container_ingress(self):
        import sys
        from unittest.mock import patch
        from apc_core import server
        with patch.dict(os.environ, {"APC_CORE_RECOVERY_TEST_PIN": "123456", "APC_CORE_DATA_DIR": "/tmp/core-test"}):
            with patch.object(sys, "argv", ["server", "--manifest", "missing.json", "--host", "0.0.0.0", "--container-ingress"]):
                with self.assertRaises(SystemExit):
                    server.main()

    def test_invoice_draft_dependencies_are_optional_and_use_only_the_validated_accepted_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "accepted.sqlite"
            accepted.write_bytes(b"accepted artifact")
            descriptor = os.open(accepted, os.O_RDONLY | os.O_NOFOLLOW)
            manifest = {"accepted_artifact_sha256": "accepted-hash"}
            items, source, service = Mock(), Mock(), Mock()
            source.source_sha256 = "accepted-hash"
            from apc_core import server
            with patch.object(server, "_read_accepted_manifest", return_value=(descriptor, accepted, manifest)), \
                 patch.object(server.ItemExplorer, "from_open_descriptor", return_value=items) as item_factory, \
                 patch.object(server.InvoiceConversionSource, "from_open_descriptor", return_value=source) as source_factory, \
                 patch.object(server, "InvoiceDraftStore", return_value=Mock()) as store_factory, \
                 patch.object(server, "InvoiceDraftService", return_value=service):
                result = server.load_accepted_customer_price_order_runtime(root / "ignored.json", data_dir=root / "state", with_invoice_drafts=True)
            self.assertEqual((items, None, None, None, None, source, service, manifest), result)
            item_factory.assert_called_once_with(descriptor, accepted, data_dir=root / "state")
            source_factory.assert_called_once_with(descriptor, accepted, current_price_lookup=None)
            store_factory.assert_called_once_with(root / "state")

        from apc_core import server
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeContractError):
                server.allowed_mutation_origins(container_ingress=True)
        with patch.dict(os.environ, {"APC_CORE_ALLOWED_MUTATION_ORIGINS": "http://192.168.1.246"}, clear=True):
            self.assertEqual(frozenset({"http://192.168.1.246"}), server.allowed_mutation_origins(container_ingress=True))

    def test_recovery_test_mode_is_disabled_without_a_pin_and_local_only_with_an_explicit_pin(self):
        from unittest.mock import patch
        from apc_core import server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual((None, None), server.recovery_test_mode(data_dir=root))
            with patch.dict(os.environ, {"APC_CORE_RECOVERY_TEST_PIN": "123456"}, clear=True):
                authorizer, service = server.recovery_test_mode(data_dir=root)
            self.assertTrue(authorizer.is_authorized(authorizer.authenticate(pin="123456", client_id="127.0.0.1")))
            self.assertEqual([], service.accepted_snapshots())


if __name__ == "__main__":
    unittest.main()
