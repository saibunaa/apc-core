import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from apc_core.awb_explorer import AWBExplorer, ReadOnlySourceContractError, html
from apc_core.item_explorer import ItemExplorer, make_handler


AWB_DEFINITION = (
    '"Inv No" TEXT, "AWB" TEXT, "AWB Date" TEXT, "shipby" TEXT, "AWB Box" TEXT, "Province" TEXT, '
    '"Weight" TEXT, "RATE" TEXT, "Agent" TEXT, "Carrier" TEXT, "Total THB" TEXT, "Total US" TEXT, "exRate" TEXT'
)
# The KG/2026/004 row is the screenshot-confirmed sample the whole module is
# calibrated against: 294 x 251 = 73,794; +720+300 = 74,814; /29.85 = 2,506.33;
# +190 Cargo = 2,696.33 stored.
KG = ("KG/2026/004", "180-2002 8783", "2026-02-16", "KE652-KE017", "34", "LOS ANGELES",
      "294.00", "251.00", "720.00", "300.00", "74814.00", "2696.33", "29.85")
# Within the legacy band and with a stored total: hidden by the anomaly view.
JK = ("JK/2026/007", "217-0733 1120", "2026-02-15", "TG910", "3", "FRANKFURT",
      "45.0", "239.00", "0", "0", "12505.00", "608.00", "32.00")
# No weight, no rate, no exchange rate: the calculation cannot run at all.
MRA = ("MRA/2026/017", "555-1587 4410", "2026-08-29", "SU273", "24", "MOSCOW",
       "", "", "", "", "", "", "0")
# Identity is incomplete: no AWB number, so no money may be shown.
NOAWB = ("XX/2026/001", "", "2026-03-01", "CX700", "2", "HONG KONG",
         "10.0", "100.00", "0", "0", "1000.00", "0", "31.00")


class AWBExplorerTests(unittest.TestCase):
    """Fixture mirrors the legacy AWB schema recovered from frmAWB/frmAWBList."""

    def make_snapshot(self, root: Path, *, missing_awb: bool = False, missing_column: bool = False,
                      aliased: bool = False, charges: bool = True) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        source = root / "accepted-awb.sqlite"
        con = sqlite3.connect(source)
        definition = AWB_DEFINITION
        if missing_column:
            definition = definition.replace('"AWB" TEXT, ', "")
        if aliased:
            definition = definition.replace('"Inv No"', '"Invoice No"').replace('"AWB" TEXT', '"AWB No" TEXT')
        if not missing_awb:
            con.execute(f'CREATE TABLE "MainDB__AWB" ({definition})')
            if not missing_column:
                placeholders = ", ".join("?" * 13)
                con.executemany(f'INSERT INTO "MainDB__AWB" VALUES ({placeholders})', [KG, JK, MRA, NOAWB])
        con.execute('CREATE TABLE "MainDB__CUST" ("Cust ID" TEXT, "Name" TEXT)')
        con.executemany('INSERT INTO "MainDB__CUST" VALUES (?, ?)',
                        [("KG", "Kagoshima Aqua"), ("JK", "Jakarta Plants"), ("MRA", "Mira Trading"), ("XX", "Unknown Co")])
        con.execute('CREATE TABLE "MainDB__CUST_CON" ("Cust ID" TEXT, "Charges" TEXT)')
        if charges:
            con.executemany('INSERT INTO "MainDB__CUST_CON" VALUES (?, ?)', [("KG", "190.00"), ("JK", "150.00")])
        con.commit()
        con.close()
        return source

    def explorer(self, tmp: str, **kwargs) -> AWBExplorer:
        explorer = AWBExplorer(self.make_snapshot(Path(tmp), **kwargs))
        self.addCleanup(explorer.close)
        return explorer

    @staticmethod
    def line(payload: dict, label: str) -> dict:
        return next(item for item in payload["freight"] if item["label"] == label)

    # ---- schema contract ------------------------------------------------

    def test_absent_awb_table_is_refused_so_the_module_can_be_skipped_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ReadOnlySourceContractError):
                AWBExplorer(self.make_snapshot(Path(tmp), missing_awb=True))

    def test_missing_identity_column_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ReadOnlySourceContractError):
                AWBExplorer(self.make_snapshot(Path(tmp), missing_column=True))

    def test_alternative_legacy_column_spellings_still_open(self):
        """Column names are recovered from VB6, never verified against a real
        export, so resolution is by alias rather than one asserted spelling."""
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer(tmp, aliased=True)
            found = explorer.search_shipments(anomaly_only=False)
            self.assertEqual(4, found["total"])
            self.assertIn("KG/2026/004", [row["invoice_no"] for row in found["shipments"]])

    # ---- the calculation ledger ----------------------------------------

    def test_kg_ledger_reproduces_every_stored_and_recomputed_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.explorer(tmp).open_shipment("KG/2026/004")
            self.assertTrue(payload["identity_complete"])
            self.assertEqual("73794.00", self.line(payload, "Freight subtotal")["recomputed"])
            grand_thb = self.line(payload, "Grand total THB")
            self.assertEqual(("74814.00", "74814.00", True), (grand_thb["stored"], grand_thb["recomputed"], grand_thb["agrees"]))
            cargo = self.line(payload, "Cargo")
            self.assertEqual("190.00", cargo["stored"])
            self.assertIn("Charges", cargo["source"])
            grand_usd = self.line(payload, "Grand total USD")
            self.assertEqual(("2696.33", "2696.33", True), (grand_usd["stored"], grand_usd["recomputed"], grand_usd["agrees"]))

    def test_stored_and_recomputed_never_merge_and_a_disagreement_reports_its_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_snapshot(Path(tmp))
            con = sqlite3.connect(source)
            con.execute('UPDATE "MainDB__AWB" SET "Total US" = ? WHERE "Inv No" = ?', ("2686.33", "KG/2026/004"))
            con.commit()
            con.close()
            explorer = AWBExplorer(source)
            self.addCleanup(explorer.close)
            usd = self.line(explorer.open_shipment("KG/2026/004"), "Grand total USD")
            self.assertEqual("2686.33", usd["stored"])
            self.assertEqual("2696.33", usd["recomputed"])
            self.assertFalse(usd["agrees"])
            self.assertEqual("10.00", usd["delta"])

    def test_zero_exchange_rate_reports_not_computable_rather_than_a_stale_number(self):
        """VB6 CountTotal leaves the previous value in the box; a stale number
        readable as current is exactly what this module must not reproduce."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.explorer(tmp).open_shipment("MRA/2026/017")
            usd = self.line(payload, "Grand total USD")
            self.assertIsNone(usd["recomputed"])
            self.assertIn("exrate-zero", usd["flags"])
            self.assertTrue(any("not computable" in notice for notice in payload["notices"]))

    def test_absent_cargo_charge_refuses_the_legacy_hardcoded_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.explorer(tmp, charges=False).open_shipment("KG/2026/004")
            usd = self.line(payload, "Grand total USD")
            self.assertIsNone(usd["recomputed"])
            self.assertIn("cargo-unknown", usd["flags"])
            self.assertIsNone(self.line(payload, "Cargo")["stored"])

    def test_rate_line_is_marked_unverified_and_no_freight_rule_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.explorer(tmp).open_shipment("KG/2026/004")
            rate = self.line(payload, "Rate")
            self.assertEqual("251.00", rate["stored"])
            self.assertIsNone(rate["recomputed"])
            self.assertIn("rate-rule-not-verified", rate["flags"])

    # ---- identity gating -----------------------------------------------

    def test_incomplete_identity_withholds_every_freight_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.explorer(tmp).open_shipment("XX/2026/001")
            self.assertFalse(payload["identity_complete"])
            self.assertEqual(["awb_no"], payload["missing_identifiers"])
            self.assertEqual([], payload["freight"])
            self.assertNotIn("1000.00", json.dumps(payload))

    def test_customer_is_derived_from_the_invoice_prefix_and_labelled_as_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = self.explorer(tmp).open_shipment("KG/2026/004")["identity"]
            self.assertEqual(("KG", "Kagoshima Aqua", True), (identity["customer_id"], identity["customer_name"], identity["customer_derived"]))

    def test_order_number_is_reported_unmapped_because_no_legacy_link_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = self.explorer(tmp).open_shipment("KG/2026/004")["identity"]
            self.assertIsNone(identity["order_no"])
            self.assertEqual("unmapped", identity["order_no_status"])

    # ---- browse contract ------------------------------------------------

    def test_duplicate_invoice_rows_open_their_own_shipment_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_snapshot(Path(tmp))
            con = sqlite3.connect(source)
            con.execute(
                'INSERT INTO "MainDB__AWB" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ("KG/2026/004", "999-9999 9999", "2026-02-17", "TG999", "35", "TOKYO",
                 "295.00", "252.00", "0", "0", "74340.00", "2680.00", "29.85"),
            )
            con.commit()
            con.close()
            explorer = AWBExplorer(source)
            self.addCleanup(explorer.close)

            rows = [row for row in explorer.search_shipments(anomaly_only=False)["shipments"] if row["invoice_no"] == "KG/2026/004"]
            self.assertEqual(2, len(rows))
            self.assertNotEqual(rows[0]["shipment_id"], rows[1]["shipment_id"])
            details = {explorer.open_shipment_by_id(row["shipment_id"])["identity"]["awb_no"] for row in rows}
            self.assertEqual({"180-2002 8783", "999-9999 9999"}, details)

    def test_anomaly_view_is_on_by_default_and_turning_it_off_only_widens(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer(tmp)
            flagged = explorer.search_shipments()
            everything = explorer.search_shipments(anomaly_only=False)
            self.assertTrue(flagged["anomaly_only"])
            self.assertLess(flagged["total"], everything["total"])
            self.assertNotIn("JK/2026/007", [row["invoice_no"] for row in flagged["shipments"]])

    def test_anomaly_band_is_reported_with_its_origin_and_never_as_settled_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = self.explorer(tmp).search_shipments()
            self.assertEqual(["30", "37"], found["anomaly_band"])
            self.assertIn("not confirmed", found["anomaly_band_origin"])
            reasons = next(row["anomaly_reasons"] for row in found["shipments"] if row["invoice_no"] == "KG/2026/004")
            self.assertEqual(["exrate-below-band"], [reason["code"] for reason in reasons])
            self.assertFalse(reasons[0]["confirmed"])

    def test_text_filters_are_prefix_matches_not_contains(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer(tmp)
            self.assertEqual(1, explorer.search_shipments(invoice_prefix="kg", anomaly_only=False)["total"])
            self.assertEqual(0, explorer.search_shipments(invoice_prefix="2026", anomaly_only=False)["total"])

    def test_unit_is_withheld_because_it_discloses_margin_per_kilo(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.explorer(tmp).search_shipments(anomaly_only=False)["shipments"][0]
            self.assertIsNone(row["unit"])
            self.assertTrue(row["unit_withheld"])

    def test_blank_numerics_stay_blank_and_are_never_rendered_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = next(row for row in self.explorer(tmp).search_shipments()["shipments"] if row["invoice_no"] == "MRA/2026/017")
            self.assertEqual("", row["weight"])
            self.assertEqual("", row["rate"])

    def test_malformed_filters_and_pages_are_rejected_before_reaching_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer(tmp)
            with self.assertRaises(ValueError):
                explorer.search_shipments(invoice_prefix=object())
            with self.assertRaises(ValueError):
                explorer.search_shipments(offset="not-a-page")

    def test_unknown_invoice_and_non_string_lookups_return_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer = self.explorer(tmp)
            self.assertIsNone(explorer.open_shipment("NOPE/1"))
            self.assertIsNone(explorer.open_shipment(None))


class AWBRoutingTests(unittest.TestCase):
    """The page is served read-only; no mutation route exists for it."""

    def item_snapshot(self, root: Path) -> Path:
        source = root / "accepted-item.sqlite"
        con = sqlite3.connect(source)
        con.execute('CREATE TABLE "MainDB__ITEM" ("Item ID" TEXT, "Description" TEXT, "Description TH" TEXT, "Type" TEXT, "Family" TEXT)')
        con.execute('INSERT INTO "MainDB__ITEM" VALUES (?, ?, ?, ?, ?)', ("IT-001", "Anubias", "", "Rhizome", "Araceae"))
        con.commit()
        con.close()
        return source

    def serve(self, tmp: str, awb_explorer):
        explorer = ItemExplorer(self.item_snapshot(Path(tmp)), data_dir=Path(tmp) / "core-state")
        self.addCleanup(explorer.close)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            explorer, {"accepted": True}, None, None, None, awb_explorer))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def test_absent_awb_hides_shipments_and_leaves_routes_unregistered(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, port = self.serve(tmp, None)
            conn = HTTPConnection(host, port, timeout=3)
            conn.request("GET", "/")
            menu = conn.getresponse()
            menu_html = menu.read().decode("utf-8")
            self.assertEqual(200, menu.status)
            self.assertNotIn("Shipments", menu_html)
            self.assertNotIn("Shipment tracking will appear here.", menu_html)
            self.assertNotIn('href="shipments/"', menu_html)

            for route in (
                "/shipments",
                "/shipments/",
                "/shipments/api/shipments",
                "/shipments/api/shipments/KG%2F2026%2F004",
            ):
                conn.request("GET", route)
                response = conn.getresponse()
                self.assertEqual(404, response.status)
                response.read()

    def test_shipment_page_and_api_are_served_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            awb = AWBExplorerTests().explorer(tmp)
            self.addCleanup(awb.close)
            host, port = self.serve(tmp, awb)
            conn = HTTPConnection(host, port, timeout=3)
            conn.request("GET", "/shipments/")
            page = conn.getresponse()
            body = page.read().decode("utf-8")
            self.assertEqual(200, page.status)
            self.assertIn("Total Record(s)", body)

            conn.request("GET", "/shipments/api/shipments?anomaly_only=0")
            payload = json.loads(conn.getresponse().read())
            self.assertEqual(4, payload["total"])
            shipment_id = payload["shipments"][0]["shipment_id"]
            conn.request("GET", f"/shipments/api/shipments/{shipment_id}")
            detail = json.loads(conn.getresponse().read())
            self.assertEqual(payload["shipments"][0]["awb_no"], detail["identity"]["awb_no"])

            conn.request("POST", "/shipments/api/shipments", body=b"{}", headers={"Content-Type": "application/json"})
            self.assertEqual(405, conn.getresponse().status)

    def test_shipment_detail_database_error_is_a_controlled_bad_request(self):
        class BrokenAWB:
            def open_shipment_by_id(self, shipment_id: str):
                raise sqlite3.OperationalError("read failure")

        with tempfile.TemporaryDirectory() as tmp:
            host, port = self.serve(tmp, BrokenAWB())
            conn = HTTPConnection(host, port, timeout=3)
            conn.request("GET", "/shipments/api/shipments/KG%2F2026%2F004")
            response = conn.getresponse()
            self.assertEqual(400, response.status)
            self.assertEqual({"error": "invalid shipment query"}, json.loads(response.read()))

    def test_page_guards_every_write_action_and_offers_no_mutation_endpoint(self):
        body = html()
        self.assertIn("Open shipment", body)
        for action in ("Save", "Delete", "Print", "Print Preview"):
            self.assertIn(action, body)
        self.assertNotIn("method:'POST'", body)
        self.assertNotIn('method: "POST"', body)
        self.assertIn("guarded", body)
        # The legacy Delete key must not reach anything in the grid.
        self.assertNotIn("'Delete'", body)
