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
            self.assertEqual(
                {
                    "items": {"required": True, "ready": True, "status": "verified"},
                    "customers": {"ready": False, "status": "unavailable"},
                    "usa_name_direct_source": {"available": False},
                    "change_name_table": {"available": False},
                    "awb_shipments": {"ready": False, "status": "unavailable"},
                    "orders": {"ready": False, "status": "unavailable"},
                },
                manifest["capabilities"],
            )
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

    def test_capability_inventory_reports_schema_only_optional_readiness_without_rejecting_base_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute('ALTER TABLE "MainDB__ITEM" ADD COLUMN "USA Name" TEXT')
            connection.execute('CREATE TABLE "TempDB__ChangeName" ("Legacy Name" TEXT)')
            connection.execute('CREATE TABLE "MainDB__AWB" ("Invoice No" TEXT, "AWB No" TEXT, "Date" TEXT)')
            for table, columns in {
                "MainDB__ORDER": ("Order No", "Order Date", "Cust ID"),
                "MainDB__ORDER_ITEM": ("Order No", "Line No", "Item ID", "Qty"),
                "MainDB__CUST": ("Cust ID", "Name", "Inv Type"),
                "MainDB__CUST_CON": ("Cust ID", "Com Code"),
                "MainDB__CUST_CONSIGNEE": ("Cust ID", "Consignee"),
                "MainDB__CUST_NOTE": ("Cust ID", "Order", "Invoice"),
            }.items():
                definition = ", ".join(f'"{column}" TEXT' for column in columns)
                connection.execute(f'CREATE TABLE "{table}" ({definition})')
            connection.commit()
            connection.close()
            output = root / "state" / "accepted_snapshot.json"

            manifest = certify_snapshot(source, output, generated_at="2026-08-25T13:00:00Z")

            self.assertEqual(
                {
                    "items": {"required": True, "ready": True, "status": "verified"},
                    "customers": {"ready": True, "status": "verified"},
                    "usa_name_direct_source": {"available": True},
                    "change_name_table": {"available": True},
                    "awb_shipments": {"ready": True, "status": "verified"},
                    "orders": {"ready": True, "status": "verified"},
                },
                manifest["capabilities"],
            )
            self.assertNotIn("customer_ready", manifest)
            self.assertEqual(manifest, json.loads(output.read_text(encoding="utf-8")))

    def test_awb_readiness_accepts_every_configured_identity_alias(self):
        aliases = (
            ("invoice", ("Inv No", "INVOICE.Inv No", "Invoice No", "InvNo")),
            ("awb", ("AWB", "AWB No", "AWBNo")),
            ("date", ("AWB Date", "AWBDate", "Date")),
        )
        for group_index, (group_name, group_aliases) in enumerate(aliases):
            for alias in group_aliases:
                with self.subTest(group=group_name, alias=alias), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source = self.make_snapshot(root)
                    selected = ["Inv No", "AWB", "AWB Date"]
                    selected[group_index] = alias
                    connection = sqlite3.connect(source)
                    definition = ", ".join(f'"{column}" TEXT' for column in selected)
                    connection.execute(f'CREATE TABLE "MainDB__AWB" ({definition})')
                    connection.commit()
                    connection.close()

                    manifest = certify_snapshot(source, root / "state" / "accepted_snapshot.json", generated_at="2026-08-25T13:00:00Z")

                    self.assertTrue(manifest["accepted"])
                    self.assertEqual(
                        {"ready": True, "status": "verified"},
                        manifest["capabilities"]["awb_shipments"],
                    )

    def test_missing_each_awb_identity_group_leaves_awb_shipments_unavailable_without_rejecting_snapshot(self):
        identity_columns = ("Inv No", "AWB", "AWB Date")
        for missing_index, missing_group in enumerate(("invoice", "awb", "date")):
            with self.subTest(missing_group=missing_group), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self.make_snapshot(root)
                remaining = [column for index, column in enumerate(identity_columns) if index != missing_index]
                connection = sqlite3.connect(source)
                definition = ", ".join(f'"{column}" TEXT' for column in remaining)
                connection.execute(f'CREATE TABLE "MainDB__AWB" ({definition})')
                connection.commit()
                connection.close()

                manifest = certify_snapshot(source, root / "state" / "accepted_snapshot.json", generated_at="2026-08-25T13:00:00Z")

                self.assertTrue(manifest["accepted"])
                self.assertEqual(
                    {"ready": False, "status": "unavailable"},
                    manifest["capabilities"]["awb_shipments"],
                )

    def test_customers_are_verified_without_making_orders_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
            connection.commit()
            connection.close()

            manifest = certify_snapshot(source, root / "state" / "accepted_snapshot.json", generated_at="2026-08-25T13:00:00Z")

            self.assertTrue(manifest["accepted"])
            self.assertEqual({"ready": True, "status": "verified"}, manifest["capabilities"]["customers"])
            self.assertEqual({"ready": False, "status": "unavailable"}, manifest["capabilities"]["orders"])

    def test_missing_order_inv_type_does_not_change_customer_or_awb_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE "MainDB__AWB" ("Inv No" TEXT, "AWB" TEXT, "AWB Date" TEXT)')
            for table, columns in {
                "MainDB__ORDER": ("Order No", "Order Date", "Cust ID"),
                "MainDB__ORDER_ITEM": ("Order No", "Line No", "Item ID", "Qty"),
                "MainDB__CUST": ("Cust ID", "Name"),
                "MainDB__CUST_CON": ("Cust ID", "Com Code"),
                "MainDB__CUST_CONSIGNEE": ("Cust ID", "Consignee"),
                "MainDB__CUST_NOTE": ("Cust ID", "Order", "Invoice"),
            }.items():
                definition = ", ".join(f'"{column}" TEXT' for column in columns)
                connection.execute(f'CREATE TABLE "{table}" ({definition})')
            connection.commit()
            connection.close()

            manifest = certify_snapshot(source, root / "state" / "accepted_snapshot.json", generated_at="2026-08-25T13:00:00Z")

            self.assertTrue(manifest["accepted"])
            self.assertEqual({"ready": True, "status": "verified"}, manifest["capabilities"]["customers"])
            self.assertEqual({"ready": True, "status": "verified"}, manifest["capabilities"]["awb_shipments"])
            self.assertEqual({"ready": False, "status": "unavailable"}, manifest["capabilities"]["orders"])

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
