import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apc_core.item_explorer import EDITABLE_FIELDS, ItemExplorer


class CoreOwnedItemLifecycleTests(unittest.TestCase):
    def make_snapshot(self, root: Path, rows=None) -> Path:
        source = root / "accepted.sqlite"
        connection = sqlite3.connect(source)
        connection.execute(
            'CREATE TABLE "MainDB__ITEM" ('
            '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, '
            '"Type" TEXT, "Family" TEXT, "Price EU" REAL)'
        )
        connection.executemany(
            'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?, ?)',
            rows or [("IT-001", "Neon tetra", "นีออน", "Fish", "Tropical", 99.0)],
        )
        connection.commit()
        connection.close()
        return source

    def test_00_core_store_exposes_a_canonical_item_record_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = ItemExplorer(self.make_snapshot(Path(tmp)), data_dir=Path(tmp) / "state")
            self.assertTrue(hasattr(explorer._local_store(), "canonical_for"))

    def test_backfill_creates_core_canonical_record_with_provenance_and_never_overwrites_core_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            original_bytes = source.read_bytes()
            explorer = ItemExplorer(source, data_dir=root / "state")

            first = explorer.backfill_from_snapshot()
            self.assertEqual(1, first["accepted"])
            canonical = explorer._local_store().canonical_for("IT-001")
            self.assertEqual("Neon tetra", canonical["description"])
            self.assertEqual("IT-001", canonical["source_item_id"])
            self.assertEqual(hashlib.sha256(original_bytes).hexdigest(), canonical["source_artifact_sha256"])
            self.assertFalse(canonical["core_created"])
            self.assertFalse(canonical["archived"])

            explorer.edit("IT-001", {"description": "Core edited"}, "YIM")
            second = explorer.backfill_from_snapshot()
            self.assertEqual(1, second["accepted"])
            self.assertEqual("Core edited", explorer.search()["items"][0]["description"])
            self.assertEqual(original_bytes, source.read_bytes())

    def test_duplicate_is_unsaved_until_explicit_create_and_create_rejects_existing_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "state")
            explorer.backfill_from_snapshot()
            activity_before = explorer.activity_count()

            draft = explorer.duplicate("IT-001", "YIM")
            self.assertEqual("IT-001", draft["original_item_id"])
            self.assertTrue(draft["core_created"])
            self.assertIsNone(explorer._local_store().canonical_for(draft["item_id"]))
            self.assertEqual(activity_before, explorer.activity_count())
            self.assertEqual([], explorer.search(item_id_prefix=draft["item_id"])["items"])

            created = explorer.create(draft, "YIM")
            self.assertEqual(draft["item_id"], created["item_id"])
            self.assertTrue(created["core_created"])
            self.assertEqual(1, explorer.activity_count() - activity_before)
            with self.assertRaises(ValueError):
                explorer.create({**draft, "description": "again"}, "YIM")

    def test_archive_hides_source_and_core_created_items_without_destructive_delete_and_audits_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "state")
            explorer.backfill_from_snapshot()
            created = explorer.create({
                "item_id": "CORE-001", "description": "Core item", "description_th": "สินค้า", "type": "Fish",
                "original_item_id": "", "core_created": True,
                **{field: "" for field in EDITABLE_FIELDS if field not in {"description", "description_th", "type"}},
            }, "YIM")

            archived_source = explorer.archive("IT-001", "BIAS")
            archived_core = explorer.archive(created["item_id"], "YIM")
            self.assertTrue(archived_source["archived"])
            self.assertTrue(archived_core["archived"])
            self.assertEqual([], explorer.search()["items"])
            self.assertIsNotNone(explorer._local_store().canonical_for("IT-001"))
            self.assertIsNotNone(explorer._local_store().canonical_for("CORE-001"))
            self.assertEqual([( "IT-001", "BIAS"), ("CORE-001", "YIM")], explorer._local_store().connection.execute(
                "SELECT item_id, actor_username FROM activity WHERE changes_json LIKE '%archived%' ORDER BY id"
            ).fetchall())


if __name__ == "__main__":
    unittest.main()
