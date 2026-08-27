import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
