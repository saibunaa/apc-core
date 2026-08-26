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
        connection.commit()
        connection.close()

    def certify(self, root: Path) -> tuple[Path, Path, dict]:
        source = root / "source.sqlite"
        self.make_snapshot(source, "IT-001")
        manifest_path = root / "state" / "accepted_snapshot.json"
        return source, manifest_path, certify_snapshot(source, manifest_path, "2026-08-25T13:00:00Z")

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


if __name__ == "__main__":
    unittest.main()
