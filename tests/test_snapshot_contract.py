import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apc_core.snapshot_contract import SnapshotContractError, certify_snapshot


class SnapshotContractTests(unittest.TestCase):
    def make_snapshot(
        self, root: Path, name: str = "latest.sqlite", include_item_table: bool = True, item_id: str = "IT-001"
    ) -> Path:
        source = root / name
        connection = sqlite3.connect(source)
        if include_item_table:
            connection.execute(
                'CREATE TABLE "MainDB__ITEM" ('
                '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, '
                '"Type" TEXT, "Family" TEXT)'
            )
            connection.execute(
                'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?)',
                (item_id, "Sample item", "สินค้าตัวอย่าง", "Fish", "Tropical"),
            )
        connection.commit()
        connection.close()
        return source

    def test_certifies_a_copied_accepted_artifact_and_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            output = root / "state" / "accepted_snapshot.json"

            manifest = certify_snapshot(source, output, generated_at="2026-08-25T13:00:00Z")

            accepted_path = Path(manifest["accepted_artifact_path"])
            self.assertTrue(manifest["accepted"])
            self.assertEqual("read_only_item_explorer", manifest["scope"])
            self.assertEqual(source_hash, manifest["source_sha256"])
            self.assertEqual(accepted_path, output.parent / f"accepted_snapshot-{source_hash}.sqlite")
            self.assertEqual(source_hash, manifest["accepted_artifact_sha256"])
            self.assertEqual(source.read_bytes(), accepted_path.read_bytes())
            self.assertEqual(1, manifest["item_count"])
            self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(manifest, json.loads(output.read_text(encoding="utf-8")))

    def test_certification_opens_uri_special_source_path_readonly_and_pins_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root, name="items #? %.sqlite")
            output = root / "state" / "accepted_snapshot.json"

            manifest = certify_snapshot(source, output, generated_at="2026-08-25T13:00:00Z")

            self.assertEqual(source.read_bytes(), Path(manifest["accepted_artifact_path"]).read_bytes())

    def test_certification_hash_remains_bound_to_accepted_copy_after_source_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root, name="source.sqlite")
            replacement = self.make_snapshot(root, name="replacement.sqlite", item_id="IT-REPLACED")
            output = root / "state" / "accepted_snapshot.json"

            manifest = certify_snapshot(source, output, generated_at="2026-08-25T13:00:00Z")
            source.write_bytes(replacement.read_bytes())

            accepted = Path(manifest["accepted_artifact_path"])
            self.assertEqual(hashlib.sha256(accepted.read_bytes()).hexdigest(), manifest["accepted_artifact_sha256"])
            self.assertNotEqual(hashlib.sha256(source.read_bytes()).hexdigest(), manifest["accepted_artifact_sha256"])

    def test_stale_part_does_not_block_unique_atomic_manifest_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            output = root / "state" / "accepted_snapshot.json"
            output.parent.mkdir()
            stale_part = output.with_name(output.name + ".part")
            stale_part.write_text("stale", encoding="utf-8")

            manifest = certify_snapshot(source, output, generated_at="2026-08-25T13:00:00Z")

            self.assertEqual(manifest, json.loads(output.read_text(encoding="utf-8")))
            self.assertTrue(stale_part.exists())
            self.assertEqual("stale", stale_part.read_text(encoding="utf-8"))

    def test_certification_is_idempotent_for_the_same_accepted_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            output = root / "state" / "accepted_snapshot.json"
            first = certify_snapshot(source, output, generated_at="2026-08-25T13:00:00Z")
            second = certify_snapshot(source, output, generated_at="2026-08-25T14:00:00Z")
            self.assertEqual(first["accepted_artifact_path"], second["accepted_artifact_path"])
            self.assertTrue(Path(second["accepted_artifact_path"]).is_file())

    def test_rejects_snapshot_missing_item_table_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root, include_item_table=False)
            output = root / "state" / "accepted_snapshot.json"

            with self.assertRaises(SnapshotContractError):
                certify_snapshot(source, output, generated_at="2026-08-25T13:00:00Z")

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
