import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ImportSnapshotCliContractTests(unittest.TestCase):
    def make_snapshot(self, path: Path, item_id: str) -> None:
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)')
        con.execute('INSERT INTO "MainDB__ITEM" VALUES (?,?,?,?,?)', (item_id, "Item", "สินค้า", "Fish", "Tropical"))
        con.commit(); con.close()

    def test_preview_picks_newest_local_candidate_without_writing_state(self):
        from apc_core.import_snapshot import latest_snapshot, preview
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); snapshots = root / "snapshots"; snapshots.mkdir(); state = root / "state"
            self.make_snapshot(snapshots / "apc_mdb_snapshot_20260825_224502_bkk.sqlite", "OLD")
            newest = snapshots / "apc_mdb_snapshot_20260826_064501_bkk.sqlite"
            self.make_snapshot(newest, "NEW")
            report = preview(latest_snapshot(snapshots))
            self.assertEqual(str(newest.resolve()), report["source"])
            self.assertEqual(1, report["item_count"])
            self.assertNotIn("confirmed", report)
            self.assertFalse(state.exists())

    def test_confirm_certifies_and_backfills_only_selected_local_candidate(self):
        from apc_core.import_snapshot import import_latest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); snapshots = root / "snapshots"; snapshots.mkdir(); state = root / "state"
            source = snapshots / "apc_mdb_snapshot_20260826_064501_bkk.sqlite"
            self.make_snapshot(source, "NEW")
            report = import_latest(snapshots, state)
            self.assertTrue(report["confirmed"])
            self.assertEqual(1, report["backfill"]["accepted"])
            self.assertTrue((state / "accepted_snapshot.json").is_file())
            self.assertTrue((state / "apc_core.sqlite").is_file())

    def test_confirm_keeps_selected_snapshot_descriptor_pinned_when_name_is_replaced_after_preview(self):
        from apc_core import import_snapshot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); snapshots = root / "snapshots"; snapshots.mkdir(); state = root / "state"
            source = snapshots / "apc_mdb_snapshot_20260826_064501_bkk.sqlite"
            replacement = snapshots / "replacement.sqlite"
            self.make_snapshot(source, "PINNED")
            self.make_snapshot(replacement, "SWAPPED")
            original_preview = import_snapshot.preview_selected

            def replace_visible_name(selected):
                report = original_preview(selected)
                replacement.replace(source)
                return report

            with patch.object(import_snapshot, "preview_selected", side_effect=replace_visible_name):
                report = import_snapshot.import_latest(snapshots, state)

            explorer, _ = import_snapshot.load_accepted_runtime(state / "accepted_snapshot.json", data_dir=state)
            try:
                self.assertEqual("PINNED", explorer.search()["items"][0]["item_id"])
            finally:
                explorer.close()


if __name__ == "__main__":
    unittest.main()
