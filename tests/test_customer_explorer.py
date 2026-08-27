import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apc_core.customer_explorer import CustomerExplorer


class TestCustomerExplorerContract(unittest.TestCase):
    def make_snapshot(self, root: Path, customer_rows=None) -> Path:
        source = root / "accepted-customers.sqlite"
        connection = sqlite3.connect(source)
        connection.execute(
            'CREATE TABLE "MainDB__CUST" ('
            '"Cust ID" TEXT, "Name" TEXT, "Address 1" TEXT, "Tel" TEXT, "Fax" TEXT, '
            '"Email" TEXT, "Price Type" TEXT, "BoxType" TEXT, "Inv Header" TEXT, '
            '"Inv Type" TEXT, "Year No" TEXT)'
        )
        connection.executemany(
            'INSERT INTO "MainDB__CUST" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            customer_rows or [("C-001", "Pacific Plants", "Bangkok", "02000001", "02000002", "ops@example.test", "EU", "B", "APC", "1", "2026")],
        )
        connection.execute(
            'CREATE TABLE "MainDB__CUST_CON" ('
            '"Cust ID" TEXT, "Exporter" TEXT, "Commercial" TEXT, "Order Settings" TEXT, '
            '"HC Settings" TEXT, "AWB Configuration" TEXT)'
        )
        connection.execute(
            'INSERT INTO "MainDB__CUST_CON" VALUES (?, ?, ?, ?, ?, ?)',
            ("C-001", "APC Export", "Commercial team", "order", "hc", "awb"),
        )
        connection.execute(
            'CREATE TABLE "MainDB__CUST_CONSIGNEE" ('
            '"Cust ID" TEXT, "Consignee" TEXT, "Country" TEXT, "Province" TEXT, "Broker" TEXT, "Flight" TEXT)'
        )
        connection.execute(
            'INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?, ?, ?, ?, ?)',
            ("C-001", "Pacific Imports", "JP", "Tokyo", "Broker A", "TG682"),
        )
        connection.execute(
            'CREATE TABLE "MainDB__CUST_NOTE" ('
            '"Cust ID" TEXT, "Note Type" TEXT, "Note" TEXT)'
        )
        connection.executemany(
            'INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)',
            [("C-001", "Order", "Order note"), ("C-001", "Invoice", "Invoice note")],
        )
        connection.commit()
        connection.close()
        return source

    def test_00_customer_explorer_source_contract_is_implemented(self):
        source = Path(__file__).resolve().parents[1] / "apc_core" / "customer_explorer.py"
        self.assertTrue(source.is_file())

    def test_backfill_adopts_customer_children_notes_and_immutable_provenance_without_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            before = source.read_bytes()
            explorer = CustomerExplorer(source, data_dir=root / "state")

            summary = explorer.backfill_from_snapshot()
            profile = explorer.profile("C-001")

            self.assertEqual({"accepted": 1, "duplicate": 0, "unmatched": 0, "malformed": 0, "preserved": 0}, summary)
            self.assertEqual("Pacific Plants", profile["customer"]["name"])
            self.assertEqual("C-001", profile["customer"]["source_customer_id"])
            self.assertEqual(hashlib.sha256(before).hexdigest(), profile["customer"]["source_artifact_sha256"])
            self.assertFalse(profile["customer"]["core_created"])
            self.assertFalse(profile["customer"]["archived"])
            self.assertEqual("APC Export", profile["export_config"]["exporter"])
            self.assertEqual("Pacific Imports", profile["consignees"][0]["consignee"])
            self.assertEqual(["Order note"], [note["body"] for note in profile["order_notes"]])
            self.assertEqual(["Invoice note"], [note["body"] for note in profile["invoice_notes"]])
            self.assertNotIn("prc", repr(profile).casefold())
            self.assertEqual(before, source.read_bytes())

    def test_backfill_adopts_verified_minipc_source_aliases_without_customer_item_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "accepted-minipc-customers.sqlite"
            connection = sqlite3.connect(source)
            connection.execute(
                'CREATE TABLE "MainDB__CUST" ('
                '"Cust ID" TEXT, "Price Type" TEXT, "Name" TEXT, "Add1" TEXT, "Add2" TEXT, "Add3" TEXT, '
                '"Tel" TEXT, "Fax" TEXT, "Email" TEXT, "Inv Header" TEXT, "Inv Type" TEXT, '
                '"Last Yr No" TEXT, "This Yr No" TEXT, "BoxType" TEXT, "Price Range" TEXT)'
            )
            connection.execute(
                'INSERT INTO "MainDB__CUST" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ("C-001", "EU", "MiniPC Plants", "Add 1", "Add 2", "Add 3", "02000001", "", "ops@example.test", "APC", "1", "2025", "2026", "B", "excluded pricing"),
            )
            connection.execute('CREATE TABLE "MainDB__CUST_CON" ("Cust ID" TEXT, "Com Code" TEXT, "Exporter" TEXT)')
            connection.execute('INSERT INTO "MainDB__CUST_CON" VALUES (?, ?, ?)', ("C-001", "COMM", "APC Export"))
            connection.execute('CREATE TABLE "MainDB__CUST_CONSIGNEE" ("Cust ID" TEXT, "Consignee" TEXT, "Province" TEXT, "Country" TEXT, "Broker" TEXT, "Flight" TEXT)')
            connection.execute('INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?, ?, ?, ?, ?)', ("C-001", "MiniPC Imports", "Tokyo", "JP", "Broker A", "TG682"))
            connection.execute('CREATE TABLE "MainDB__CUST_NOTE" ("Cust ID" TEXT, "Invoice" TEXT, "Order" TEXT)')
            connection.execute('INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)', ("C-001", "Invoice note", "Order note"))
            connection.execute('CREATE TABLE "MainDB__CUST_PRC" ("Cust ID" TEXT, "Item ID" TEXT, "Price" TEXT)')
            connection.execute('INSERT INTO "MainDB__CUST_PRC" VALUES (?, ?, ?)', ("C-001", "IT-001", "99.99"))
            connection.commit(); connection.close()

            explorer = CustomerExplorer(source, data_dir=root / "state")
            self.assertEqual(1, explorer.backfill_from_snapshot()["accepted"])
            profile = explorer.profile("C-001")

            self.assertEqual("Add 1", profile["customer"]["address_1"])
            self.assertEqual("Add 2", profile["customer"]["address_2"])
            self.assertEqual("Add 3", profile["customer"]["address_3"])
            self.assertEqual("2026", profile["customer"]["invoice_year"])
            self.assertEqual("COMM", profile["export_config"]["commercial"])
            self.assertEqual("MiniPC Imports", profile["consignees"][0]["consignee"])
            self.assertEqual(["Order note"], [note["body"] for note in profile["order_notes"]])
            self.assertEqual(["Invoice note"], [note["body"] for note in profile["invoice_notes"]])
            self.assertNotIn("99.99", repr(profile))

    def test_backfill_maps_exact_frm_customer_edit_configuration_and_consignee_contracts_without_pricing_rules(self):
        """Map only values that frmCustomerEdit actually persists to CUST CON/CONSIGNEE."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "frm-customer-edit-contract.sqlite"
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
            connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?)', ("C-LEGACY", "Legacy customer"))
            connection.execute(
                'CREATE TABLE "MainDB__CUST_CON" ('
                '"Cust ID" TEXT, "Clean" TEXT, "Sticker" TEXT, "Exporter" TEXT, "Com Code" TEXT, '
                '"Exporter Add" TEXT, "Formula Type" TEXT, "RATE" TEXT, "Charges" TEXT)'
            )
            connection.execute(
                'INSERT INTO "MainDB__CUST_CON" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ("C-LEGACY", "1", "0", "APC Export", "25345", "Exporter address", "2", "37.5", "150"),
            )
            connection.execute(
                'CREATE TABLE "MainDB__CUST_CONSIGNEE" ('
                '"Cust ID" TEXT, "Consignee" TEXT, "Con Add" TEXT, "Country" TEXT, "Province" TEXT, '
                '"Broker" TEXT, "FLIGHT" TEXT, "Time" TEXT, "HC Set2" TEXT)'
            )
            connection.execute(
                'INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ("C-LEGACY", "Legacy receiver", "Receiver address", "JP", "Tokyo", "Broker A", "TG682", "10:30", "1"),
            )
            connection.execute('CREATE TABLE "MainDB__CUST_NOTE" ("Cust ID" TEXT, "Note Type" TEXT, "Note" TEXT)')
            connection.commit(); connection.close()
            before = source.read_bytes()

            explorer = CustomerExplorer(source, data_dir=root / "state")
            self.assertEqual(1, explorer.backfill_from_snapshot()["accepted"])
            profile = explorer.profile("C-LEGACY")

            self.assertEqual(
                {"order_clean": "1", "order_sticker": "0", "exporter": "APC Export", "commercial": "25345",
                 "hc_exporter_address": "Exporter address", "awb_formula_type": "2", "awb_rate": "37.5", "awb_charges": "150"},
                {key: profile["export_config"][key] for key in ("order_clean", "order_sticker", "exporter", "commercial", "hc_exporter_address", "awb_formula_type", "awb_rate", "awb_charges")},
            )
            self.assertEqual(
                {"consignee_address": "Receiver address", "broker": "Broker A", "flight": "TG682", "time": "10:30", "hc_set_2": "1"},
                {key: profile["consignees"][0][key] for key in ("consignee_address", "broker", "flight", "time", "hc_set_2")},
            )
            self.assertNotIn("discount", repr(profile).casefold())
            self.assertNotIn("currency", repr(profile).casefold())
            self.assertEqual(before, source.read_bytes())

    def test_refresh_updates_untouched_source_fields_but_never_core_edits_children_or_archive_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            explorer = CustomerExplorer(source, data_dir=root / "state")
            explorer.backfill_from_snapshot()
            explorer.edit("C-001", {"name": "Core customer name"}, "YIM")
            explorer.add_consignee("C-001", {"consignee": "Core consignee", "country": "TH"}, "YIM")
            explorer.add_note("C-001", "order", "Core order note", "YIM")

            refreshed_source = root / "accepted-customers-refresh.sqlite"
            refreshed_source.write_bytes(source.read_bytes())
            connection = sqlite3.connect(refreshed_source)
            connection.execute('UPDATE "MainDB__CUST" SET "Name"=?, "Tel"=? WHERE "Cust ID"=?', ("New source name", "02999999", "C-001"))
            connection.execute('UPDATE "MainDB__CUST_CONSIGNEE" SET "Country"=? WHERE "Cust ID"=? AND "Consignee"=?', ("US", "C-001", "Pacific Imports"))
            connection.commit(); connection.close()
            explorer.close()
            explorer = CustomerExplorer(refreshed_source, data_dir=root / "state")
            explorer.refresh_from_snapshot()
            profile = explorer.profile("C-001")
            self.assertEqual("Core customer name", profile["customer"]["name"])
            self.assertEqual("02999999", profile["customer"]["tel"])
            self.assertEqual(hashlib.sha256(refreshed_source.read_bytes()).hexdigest(), profile["customer"]["source_artifact_sha256"])
            self.assertEqual(str(refreshed_source), profile["customer"]["source_artifact_path"])
            self.assertEqual("US", profile["consignees"][0]["country"])
            self.assertIn("Core consignee", [row["consignee"] for row in profile["consignees"]])
            self.assertIn("Core order note", [row["body"] for row in profile["order_notes"]])

            explorer.archive("C-001", "BIAS")
            explorer.refresh_from_snapshot()
            self.assertEqual([], explorer.search()["customers"])
            archived = explorer.profile("C-001", include_archived=True)["customer"]
            self.assertTrue(archived["archived"])

    def test_normal_customer_reads_report_reconciliation_required_without_bulk_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            explorer = CustomerExplorer(source, data_dir=root / "state")

            self.assertEqual(
                {"state": "reconciliation_required", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                explorer.reconciliation_status(),
            )
            with patch.object(explorer, "backfill_from_snapshot", side_effect=AssertionError("unexpected bulk rebuild")):
                self.assertEqual([], explorer.search()["customers"])
                with self.assertRaisesRegex(ValueError, "unknown customer"):
                    explorer.profile("C-001")
                with self.assertRaisesRegex(ValueError, "unknown customer"):
                    explorer.order_entry_note_panel("C-001")
            self.assertEqual(
                {"state": "reconciliation_required", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                explorer.reconciliation_status(),
            )

            explorer.backfill_from_snapshot()
            self.assertEqual(
                {"state": "ready", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                explorer.reconciliation_status(),
            )

            later = root / "accepted-customers-later.sqlite"
            later.write_bytes(source.read_bytes())
            connection = sqlite3.connect(later)
            connection.execute('UPDATE "MainDB__CUST" SET "Name"=? WHERE "Cust ID"=?', ("Later source name", "C-001"))
            connection.commit(); connection.close()
            restarted = CustomerExplorer(later, data_dir=root / "state")

            with patch.object(restarted, "backfill_from_snapshot", side_effect=AssertionError("unexpected bulk rebuild")):
                self.assertEqual("Pacific Plants", restarted.search()["customers"][0]["name"])
            self.assertEqual(
                {"state": "reconciliation_required", "source_sha256": hashlib.sha256(later.read_bytes()).hexdigest()},
                restarted.reconciliation_status(),
            )

    def test_restart_requires_explicit_reconciliation_before_later_snapshot_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = self.make_snapshot(
                root,
                [
                    ("C-001", "Pacific Plants", "Bangkok", "02000001", "02000002", "ops@example.test", "EU", "B", "APC", "1", "2026"),
                    ("C-REMOVED", "Removed source customer", "", "", "", "", "", "", "", "", ""),
                ],
            )
            first = CustomerExplorer(initial, data_dir=root / "state")
            self.assertEqual(2, first.backfill_from_snapshot()["accepted"])
            self.assertEqual(2, first.search()["total"])
            first.edit("C-001", {"name": "Core-edited Pacific"}, "YIM")
            first.archive("C-001", "YIM")
            first.close()

            later = root / "accepted-customers-later.sqlite"
            later.write_bytes(initial.read_bytes())
            connection = sqlite3.connect(later)
            connection.execute('UPDATE "MainDB__CUST" SET "Name"=?, "Tel"=? WHERE "Cust ID"=?', ("Later source name", "02999999", "C-001"))
            connection.execute('DELETE FROM "MainDB__CUST" WHERE "Cust ID"=?', ("C-REMOVED",))
            connection.execute(
                'INSERT INTO "MainDB__CUST" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ("C-NEW", "New accepted customer", "Chiang Mai", "03000000", "", "new@example.test", "TH", "A", "APC", "2", "2027"),
            )
            connection.commit(); connection.close()

            restarted = CustomerExplorer(later, data_dir=root / "state")
            self.assertEqual("reconciliation_required", restarted.reconciliation_status()["state"])
            self.assertEqual(["C-REMOVED"], [customer["customer_id"] for customer in restarted.search()["customers"]])
            self.assertEqual(2, restarted.refresh_from_snapshot()["accepted"])
            customers = restarted.search()["customers"]
            archived = restarted.profile("C-001", include_archived=True)["customer"]
            removed = restarted.profile("C-REMOVED", include_archived=True)["customer"]

            self.assertEqual(["C-NEW"], [customer["customer_id"] for customer in customers])
            self.assertEqual("Core-edited Pacific", archived["name"])
            self.assertEqual("02999999", archived["tel"])
            self.assertTrue(archived["archived"])
            self.assertEqual(str(later), archived["source_artifact_path"])
            self.assertEqual(hashlib.sha256(later.read_bytes()).hexdigest(), archived["source_artifact_sha256"])
            self.assertTrue(removed["archived"])

    def test_create_edit_child_and_note_mutations_require_active_actor_and_append_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explorer = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            explorer.backfill_from_snapshot()
            with self.assertRaises(ValueError):
                explorer.create({"customer_id": "CORE-001", "name": "Core"}, "NOPE")
            created = explorer.create({"customer_id": "CORE-001", "name": "Core customer", "email": "core@example.test"}, "YIM")
            self.assertTrue(created["core_created"])
            with self.assertRaises(ValueError):
                explorer.edit("CORE-001", {"customer_id": "MOVED"}, "YIM")
            explorer.edit("CORE-001", {"tel": "02123456"}, "YIM")
            explorer.add_consignee("CORE-001", {"consignee": "Core receiver", "country": "TH"}, "YIM")
            explorer.add_note("CORE-001", "invoice", "Invoice instruction", "YIM")
            activity = explorer.activity()
            self.assertEqual(["YIM"] * 4, [entry["actor_username"] for entry in activity])
            self.assertEqual(["created", "edit", "consignee_created", "note_created"], [entry["action"] for entry in activity])

    def test_quarantine_is_persistent_and_excludes_duplicate_blank_and_orphan_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root, [("C-DUP", "First", "", "", "", "", "", "", "", "", ""), ("C-DUP", "Second", "", "", "", "", "", "", "", "", ""), ("", "Blank", "", "", "", "", "", "", "", "", "")])
            connection = sqlite3.connect(source)
            connection.execute('INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?, ?, ?, ?, ?)', ("MISSING", "Orphan", "TH", "", "", ""))
            connection.commit(); connection.close()
            explorer = CustomerExplorer(source, data_dir=root / "state")
            summary = explorer.backfill_from_snapshot()

            self.assertEqual({"accepted": 0, "duplicate": 2, "unmatched": 6, "malformed": 0, "preserved": 0}, summary)
            self.assertEqual([], explorer.search()["customers"])
            reasons = [q["reason"] for q in explorer.quarantine()]
            self.assertEqual(2, reasons.count("duplicate_customer_id"))
            self.assertIn("blank_customer_id", reasons)
            self.assertIn("orphan_consignee", reasons)
    def test_customer_ui_and_loopback_api_are_connected_without_pricing(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            customers.backfill_from_snapshot()
            items_source = root / "items.sqlite"
            connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-001", "Item", "สินค้า", "Fish", "Fish")')
            connection.commit(); connection.close()
            handler = make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/")
                response = connection.getresponse(); html = response.read().decode("utf-8"); connection.close()
                self.assertEqual(200, response.status)
                for marker in ("Customer Explorer", "Basic", "Additional", "sticky", "Consignees", "Core-owned", "Order Entry side-panel contract"):
                    self.assertIn(marker, html)
                self.assertIn("Note - Order", html)
                self.assertIn("Note - Invoice", html)
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/api/customers?q=pacific")
                response = connection.getresponse(); body = json.loads(response.read()); connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual("C-001", body["customers"][0]["customer_id"])
            finally:
                server.shutdown(); server.server_close()
    def test_customer_master_uses_basic_additional_tabs_and_exposes_note_side_panel_contract(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            customers.backfill_from_snapshot()
            items_source = root / "items.sqlite"
            connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")')
            connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/")
                response = connection.getresponse(); html = response.read().decode("utf-8"); connection.close()
                self.assertEqual(200, response.status)
                self.assertIn("[['Basic',basic],['Additional',additional],['Note - Order',noteOrder],['Note - Invoice',noteInvoice]]", html)
                self.assertIn("setAttribute('role','tab')", html)
                self.assertIn("Note - Order", html)
                self.assertIn("Note - Invoice", html)
                self.assertIn("Order Entry side-panel contract", html)

                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/api/customers/C-001/order-entry-notes")
                response = connection.getresponse(); panel = json.loads(response.read()); connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual("C-001", panel["customer_id"])
                self.assertEqual("Pacific Plants", panel["customer_name"])
                self.assertEqual(["Order note"], [note["body"] for note in panel["order_notes"]])
                self.assertEqual(["Invoice note"], [note["body"] for note in panel["invoice_notes"]])
                self.assertEqual("future_order_entry_side_panel", panel["consumption"])
            finally:
                server.shutdown(); server.server_close()

    def test_customer_edit_manages_order_and_invoice_notes_and_side_panel_reflects_customer_scoped_audited_writes(self):
        """Customer Edit owns both note workflows; the Order side-panel reads those same Core records."""
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            customers.backfill_from_snapshot()
            items_source = root / "items.sqlite"
            connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")')
            connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/")
                response = connection.getresponse(); html = response.read().decode("utf-8"); connection.close()
                self.assertEqual(200, response.status)
                self.assertIn("[['Basic',basic],['Additional',additional],['Note - Order',noteOrder],['Note - Invoice',noteInvoice]]", html)
                self.assertIn("Manage Order Notes", html)
                self.assertIn("Manage Invoice Notes", html)
                self.assertIn("edit ? actions.append(noteActions(c.customer_id))", html)
                self.assertIn("function notePanel(", html)
                self.assertIn("panel.dataset.tab='note-'+kind", html)
                self.assertNotIn("Order Entry UI", html)

                connection = HTTPConnection(host, port, timeout=3)
                connection.request(
                    "POST", "/customers/api/customers/C-001/notes",
                    json.dumps({"kind": "invoice", "body": "Customer edit invoice note", "actor": "YIM"}),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse(); response.read(); connection.close()
                self.assertEqual(200, response.status)

                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/api/customers/C-001/order-entry-notes")
                response = connection.getresponse(); panel = json.loads(response.read()); connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual(["Order note"], [row["body"] for row in panel["order_notes"]])
                self.assertEqual(["Invoice note", "Customer edit invoice note"], [row["body"] for row in panel["invoice_notes"]])
                activity = customers.activity("C-001")[-1]
                self.assertEqual("note_created", activity["action"])
                self.assertEqual("YIM", activity["actor_username"])
                self.assertEqual({"kind": "invoice", "body": "Customer edit invoice note"}, activity["after"])
            finally:
                server.shutdown(); server.server_close()

    def test_refresh_archives_removed_source_children_without_touching_core_created_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            explorer = CustomerExplorer(source, data_dir=root / "state")
            explorer.backfill_from_snapshot()
            explorer.add_consignee("C-001", {"consignee": "Core receiver", "country": "TH"}, "YIM")
            explorer.add_note("C-001", "order", "Core order note", "YIM")
            refreshed = root / "accepted-customers-refresh.sqlite"
            refreshed.write_bytes(source.read_bytes())
            connection = sqlite3.connect(refreshed)
            connection.execute('DELETE FROM "MainDB__CUST_CONSIGNEE" WHERE "Cust ID"=?', ("C-001",))
            connection.execute('DELETE FROM "MainDB__CUST_NOTE" WHERE "Cust ID"=?', ("C-001",))
            connection.commit(); connection.close()
            explorer.close()
            explorer = CustomerExplorer(refreshed, data_dir=root / "state")
            explorer.refresh_from_snapshot()
            profile = explorer.profile("C-001")
            self.assertEqual(["Core receiver"], [row["consignee"] for row in profile["consignees"]])
            self.assertEqual(["Core order note"], [row["body"] for row in profile["order_notes"]])
            self.assertEqual([], profile["invoice_notes"])

    def test_refresh_removes_disappeared_source_export_config_without_reviving_a_core_archived_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            explorer = CustomerExplorer(source, data_dir=root / "state")
            explorer.backfill_from_snapshot()
            refreshed = root / "accepted-customers-refresh.sqlite"
            refreshed.write_bytes(source.read_bytes())
            connection = sqlite3.connect(refreshed)
            connection.execute('DELETE FROM "MainDB__CUST_CON" WHERE "Cust ID"=?', ("C-001",))
            connection.commit(); connection.close()
            explorer.close()
            explorer = CustomerExplorer(refreshed, data_dir=root / "state")
            explorer.refresh_from_snapshot()
            self.assertEqual("", explorer.profile("C-001")["export_config"]["exporter"])

            explorer.edit_export_config("C-001", {"exporter": "Core export"}, "YIM")
            explorer.archive_export_config("C-001", "BIAS")
            explorer.refresh_from_snapshot()
            self.assertEqual("", explorer.profile("C-001")["export_config"]["exporter"])
            self.assertEqual(
                ["export_config_edited", "export_config_archived"],
                [row["action"] for row in explorer.activity("C-001")][-2:],
            )

    def test_customer_child_mutation_api_edits_and_archives_config_consignee_and_note_with_actor(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            items_source = root / "items.sqlite"; connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)'); connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")'); connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                requests = [
                    ("/customers/api/customers/C-001/export-config", {"exporter": "Core Export", "actor": "YIM"}),
                    ("/customers/api/customers/C-001/consignees/1", {"country": "US", "actor": "YIM"}),
                    ("/customers/api/customers/C-001/notes/1", {"body": "Revised order note", "actor": "YIM"}),
                    ("/customers/api/customers/C-001/export-config/archive", {"actor": "BIAS"}),
                    ("/customers/api/customers/C-001/consignees/1/archive", {"actor": "BIAS"}),
                    ("/customers/api/customers/C-001/notes/1/archive", {"actor": "BIAS"}),
                ]
                for path, payload in requests:
                    connection = HTTPConnection(host, port, timeout=3)
                    connection.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
                    response = connection.getresponse(); response_body = response.read().decode("utf-8"); connection.close()
                    self.assertEqual(200, response.status, f"{path}: {response_body}")
                profile = customers.profile("C-001")
                self.assertEqual("", profile["export_config"]["exporter"])
                self.assertEqual([], profile["consignees"])
                self.assertEqual([], profile["order_notes"])
                self.assertEqual(
                    ["export_config_edited", "consignee_edited", "note_edited", "export_config_archived", "consignee_archived", "note_archived"],
                    [row["action"] for row in customers.activity("C-001")],
                )
            finally:
                server.shutdown(); server.server_close()

    def test_refresh_archives_disappeared_source_customer_and_persists_duplicate_quarantine_without_revival(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            explorer = CustomerExplorer(source, data_dir=root / "state")
            explorer.backfill_from_snapshot()

            missing = root / "accepted-customers-missing.sqlite"
            missing.write_bytes(source.read_bytes())
            connection = sqlite3.connect(missing)
            connection.execute('DELETE FROM "MainDB__CUST" WHERE "Cust ID"=?', ("C-001",))
            connection.execute('DELETE FROM "MainDB__CUST_CON" WHERE "Cust ID"=?', ("C-001",))
            connection.execute('DELETE FROM "MainDB__CUST_CONSIGNEE" WHERE "Cust ID"=?', ("C-001",))
            connection.execute('DELETE FROM "MainDB__CUST_NOTE" WHERE "Cust ID"=?', ("C-001",))
            connection.commit(); connection.close()
            explorer.close()
            explorer = CustomerExplorer(missing, data_dir=root / "state")
            explorer.refresh_from_snapshot()
            self.assertEqual([], explorer.search()["customers"])
            self.assertTrue(explorer.profile("C-001", include_archived=True)["customer"]["archived"])

            duplicate = root / "accepted-customers-duplicate.sqlite"
            duplicate.write_bytes(source.read_bytes())
            connection = sqlite3.connect(duplicate)
            connection.execute('INSERT INTO "MainDB__CUST" SELECT * FROM "MainDB__CUST" WHERE "Cust ID"=?', ("C-001",))
            connection.commit(); connection.close()
            explorer.close()
            explorer = CustomerExplorer(duplicate, data_dir=root / "state")
            explorer.refresh_from_snapshot()
            self.assertEqual([], explorer.search()["customers"])
            self.assertIn({"customer_id": "C-001", "reason": "duplicate_customer_id"}, explorer.quarantine())
            explorer.refresh_from_snapshot()
            self.assertIn({"customer_id": "C-001", "reason": "duplicate_customer_id"}, explorer.quarantine())

    def test_refresh_quarantines_source_id_collision_with_core_customer_without_attaching_source_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            for table in ("MainDB__CUST", "MainDB__CUST_CON", "MainDB__CUST_CONSIGNEE", "MainDB__CUST_NOTE"):
                connection.execute(f'DELETE FROM "{table}"')
            connection.commit(); connection.close()
            explorer = CustomerExplorer(source, data_dir=root / "state")
            explorer.backfill_from_snapshot()
            explorer.create({"customer_id": "C-001", "name": "Core customer"}, "YIM")
            explorer.add_consignee("C-001", {"consignee": "Core receiver", "country": "TH"}, "YIM")
            explorer.add_note("C-001", "invoice", "Core invoice note", "YIM")

            collision = root / "accepted-customers-collision.sqlite"
            collision.write_bytes(source.read_bytes())
            connection = sqlite3.connect(collision)
            connection.execute('INSERT INTO "MainDB__CUST" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', ("C-001", "Source customer", "Bangkok", "02000001", "", "", "EU", "B", "APC", "1", "2026"))
            connection.execute('INSERT INTO "MainDB__CUST_CON" VALUES (?, ?, ?, ?, ?, ?)', ("C-001", "Source export", "Commercial", "order", "hc", "awb"))
            connection.execute('INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?, ?, ?, ?, ?)', ("C-001", "Source receiver", "JP", "Tokyo", "Broker", "TG682"))
            connection.execute('INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)', ("C-001", "Order", "Source order note"))
            connection.commit(); connection.close()
            explorer.close()
            explorer = CustomerExplorer(collision, data_dir=root / "state")
            explorer.refresh_from_snapshot()
            profile = explorer.profile("C-001")
            self.assertTrue(profile["customer"]["core_created"])
            self.assertEqual("Core customer", profile["customer"]["name"])
            self.assertEqual(["Core receiver"], [row["consignee"] for row in profile["consignees"]])
            self.assertEqual(["Core invoice note"], [row["body"] for row in profile["invoice_notes"]])
            self.assertEqual("", profile["export_config"]["exporter"])
            self.assertIn({"customer_id": "C-001", "reason": "source_collision_core_created"}, explorer.quarantine())

    def test_customer_child_creation_api_adds_core_owned_consignee_and_note_with_actor_audit(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            items_source = root / "items.sqlite"; connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)'); connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")'); connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                for path, payload in (
                    ("/customers/api/customers/C-001/consignees", {"consignee": "API receiver", "country": "TH", "actor": "YIM"}),
                    ("/customers/api/customers/C-001/notes", {"kind": "order", "body": "API order note", "actor": "BIAS"}),
                ):
                    connection = HTTPConnection(host, port, timeout=3)
                    connection.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
                    response = connection.getresponse(); response_body = response.read().decode("utf-8"); connection.close()
                    self.assertEqual(200, response.status, f"{path}: {response_body}")
                profile = customers.profile("C-001")
                self.assertIn("API receiver", [row["consignee"] for row in profile["consignees"]])
                self.assertIn("API order note", [row["body"] for row in profile["order_notes"]])
                self.assertEqual(["consignee_created", "note_created"], [row["action"] for row in customers.activity("C-001")][-2:])
                self.assertEqual(["YIM", "BIAS"], [row["actor_username"] for row in customers.activity("C-001")][-2:])
            finally:
                server.shutdown(); server.server_close()

    def test_customer_reconciliation_status_ui_is_read_only(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            items_source = root / "items.sqlite"
            connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")')
            connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                with patch.object(customers, "backfill_from_snapshot", side_effect=AssertionError("status UI must not reconcile")):
                    connection = HTTPConnection(host, port, timeout=3)
                    connection.request("GET", "/customers/api/reconciliation-status")
                    response = connection.getresponse(); payload = json.loads(response.read()); connection.close()
                    self.assertEqual(200, response.status)
                    self.assertEqual("reconciliation_required", payload["state"])

                    connection = HTTPConnection(host, port, timeout=3)
                    connection.request("GET", "/customers/")
                    response = connection.getresponse(); html = response.read().decode("utf-8"); connection.close()
                    self.assertEqual(200, response.status)
                    self.assertIn('id="reconciliation-status"', html)
                    self.assertIn("api/reconciliation-status", html)
                    self.assertIn("Data check required", html)
                    self.assertNotIn("Reconcile now", html)
            finally:
                server.shutdown(); server.server_close()

    def test_root_customer_card_links_to_customer_explorer(self):
        source = (Path(__file__).resolve().parents[1] / "apc_core" / "item_explorer.py").read_text(encoding="utf-8")
        self.assertIn('href="customers/"', source)
        self.assertNotIn('href="/customers/"', source)
        self.assertNotIn('Customer Explorer</h2><p>Coming soon.', source)

    def test_customer_editor_ui_uses_active_staff_and_real_customer_mutation_paths(self):
        """Customer editing stays within Basic/Additional and never becomes Order UI."""
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            customers.backfill_from_snapshot()
            items_source = root / "items.sqlite"
            connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")')
            connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/")
                response = connection.getresponse(); html = response.read().decode("utf-8"); connection.close()
                self.assertEqual(200, response.status)
                self.assertIn("setAttribute('role','tablist')", html)
                self.assertIn("[['Basic',basic],['Additional',additional],['Note - Order',noteOrder],['Note - Invoice',noteInvoice]]", html)
                self.assertNotIn('id="active-staff"', html)
                self.assertIn("function activeStaff(){return window.apcCoreActiveStaff||''}", html)
                self.assertIn('id="new-customer"', html)
                self.assertIn('>Add customer<', html)
                for marker in (
                    "button('edit-customer'", "button('save-customer'", "button('archive-customer'", "button('cancel-customer'",
                    'id="note-manager"', 'Manage customer notes', 'aria-live="polite"',
                    "api/customers/'+encodeURIComponent(id)",
                    "api/customers/'+encodeURIComponent(id)+'/export-config",
                    "api/customers/'+encodeURIComponent(id)+'/consignees",
                    "api/customers/'+encodeURIComponent(id)+'/notes",
                    "actor:activeStaff()",
                    'fetch("api/staff")',
                ):
                    self.assertIn(marker, html)
                self.assertIn("function notePanel(", html)
                self.assertIn("panel.dataset.tab='note-'+kind", html)
                self.assertIn('aria-modal="true"', html)
                self.assertIn("lastNotesTrigger", html)
                self.assertIn("event.key==='Escape'", html)
                self.assertIn("if(!edit){actions.querySelectorAll", html)
                self.assertIn("cfg.querySelectorAll('input').forEach", html)
                self.assertIn("[additional,noteOrder,noteInvoice].forEach", html)
                self.assertIn("event.key==='Tab'", html)
                self.assertIn("document.activeElement===last", html)
                self.assertIn("commitCustomerCode", html)
                self.assertIn("await load()", html)
                self.assertIn("Customer Type", html)
                self.assertIn("Grower", html)
                self.assertIn("Wholeseller", html)
                self.assertIn("Retail", html)
                self.assertIn("card.dataset.choiceField=field", html)
                self.assertIn("customer-code", html)
                self.assertIn("customer-name", html)
                self.assertIn("customer-type", html)
                self.assertIn("grid-template-columns:72px minmax(0,1fr) 72px", html)
                self.assertIn("customer-list-header", html)
                self.assertIn(".customer-list-header{position:sticky;top:58px", html)
                self.assertIn("appearance:auto;width:16px;height:16px;padding:0", html)
                self.assertIn("kind=el('span',c.price_type||'—')", html)
                self.assertIn("event.key==='Tab'", html)
                self.assertEqual(250, CustomerExplorer.search.__defaults__[1])
                self.assertIn('"has_more"', Path("apc_core/customer_explorer.py").read_text())
                self.assertIn('"has_more"', Path("apc_core/item_explorer.py").read_text())
                self.assertIn('"has_more"', Path("apc_core/customer_price_module.py").read_text())
                self.assertNotIn("Customer data matches accepted artifact.", html)
                self.assertNotIn("Reconciliation required", html)
                self.assertNotIn('Order Entry UI', html)

                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/api/staff")
                response = connection.getresponse(); body = json.loads(response.read()); connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual(
                    [{"username": "BIAS", "role": "Admin"}, {"username": "BON", "role": "Editor"},
                     {"username": "DERRICK", "role": "Admin"}, {"username": "WAT", "role": "Editor"},
                     {"username": "YA", "role": "Editor"}, {"username": "YIM", "role": "Editor"}],
                    body["staff"],
                )
            finally:
                server.shutdown(); server.server_close()

    def test_customer_editor_ui_exposes_safe_consignee_and_note_edit_archive_lifecycles(self):
        """Existing Core-owned children have accessible edit/archive controls, not API-only paths."""
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            customers.backfill_from_snapshot()
            items_source = root / "items.sqlite"
            connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)')
            connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")')
            connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/")
                response = connection.getresponse(); html = response.read().decode("utf-8"); connection.close()
                self.assertEqual(200, response.status)
                for marker in (
                    "Edit consignee", "Archive consignee", "Edit '+label+' note", "Archive '+label+' note",
                    "consigneePath(customerId)+'/'+encodeURIComponent(row.id)", "notePath(customerId)+'/'+encodeURIComponent(note.id)",
                    "'/archive'", "actor:activeStaff()", "prompt('Consignee'", "prompt('Note body'",
                ):
                    self.assertIn(marker, html)
                self.assertNotIn('innerHTML', html)
                self.assertIn("function notePanel(", html)
                self.assertIn("panel.dataset.tab='note-'+kind", html)
                self.assertNotIn('Order Entry UI', html)
            finally:
                server.shutdown(); server.server_close()

    def test_customer_routes_require_an_exact_customers_path_segment(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            items_source = root / "items.sqlite"; connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)'); connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")'); connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/customers/api/customersX")
                self.assertEqual(404, connection.getresponse().status); connection.close()
            finally:
                server.shutdown(); server.server_close()

    def test_customer_mutation_api_forwards_only_core_owned_actions_with_actor(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        from apc_core.item_explorer import ItemExplorer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); customers = CustomerExplorer(self.make_snapshot(root), data_dir=root / "state")
            items_source = root / "items.sqlite"; connection = sqlite3.connect(items_source)
            connection.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT)'); connection.execute('INSERT INTO "MainDB__ITEM" VALUES ("IT-1")'); connection.commit(); connection.close()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ItemExplorer(items_source, data_dir=root / "state"), {"accepted": True}, customer_explorer=customers))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                host, port = server.server_address
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("POST", "/customers/api/customers", json.dumps({"customer_id":"CORE-API","name":"API customer","actor":"YIM"}), {"Content-Type":"application/json"})
                response = connection.getresponse(); body = json.loads(response.read()); connection.close()
                self.assertEqual(201, response.status)
                self.assertTrue(body["customer"]["core_created"])
                connection = HTTPConnection(host, port, timeout=3)
                connection.request("POST", "/customers/api/customers/CORE-API", json.dumps({"tel":"02123456","actor":"YIM"}), {"Content-Type":"application/json"})
                self.assertEqual(200, connection.getresponse().status); connection.close()
            finally:
                server.shutdown(); server.server_close()
    def test_customer_mutation_routes_require_explicit_direct_private_lan_policy(self):
        source = (Path(__file__).resolve().parents[1] / "apc_core" / "item_explorer.py").read_text(encoding="utf-8")
        self.assertIn('customer_path == "/customers/api/customers" or customer_path.startswith("/customers/api/customers/")', source)
        self.assertIn("_customer_client_allowed(self.client_address[0], customer_lan_ingress)", source)
        self.assertIn("ipaddress.ip_network(\"10.0.0.0/8\")", source)
        self.assertNotIn("X-Forwarded-For", source)

    def test_duplicate_export_config_quarantines_and_excludes_all_source_config_without_touching_other_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute('INSERT INTO "MainDB__CUST_CON" VALUES (?, ?, ?, ?, ?, ?)', ("C-001", "Duplicate export", "Commercial", "order", "hc", "awb"))
            connection.commit(); connection.close()
            explorer = CustomerExplorer(source, data_dir=root / "state")

            explorer.backfill_from_snapshot()
            profile = explorer.profile("C-001")

            self.assertEqual("", profile["export_config"]["exporter"])
            self.assertEqual(["Pacific Imports"], [row["consignee"] for row in profile["consignees"]])
            self.assertIn({"customer_id": "C-001", "reason": "duplicate_config"}, explorer.quarantine())
            explorer.refresh_from_snapshot()
            self.assertEqual("", explorer.profile("C-001")["export_config"]["exporter"])

    def test_duplicate_consignee_key_quarantines_and_excludes_all_source_consignees_without_touching_other_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute('INSERT INTO "MainDB__CUST_CONSIGNEE" VALUES (?, ?, ?, ?, ?, ?)', ("C-001", "PACIFIC IMPORTS", "US", "", "", ""))
            connection.commit(); connection.close()
            explorer = CustomerExplorer(source, data_dir=root / "state")

            explorer.backfill_from_snapshot()
            profile = explorer.profile("C-001")

            self.assertEqual([], profile["consignees"])
            self.assertEqual("APC Export", profile["export_config"]["exporter"])
            self.assertEqual(["Order note"], [note["body"] for note in profile["order_notes"]])
            self.assertIn({"customer_id": "C-001", "reason": "duplicate_consignee"}, explorer.quarantine())
            explorer.refresh_from_snapshot()
            self.assertEqual([], explorer.profile("C-001")["consignees"])

    def test_duplicate_note_kind_and_body_quarantines_and_excludes_all_source_notes_without_touching_other_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.make_snapshot(root)
            connection = sqlite3.connect(source)
            connection.execute('INSERT INTO "MainDB__CUST_NOTE" VALUES (?, ?, ?)', ("C-001", "ORDER", "Order note"))
            connection.commit(); connection.close()
            explorer = CustomerExplorer(source, data_dir=root / "state")

            explorer.backfill_from_snapshot()
            profile = explorer.profile("C-001")

            self.assertEqual([], profile["order_notes"])
            self.assertEqual([], profile["invoice_notes"])
            self.assertEqual(["Pacific Imports"], [row["consignee"] for row in profile["consignees"]])
            self.assertIn({"customer_id": "C-001", "reason": "duplicate_note"}, explorer.quarantine())
            explorer.refresh_from_snapshot()
            self.assertEqual([], explorer.profile("C-001")["order_notes"])


if __name__ == "__main__":
    unittest.main()
