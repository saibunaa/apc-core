import json
import re
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from apc_core.item_explorer import ItemExplorer, make_handler


class ItemExplorerTests(unittest.TestCase):
    def make_snapshot(self, root: Path) -> Path:
        source = root / "latest.sqlite"
        connection = sqlite3.connect(source)
        connection.execute(
            'CREATE TABLE "MainDB__ITEM" ('
            '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, '
            '"Type" TEXT, "Family" TEXT, "Price EU" REAL)'
        )
        connection.executemany(
            'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?, ?)',
            [
                ("IT-001", "Neon tetra", "นีออนเตตร้า", "Fish", "Tropical", 99.0),
                ("IT-002", "River stone", "หินแม่น้ำ", "Supply", "Hardscape", 10.0),
            ],
        )
        connection.commit()
        connection.close()
        return source

    def test_search_returns_allowlisted_readonly_item_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = ItemExplorer(self.make_snapshot(Path(tmp)), data_dir=Path(tmp) / "core-state")
            result = explorer.search("neon", limit=20)

            self.assertEqual(1, result["total"])
            self.assertEqual("IT-001", result["items"][0]["item_id"])
            self.assertEqual("Neon tetra", result["items"][0]["description"])
            self.assertEqual("นีออนเตตร้า", result["items"][0]["description_th"])
            self.assertEqual("Fish", result["items"][0]["type"])
            self.assertEqual(["Fish", "Supply"], result["type_options"])
            self.assertEqual("Tropical", result["items"][0]["scientific_family"])
            self.assertEqual("", result["items"][0]["family"])
            self.assertNotIn("Price EU", result["items"][0])

    def test_search_pages_through_more_than_fifty_visible_items_with_bounded_page_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.executemany(
                'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?, ?)',
                [(f"IT-{number:04d}", f"Item {number}", "สินค้า", "Fish", "Tropical", 1.0) for number in range(3, 2550)],
            )
            connection.commit(); connection.close()

            explorer = ItemExplorer(source, data_dir=root / "core-state")
            first = explorer.search(limit=100, offset=0)
            last = explorer.search(limit=100, offset=2500)

            self.assertEqual(2549, first["total"])
            self.assertEqual(100, first["limit"])
            self.assertEqual(0, first["offset"])
            self.assertEqual(49, len(last["items"]))
            self.assertEqual("IT-2549", last["items"][-1]["item_id"])

    def test_search_decodes_tis620_text_for_safe_display_only(self):
        raw_thai = "เฟิร์นก้านดำ+ตะไคร่น้ำ".encode("cp874").decode("latin1")
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_snapshot(Path(tmp))
            connection = sqlite3.connect(source)
            connection.execute(
                'UPDATE "MainDB__ITEM" SET "Description TH" = ? WHERE "Item ID" = ?',
                (raw_thai, "IT-001"),
            )
            connection.commit()
            connection.close()

            result = ItemExplorer(source, data_dir=Path(tmp) / "core-state").search("IT-001")
            self.assertEqual(result["items"][0]["description_th"], "เฟิร์นก้านดำ+ตะไคร่น้ำ")

    def test_routes_menu_and_item_explorer_with_path_relative_readonly_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = ItemExplorer(self.make_snapshot(Path(tmp)), data_dir=Path(tmp) / "core-state")
            handler = make_handler(
                explorer,
                {"accepted": True, "scope": "read_only_item_explorer", "generated_at": "2026-08-26T00:00:00Z"},
            )
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/")
                response = conn.getresponse()
                menu_html = response.read().decode("utf-8")
                self.assertEqual(200, response.status)
                self.assertIn("APC Core", menu_html)
                self.assertIn('href="items/"', menu_html)
                self.assertIn("Items", menu_html)
                self.assertNotIn("legacy", menu_html.lower())
                self.assertNotIn("snapshot", menu_html.lower())
                for module in ("Orders", "Customers", "Shipments", "Activity"):
                    self.assertIn(module, menu_html)
                self.assertEqual(3, menu_html.count("Coming soon"))
                conn.close()

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/items/")
                response = conn.getresponse()
                items_html = response.read().decode("utf-8")
                self.assertEqual(200, response.status)
                self.assertIn("Item Explorer", items_html)
                self.assertIn("fetch('api/items", items_html)
                self.assertIn("Load more", items_html)
                self.assertIn("offset=", items_html)
                self.assertIn("tr:nth-child(even)", items_html)
                self.assertIn("tr:focus", items_html)
                self.assertNotIn("Accepted local snapshot", items_html)
                self.assertIn(".card:hover", menu_html)
                conn.close()

                for path in ("/api/items?q=river", "/items/api/items?q=river"):
                    conn = HTTPConnection(host, port, timeout=3)
                    conn.request("GET", path)
                    response = conn.getresponse()
                    body = json.loads(response.read())
                    self.assertEqual(200, response.status)
                    self.assertEqual("IT-002", body["items"][0]["item_id"])
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_menu_cleanup_and_item_workspace_has_return_link_and_sticky_detail_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = ItemExplorer(self.make_snapshot(Path(tmp)), data_dir=Path(tmp) / "core-state")
            handler = make_handler(explorer, {"accepted": True, "scope": "read_only_item_explorer"})
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/")
                menu_html = conn.getresponse().read().decode("utf-8")
                self.assertNotIn("Operations, in one calm place.", menu_html)
                self.assertNotIn("Start with the essentials.", menu_html)
                conn.close()

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/items/")
                items_html = conn.getresponse().read().decode("utf-8")
                self.assertIn('href="../"', items_html)
                self.assertIn("← APC Core", items_html)
                self.assertIn(".detail{position:sticky", items_html)
                self.assertNotIn("read-only pilot", items_html.lower())
                self.assertNotIn("snapshot", items_html.lower())
                conn.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_local_edits_merge_with_snapshot_and_append_audit_without_source_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            before = source.read_bytes()
            explorer = ItemExplorer(source, data_dir=root / "core-state")

            updated = explorer.edit("IT-001", {"description": "Edited neon", "family": "Freshwater"}, "YIM")

            self.assertEqual("Edited neon", updated["description"])
            self.assertEqual("Freshwater", updated["family"])
            self.assertEqual("Fish", updated["type"])
            merged = explorer.search("Edited")
            self.assertEqual("IT-001", merged["items"][0]["item_id"])
            self.assertEqual("Edited neon", merged["items"][0]["description"])
            self.assertNotIn("source", merged["items"][0])
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(1, explorer.activity_count())
            self.assertTrue((root / "core-state" / "apc_core.sqlite").is_file())

    def test_edit_rejects_item_id_and_unsupported_or_invalid_fields_without_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "core-state")
            for changes in (
                {"item_id": "IT-009"},
                {"price": 10},
                {"description": ""},
                {"type": 42},
            ):
                with self.assertRaises(ValueError):
                    explorer.edit("IT-001", changes)
            self.assertEqual(0, explorer.activity_count())

    def test_core_item_types_are_distinct_safe_snapshot_values_and_new_types_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "core-state")

            self.assertEqual(("Fish", "Supply"), explorer.item_types())
            with self.assertRaises(ValueError):
                explorer.edit("IT-001", {"type": "Plant"})
            self.assertEqual(0, explorer.activity_count())

    def test_existing_legacy_type_remains_selectable_but_other_unallowlisted_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "core-state")
            explorer._local_store().save("IT-001", {"type": "Historic legacy"})

            preserved = explorer.edit("IT-001", {"type": "Historic legacy"}, "YIM")
            self.assertEqual("Historic legacy", preserved["type"])
            with self.assertRaises(ValueError):
                explorer.edit("IT-001", {"type": "Plant"})

    def test_edit_form_renders_searchable_type_choices_and_preserves_current_selection(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()

        self.assertIn('<input name="type" list="type-options"', html)
        self.assertIn('id="type-options"', html)
        self.assertIn('addChoices(data.items)', html)
        self.assertIn('Object.entries(item)', html)

    def test_full_edit_item_fields_merge_audited_locally_and_validate_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "latest.sqlite"
            connection = sqlite3.connect(source)
            connection.execute(
                'CREATE TABLE "MainDB__ITEM" ('
                '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT, '
                '"Price EU" REAL, "Price JP" REAL, "Price TH" REAL, "QtyPerPCS" REAL, "PcsPerPack" INTEGER)'
            )
            connection.execute(
                'INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ("IT-FULL", "English name", "ชื่อไทย", "Fish", "Phyto baseline", 12.5, 600, 440, 1.5, 24),
            )
            connection.commit()
            connection.close()
            before = source.read_bytes()
            explorer = ItemExplorer(source, data_dir=root / "core-state")

            baseline = explorer.search("English")["items"][0]
            self.assertEqual("English name", baseline["description"])
            self.assertEqual("Phyto baseline", baseline["scientific_family"])
            self.assertEqual("1.5", baseline["quantity_per_piece"])
            self.assertEqual("12.5", baseline["price_eu"])
            self.assertEqual("600", baseline["price_jp"])
            self.assertEqual("440", baseline["price_th"])
            self.assertEqual("24", baseline["quantity_per_bag"])

            changes = {
                "description": "English name edited", "description_th": "ชื่อไทยแก้ไข", "usa_name": "US name",
                "type": "Fish", "quantity_per_piece": "2.75", "price_eu": "13.50", "price_jp": "650",
                "price_th": "450.25", "phyto_family": "Phyto edited", "keset_family": "Keset",
                "scientific_family": "Scientific", "thai_family": "ไทย", "apc_group": "B", "apc_team": "Team One",
                "quantity_per_carton": "30", "quantity_per_styrofoam": "6", "pack_sequence": "5", "quantity_per_bag": "3",
            }
            updated = explorer.edit("IT-FULL", changes, "YIM")
            self.assertTrue({"item_id", "family", *changes}.issubset(set(updated)))
            for field, value in changes.items():
                self.assertEqual(value, updated[field])
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(1, explorer.activity_count())

            for invalid in (
                {"item_id": "IT-OTHER"}, {"unknown": "x"}, {"price_eu": "-0.01"},
                {"quantity_per_carton": "1.2"}, {"pack_sequence": "0"}, {"pack_sequence": "6"},
                {"apc_group": "C"}, {"quantity_per_bag": True},
            ):
                with self.assertRaises(ValueError):
                    explorer.edit("IT-FULL", invalid)
            self.assertEqual(1, explorer.activity_count())

    def test_edit_form_has_all_compact_grouped_field_markers_and_locked_item_id(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()
        for marker in (
            'readonly', 'name="item_id"', 'name="description"', 'name="description_th"', 'name="usa_name"',
            'name="type"', 'name="quantity_per_piece"', 'name="price_eu"', 'name="price_jp"', 'name="price_th"',
            'name="phyto_family"', 'name="keset_family"', 'name="scientific_family"', 'name="thai_family"',
            'name="apc_group"', "'A'", "'B'", 'id="group-options"', 'name="apc_team"',
            'name="quantity_per_carton"', 'name="quantity_per_styrofoam"', 'name="pack_sequence"',
            '<option value="1">1</option>', '<option value="5">5</option>', 'name="quantity_per_bag"', 'fieldset', 'form-section',
        ):
            self.assertIn(marker, html)

    def test_post_item_edit_api_accepts_only_permitted_fields_and_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            before = source.read_bytes()
            explorer = ItemExplorer(source, data_dir=root / "core-state")
            handler = make_handler(explorer, {"accepted": True, "scope": "read_only_item_explorer"})
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                body = json.dumps({"description_th": "แก้ไขแล้ว", "type": "Fish", "actor": "YIM"}).encode("utf-8")
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/api/items/IT-001", body, {"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(200, response.status)
                self.assertEqual("แก้ไขแล้ว", json.loads(response.read())["item"]["description_th"])
                conn.close()

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/api/items/IT-001", json.dumps({"item_id": "IT-009"}), {"Content-Type": "application/json"})
                self.assertEqual(400, conn.getresponse().status)
                conn.close()
                self.assertEqual(before, source.read_bytes())
            finally:
                server.shutdown()
                server.server_close()

    def test_verified_snapshot_mapping_and_conflict_safe_backfill_preserve_core_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted.sqlite"
            connection = sqlite3.connect(source)
            connection.execute(
                'CREATE TABLE "MainDB__ITEM" ('
                '"Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, '
                '"Family" TEXT, "USA Name" TEXT, "QtyPerPcs" REAL, "Phyto Family" TEXT, '
                '"Keset Family" TEXT, "Scientific Family" TEXT, "Thai Family" TEXT, "APC Group" TEXT, '
                '"APC Team" TEXT, "QtyPerCarton" INTEGER, "QtyPerStyrofoam" INTEGER, "PackSeq" INTEGER, '
                '"PcsPerPack" INTEGER)'
            )
            connection.execute(
                'INSERT INTO "MainDB__ITEM" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ("IT-MAP", "Baseline", "ชื่อเดิม", "Fish", "VB6 family", "USA name", 2.5,
                 "Phyto mapped", "Keset mapped", "Scientific mapped", "ไทย mapped", "B", "Team blue", 24, 4, 3, 12),
            )
            connection.commit()
            connection.close()
            explorer = ItemExplorer(source, data_dir=root / "core-state")
            explorer._local_store().save("IT-MAP", {"description": "Core override"})

            summary = explorer.backfill_from_snapshot()
            item = explorer.search("IT-MAP")["items"][0]

            self.assertEqual({"accepted": 1, "duplicate": 0, "unmatched": 0, "out_of_range": 0, "preserved": 1}, summary)
            self.assertEqual("Core override", item["description"])
            self.assertEqual("Phyto mapped", item["phyto_family"])
            self.assertEqual("Scientific mapped", item["scientific_family"])
            self.assertEqual("USA name", item["usa_name"])
            self.assertEqual("2.5", item["quantity_per_piece"])
            self.assertEqual("12", item["quantity_per_bag"])
            self.assertEqual("3", item["pack_sequence"])

    def test_backfill_joins_verified_vb6_phyto_and_packing_tables_for_unmaterialized_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute(
                'CREATE TABLE "MainDB__PHYTO_GROUP" ('
                '"ITEM ID" TEXT, "DESC SPP" TEXT, "DESC TH SPP" TEXT, "GROUP" TEXT, '
                '"GROUP2" TEXT, "KESIT GROUP" TEXT, "Hardness" INTEGER)'
            )
            connection.execute(
                'CREATE TABLE "MainDB__PACKING" ('
                '"Item ID" TEXT, "Paper18" INTEGER, "Styrofoam31" INTEGER)'
            )
            connection.execute(
                'INSERT INTO "MainDB__PHYTO_GROUP" VALUES (?,?,?,?,?,?,?)',
                ("IT-001", "Phyto family", "ชื่อไฟโต", "Team Green", "A", "Keset family", 4),
            )
            connection.execute(
                'INSERT INTO "MainDB__PACKING" VALUES (?,?,?)', ("IT-001", 24, 6),
            )
            connection.commit()
            connection.close()
            explorer = ItemExplorer(source, data_dir=root / "core-state")

            summary = explorer.backfill_from_snapshot()
            item = explorer.search("IT-001")["items"][0]

            self.assertEqual(2, summary["accepted"])
            self.assertEqual("Phyto family", item["phyto_family"])
            self.assertEqual("Keset family", item["keset_family"])
            self.assertEqual("ชื่อไฟโต", item["thai_family"])
            self.assertEqual("A", item["apc_group"])
            self.assertEqual("Team Green", item["apc_team"])
            self.assertEqual("4", item["pack_sequence"])
            self.assertEqual("24", item["quantity_per_carton"])
            self.assertEqual("6", item["quantity_per_styrofoam"])

    def test_backfill_quarantines_duplicate_unmatched_and_out_of_range_without_mutating_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted.sqlite"
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT, "PackSeq" INTEGER)')
            connection.executemany(
                'INSERT INTO "MainDB__ITEM" VALUES (?,?,?,?,?,?)',
                [
                    ("IT-DUP", "first", "ไทย", "Fish", "Family", 3),
                    ("IT-DUP", "second", "ไทย", "Fish", "Family", 3),
                    ("IT-BAD", "bad", "ไทย", "Fish", "Family", 6),
                ],
            )
            connection.commit()
            connection.close()
            explorer = ItemExplorer(source, data_dir=root / "core-state")
            explorer._local_store().save("IT-ORPHAN", {"description": "Keep me"})

            summary = explorer.backfill_from_snapshot()

            self.assertEqual({"accepted": 0, "duplicate": 2, "unmatched": 1, "out_of_range": 1, "preserved": 0}, summary)
            self.assertEqual({"description": "Keep me"}, explorer._local_store().override_for("IT-ORPHAN"))
            self.assertEqual([], explorer.search()["items"])
            quarantined = explorer._local_store().quarantine_reasons()
            self.assertCountEqual(
                ["duplicate_item_id", "duplicate_item_id", "out_of_range:pack_sequence", "unmatched_override"],
                quarantined,
            )

    def test_backfill_quarantines_malformed_decimal_text_without_aborting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted.sqlite"
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT, "Price EU" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES (?,?,?,?,?,?)', ("IT-DECIMAL", "bad", "ไทย", "Fish", "Family", "1.2x"))
            connection.commit()
            connection.close()
            explorer = ItemExplorer(source, data_dir=root / "core-state")
            explorer._local_store().save("IT-DECIMAL", {"description": "Core description"})

            summary = explorer.backfill_from_snapshot()

            self.assertEqual({"accepted": 0, "duplicate": 0, "unmatched": 0, "out_of_range": 1, "preserved": 0}, summary)
            self.assertEqual([], explorer.search()["items"])
            self.assertEqual(["out_of_range:price_eu"], explorer._local_store().quarantine_reasons())

    def test_server_is_loopback_only_and_rejects_unsupported_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = ItemExplorer(self.make_snapshot(Path(tmp)), data_dir=Path(tmp) / "core-state")
            handler = make_handler(explorer, {"accepted": True, "scope": "read_only_item_explorer"})
            from http.server import ThreadingHTTPServer

            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                host, port = server.server_address
                self.assertEqual("127.0.0.1", host)
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/api/items?q=river")
                response = conn.getresponse()
                body = json.loads(response.read())
                self.assertEqual(200, response.status)
                self.assertEqual("IT-002", body["items"][0]["item_id"])
                conn.close()

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/api/items")
                response = conn.getresponse()
                self.assertEqual(400, response.status)
                self.assertEqual({"error": "invalid item create"}, json.loads(response.read()))
                conn.close()
            finally:
                server.shutdown()
                server.server_close()


    def test_item_lookup_filters_expose_verified_options_and_core_draft_survives_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            explorer = ItemExplorer(source, data_dir=root / "core-state")

            filters = explorer.filter_options()
            self.assertEqual([], filters["family_options"])  # only Phyto Family is a verified Family filter
            self.assertEqual([], filters["group_options"])
            self.assertEqual(["1", "2", "3", "4", "5"], filters["pack_sequence_options"])
            self.assertEqual(["Fish", "Supply"], filters["type_options"])
            self.assertEqual(["IT-001", "IT-002"], [item["item_id"] for item in explorer.search(item_id_prefix="it-00", limit=10)["items"]])

            before = source.read_bytes()
            draft = explorer.duplicate("IT-001", "YIM")
            self.assertNotEqual("IT-001", draft["item_id"])
            self.assertTrue(draft["item_id"].startswith("IT-001-C"))
            self.assertEqual("IT-001", draft["original_item_id"])
            self.assertTrue(draft["core_created"])
            self.assertEqual("Core-created", draft["source_label"])
            self.assertEqual("Neon tetra", draft["description"])
            self.assertEqual(before, source.read_bytes())
            explorer.backfill_from_snapshot()
            self.assertEqual([], explorer.search(item_id_prefix=draft["item_id"], limit=10)["items"])
            with self.assertRaises(ValueError):
                explorer.create({**draft, "item_id": "IT-001"}, "YIM")
            created = explorer.create({**draft, "item_id": "IT-001-CUSTOM"}, "YIM")
            self.assertEqual("IT-001-CUSTOM", created["item_id"])

    def test_search_applies_description_family_group_type_and_pack_filters_with_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute('ALTER TABLE "MainDB__ITEM" ADD COLUMN "Phyto Family" TEXT')
            connection.execute('ALTER TABLE "MainDB__ITEM" ADD COLUMN "APC Group" TEXT')
            connection.execute('ALTER TABLE "MainDB__ITEM" ADD COLUMN "PackSeq" INTEGER')
            connection.execute('UPDATE "MainDB__ITEM" SET "Phyto Family"=?, "APC Group"=?, "PackSeq"=? WHERE "Item ID"=?', ("Tetra", "A", 2, "IT-001"))
            connection.execute('UPDATE "MainDB__ITEM" SET "Phyto Family"=?, "APC Group"=?, "PackSeq"=? WHERE "Item ID"=?', ("Stone", "B", 4, "IT-002"))
            connection.commit(); connection.close()
            explorer = ItemExplorer(source, data_dir=root / "core-state")

            result = explorer.search(description="นีออน", family="Tetra", group="A", item_type="Fish", pack_sequence="2")
            self.assertEqual(1, result["total"])
            self.assertEqual("IT-001", result["items"][0]["item_id"])
            self.assertEqual(["Stone", "Tetra"], result["family_options"])
            self.assertEqual(["A", "B"], result["group_options"])

    def test_main_search_is_simple_advanced_filters_are_collapsed_and_edit_choices_are_searchable(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()
        for marker in (
            'id="search"', 'placeholder="Search item ID, English or Thai description"',
            '<details class="advanced-search">', '<summary>Advanced Search</summary>',
            'class="toolbar"', '.toolbar{position:sticky',
            'name="type" list="type-options"', 'name="phyto_family" list="phyto-family-options"',
            'name="keset_family" list="keset-family-options"',
            'name="scientific_family" list="scientific-family-options"',
            'name="thai_family" list="thai-family-options"', 'name="apc_group" type="radio" value="A"',
            'name="apc_group" type="radio" value="B"', 'name="apc_group" type="radio" value=""',
            'name="pack_sequence"', '<option value="1">1</option>', '<option value="5">5</option>',
            'id="type-options"', 'id="phyto-family-options"', 'id="group-options"', 'APC Team',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('<div class="filters">', html.split('<details class="advanced-search">', 1)[0])

    def test_usability_ui_has_controlled_filters_edit_state_and_requested_copy(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()
        for marker in (
            'Item ID prefix', 'list="item-id-options"', '<datalist id="item-id-options">',
            'Description / Thai description', 'Family', 'Group', 'Pack Sequence', 'Active filters',
            'Clear all', 'result-count', 'No items match these filters',
            'Save changes', 'Cancel', 'Unsaved changes', 'Duplicate Item', 'source_label',
            'Families &amp; Group', 'APC Team', 'position:sticky', 'item-id-options', 'Unsaved duplicate',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('Families & ownership', html)
        self.assertNotIn('<select name="item_id"', html)

    def test_item_api_supports_filters_options_and_explicit_duplicate_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = ItemExplorer(self.make_snapshot(root), data_dir=root / "core-state")
            handler = make_handler(explorer, {"accepted": True, "scope": "read_only_item_explorer"})
            from http.server import ThreadingHTTPServer
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                conn = HTTPConnection(host, port, timeout=3)
                conn.request("GET", "/api/items?item_id_prefix=IT-001&description=neon")
                response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
                self.assertEqual(200, response.status)
                self.assertEqual(1, payload["total"])
                self.assertIn("family_options", payload)

                conn = HTTPConnection(host, port, timeout=3)
                conn.request("POST", "/api/items/IT-001/duplicate", b'{"actor":"YIM"}', {"Content-Type": "application/json"})
                response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
                self.assertEqual(400, response.status)
                self.assertEqual({"error": "invalid item edit"}, payload)
                self.assertEqual(2, explorer.search()["total"])
            finally:
                server.shutdown(); server.server_close()

    def test_item_workspace_keeps_both_result_list_and_edit_detail_scrollable(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()

        self.assertIn('.workspace{display:grid;grid-template-columns:minmax(0,1fr) 470px;', html)
        self.assertIn('.queue{padding:18px;min-width:0;max-height:calc(100vh - 120px);overflow-y:auto}', html)
        self.assertIn('.detail{position:sticky;top:20px;align-self:start;max-height:calc(100vh - 40px);overflow-y:auto;', html)
        self.assertIn('@media(max-width:900px){.shell{padding:16px}.workspace{grid-template-columns:1fr}.detail{position:static;', html)

    def test_selected_item_detail_is_read_only_until_explicit_edit(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()
        detail = re.search(r'<template id="detail-template">(.*?)</template>', html).group(1)
        edit = re.search(r'<template id="edit-template">(.*?)</template>', html).group(1)

        self.assertIn('data-edit-item', detail)
        self.assertIn('>Edit<', detail)
        self.assertNotIn('<form', detail)
        self.assertNotIn('<input', detail)
        self.assertNotIn('Save changes', detail)
        self.assertNotIn('>Cancel<', detail)
        self.assertIn('<form class="edit-form">', edit)
        self.assertIn('Save changes', edit)
        self.assertIn('data-cancel', edit)
        self.assertIn('function select(item){current=item;renderDetail(item)}', html)
        self.assertIn("querySelector('[data-edit-item]').onclick=()=>renderEdit(current)", html)

    def test_cancel_restores_latest_selected_detail_without_another_request(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()

        self.assertIn("form.querySelector('[data-cancel]').onclick=()=>current&&current.core_created&&!current.item_id?renderEmptyDetail():renderDetail(current)", html)
        self.assertIn("current=(await response.json()).item;status.textContent='Saved locally.';await load();renderDetail(current)", html)

    def test_edit_save_keeps_actor_guard_and_existing_form_submission_boundary(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()

        self.assertIn('const selectedActor=requireActor();if(!selectedActor)return;const changes=Object.fromEntries(new FormData(form));changes.actor=selectedActor;', html)
        self.assertIn("fetch(creating?'api/items':'api/items/'+encodeURIComponent(current.item_id)", html)

    def test_apc_group_radios_are_native_semantic_horizontal_fieldset(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()
        group = re.search(r'<fieldset class="apc-group-row">(.*?)</fieldset>', html).group(1)

        self.assertIn('<legend>APC Group</legend>', group)
        self.assertIn('class="radio-group"', group)
        self.assertIn('.radio-group{display:flex;', html)
        for value in ('A', 'B', ''):
            self.assertIn(f'name="apc_group" type="radio" value="{value}"', group)
        self.assertNotIn('role="radio"', group)
        self.assertNotIn('tabindex=', group)

    def test_add_item_button_requires_shared_actor_opens_new_draft_and_cancels_without_mutation(self):
        html = __import__("apc_core.item_explorer", fromlist=["_item_explorer_html"])._item_explorer_html()

        self.assertIn('<button id="add-item" type="button">Add item</button>', html)
        self.assertIn("$('#add-item').onclick=()=>{if(!requireActor())return;const proposal={item_id:'',core_created:true,source_label:'Core-created'};current=proposal;renderEdit(proposal)}", html)
        self.assertIn("if(item.core_created&&!item.item_id){const idControl=form.elements.namedItem('item_id');idControl.readOnly=false;idControl.classList.remove('locked');", html)
        self.assertIn("form.querySelector('[data-cancel]').onclick=()=>current&&current.core_created&&!current.item_id?renderEmptyDetail():renderDetail(current)", html)
        self.assertIn("function renderEmptyDetail(){current=null;$('#detail').innerHTML='<p class=\"status\">Select an item to edit it locally.</p>'}", html)
        self.assertIn("fetch(creating?'api/items':'api/items/'+encodeURIComponent(current.item_id)", html)


if __name__ == "__main__":
    unittest.main()
