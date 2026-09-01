import inspect
import unittest


TEMPORARY_FIXTURE = {
    "document_id": "TMP-014",
    "state": "Temporary",
    "staff_name": "Mali S.",
    "customer_name": "North Star Clinic",
    "evidence_reference": "accepted-snapshot: 2026-09-01T08:30Z",
    "old_system_notes": "Legacy note retained for history only.",
    "lines": [
        {"item": "Specimen tube", "quantity": "12", "price": None, "line_note": "Price not supplied."},
    ],
}

REAL_WITH_HISTORY_FIXTURE = {
    "document_id": "INV-102",
    "state": "Real",
    "permanent_number": "INV-2026-00102",
    "staff_name": "Niran P.",
    "customer_name": "River Health",
    "evidence_reference": "accepted-snapshot: sha256:abc123",
    "old_system_notes": "Converted legacy invoice; display-only history.",
    "replaces": {"document_id": "INV-099", "label": "Temporary invoice"},
    "replaced_by": {"document_id": "INV-103", "label": "Corrected invoice"},
    "lines": [
        {"item": "Diagnostic kit", "quantity": "2", "price": "1,250.00", "line_note": "Customer reference RH-44."},
    ],
}


class TestInvoiceWorkflowUi(unittest.TestCase):
    def test_temporary_invoice_is_a_read_only_accessible_detail(self):
        from apc_core.invoice_workflow_ui import invoice_detail_html

        html = invoice_detail_html(TEMPORARY_FIXTURE)

        for marker in (
            "<title>Invoice</title>",
            '<h1 id="invoice-title">Invoice</h1>',
            "Temporary",
            "No number yet",
            "Mali S.",
            "North Star Clinic",
            "Evidence reference",
            "accepted-snapshot: 2026-09-01T08:30Z",
            "Old system notes (history only)",
            "Legacy note retained for history only.",
            "<table>",
            "<th scope=\"col\">Item</th>",
            "<th scope=\"col\">Quantity</th>",
            "<th scope=\"col\">Price</th>",
            "<th scope=\"col\">Line note</th>",
            "No price",
            ".history-link:focus-visible",
            "min-height:44px",
        ):
            self.assertIn(marker, html)
        self.assertIn('aria-label="Invoice state: Temporary"', html)
        self.assertNotIn("permanent number", html.lower())

    def test_real_invoice_shows_permanent_number_and_linked_history(self):
        from apc_core.invoice_workflow_ui import invoice_detail_html

        html = invoice_detail_html(REAL_WITH_HISTORY_FIXTURE)

        for marker in (
            "Real",
            "INV-2026-00102",
            "Replaces",
            "Temporary invoice",
            "Replaced by",
            "Corrected invoice",
            'href="#invoice-INV-099"',
            'href="#invoice-INV-103"',
        ):
            self.assertIn(marker, html)
        self.assertIn('aria-label="Invoice state: Real"', html)

    def test_renderer_has_no_runtime_network_or_mutation_surface(self):
        import apc_core.invoice_workflow_ui as module

        source = inspect.getsource(module).lower()
        html = module.invoice_detail_html(REAL_WITH_HISTORY_FIXTURE).lower()
        for marker in (
            "fetch(", "xmlhttprequest", "websocket", "sqlite", "requests.",
            "coreinvoicestore", "workflow", "<script", "<form", "<button",
            "save", "make real", "change evidence",
        ):
            self.assertNotIn(marker, source)
            self.assertNotIn(marker, html)
        self.assertNotIn("cancel invoice", html)
        self.assertNotIn("correct invoice", html)


INVOICE_LIST_FIXTURES = (
    {
        "display_reference": "ACME-T26-014", "customer_code": "ACME", "customer_name": "ACME Laboratories",
        "consignee": "Dr. Anya Raman · North Wing", "delivery_po_reference": "PO-ACME-441 · DEL-014",
        "evidence_reference": "accepted-snapshot: ACME-014", "state": "Temporary", "staff_name": "Mali S.",
        "recorded_at": "2026-09-01 08:30", "reviewed_at": "2026-09-01 08:30",
    },
    {
        "display_reference": "ACME-T26-015", "customer_code": "ACME", "customer_name": "ACME Laboratories",
        "consignee": "Dr. Ben Okafor · East Wing", "delivery_po_reference": "PO-ACME-442 · DEL-015",
        "evidence_reference": "accepted-snapshot: ACME-015", "state": "Temporary", "staff_name": "Niran P.",
        "recorded_at": "2026-09-01 09:10", "reviewed_at": "2026-09-01 09:18",
    },
    {
        "display_reference": "INV-2026-00102", "customer_code": "RIVER", "customer_name": "River Health",
        "consignee": "River Health Receiving", "delivery_po_reference": "PO-RH-44 · DEL-102",
        "evidence_reference": "accepted-snapshot: RIVER-102", "state": "Real", "staff_name": "Niran P.",
        "recorded_at": "2026-09-01 10:00", "reviewed_at": "2026-09-01 10:00",
    },
    {
        "display_reference": "INV-2026-00099", "customer_code": "NORTH", "customer_name": "North Star Clinic",
        "consignee": "North Star Clinic Stores", "delivery_po_reference": "PO-NS-99 · DEL-099",
        "evidence_reference": "accepted-snapshot: NORTH-099", "state": "Cancelled", "staff_name": "Mali S.",
        "recorded_at": "2026-08-31 16:00", "reviewed_at": "2026-09-01 11:00",
    },
    {
        "display_reference": "INV-2026-00103", "customer_code": "RIVER", "customer_name": "River Health",
        "consignee": "River Health Receiving", "delivery_po_reference": "PO-RH-44 · DEL-103",
        "evidence_reference": "accepted-snapshot: RIVER-103", "state": "Corrected", "staff_name": "Mali S.",
        "recorded_at": "2026-09-01 10:20", "reviewed_at": "2026-09-01 10:25",
    },
)


class TestInvoiceListUi(unittest.TestCase):
    def test_list_lookup_is_limited_to_customer_reference_or_order_number(self):
        from apc_core.invoice_workflow_ui import filter_invoice_list

        records = ({**INVOICE_LIST_FIXTURES[0], "order_number": "ORD-ACME-441"}, *INVOICE_LIST_FIXTURES[1:])
        self.assertEqual([item["display_reference"] for item in filter_invoice_list(records, search="ACME")], ["ACME-T26-014", "ACME-T26-015"])
        self.assertEqual([item["display_reference"] for item in filter_invoice_list(records, search="ORD-ACME-441")], ["ACME-T26-014"])
        self.assertEqual(filter_invoice_list(records, search="anya raman"), ())
        self.assertEqual(filter_invoice_list(records, search="PO-ACME-442"), ())
        self.assertEqual(filter_invoice_list(records, search="niran p."), ())
        self.assertEqual(filter_invoice_list(records, search="ACME-014"), ())

    def test_list_state_filters_keep_cancelled_and_corrected_distinct(self):
        from apc_core.invoice_workflow_ui import filter_invoice_list

        expected = {"All": ["ACME-T26-014", "ACME-T26-015", "INV-2026-00102", "INV-2026-00099", "INV-2026-00103"], "Temporary": ["ACME-T26-014", "ACME-T26-015"], "Real": ["INV-2026-00102"], "Cancelled": ["INV-2026-00099"], "Corrected": ["INV-2026-00103"]}
        for state, references in expected.items():
            with self.subTest(state=state):
                self.assertEqual([item["display_reference"] for item in filter_invoice_list(INVOICE_LIST_FIXTURES, state=state)], references)

    def test_list_html_keeps_acme_consignees_visible_and_has_read_only_accessible_markup(self):
        from apc_core.invoice_workflow_ui import invoice_list_html

        html = invoice_list_html(INVOICE_LIST_FIXTURES, search="ACME", state="Temporary")
        for marker in ('<main class="invoice-list-shell" aria-labelledby="invoice-list-title">', '<h1 id="invoice-list-title">Invoice list</h1>', 'aria-label="Invoice list filters"', 'aria-label="Invoice list results"', '<table>', '<caption>Matching invoice records</caption>', '<th scope="col">Reference</th>', '<th scope="col">Customer</th>', '<th scope="col">Consignee</th>', '<th scope="col">Delivery / PO ref</th>', '<th scope="col">Evidence reference</th>', '<th scope="col">State</th>', '<th scope="col">Recorded / reviewed</th>', 'ACME-T26-014', 'Dr. Anya Raman · North Wing', 'ACME-T26-015', 'Dr. Ben Okafor · East Wing', 'state-badge--temporary', 'min-height:44px', 'Search by customer code, invoice reference, or order number.', 'Search: ACME', 'Filter: Temporary'):
            self.assertIn(marker, html)
        self.assertNotIn('tabindex="0"', html)
        self.assertNotIn('.invoice-list__row:focus-visible', html)
        self.assertNotIn('Customer code · reference', html)
        self.assertNotIn("INV-2026-00102", html)

    def test_list_renderer_has_no_runtime_network_or_mutation_controls(self):
        import apc_core.invoice_workflow_ui as module

        source = inspect.getsource(module).lower()
        html = module.invoice_list_html(INVOICE_LIST_FIXTURES).lower()
        for marker in ("fetch(", "xmlhttprequest", "websocket", "sqlite", "requests.", "<script", "<form", "<button", "localstorage", "sessionstorage", "save", "make real", "change evidence", "cancel invoice", "correct invoice"):
            self.assertNotIn(marker, source)
            self.assertNotIn(marker, html)


class TestP5ReceiptViewModels(unittest.TestCase):
    TEMPORARY_RECEIPT = {
        "invoice_id": "doc-p5-014",
        "state": "temporary",
        "version": 1,
        "permanent_number": None,
        "temporary_reference": "ACME-T26-014",
        "consignee": "Dr. Anya Raman · North Wing",
        "delivery_reference": "PO-ACME-441 · DEL-014",
    }

    def test_temporary_p5_receipt_maps_to_static_detail_and_list_models(self):
        from apc_core.invoice_workflow_ui import (
            invoice_detail_html,
            invoice_list_html,
            p5_receipt_to_detail_view_model,
            p5_receipt_to_list_view_model,
        )

        detail = p5_receipt_to_detail_view_model(
            self.TEMPORARY_RECEIPT,
            staff_name="Mali S.",
            customer_name="ACME Laboratories",
            evidence_reference="accepted-snapshot: ACME-014",
            old_system_notes="Imported legacy note.",
            lines=({"item": "Specimen tube", "quantity": "12", "price": None, "line_note": "Price pending."},),
        )
        listed = p5_receipt_to_list_view_model(
            self.TEMPORARY_RECEIPT,
            customer_code="ACME",
            customer_name="ACME Laboratories",
            evidence_reference="accepted-snapshot: ACME-014",
            staff_name="Mali S.",
            recorded_at="2026-09-01 08:30",
            reviewed_at="2026-09-01 08:30",
        )

        self.assertEqual("doc-p5-014", detail["document_id"])
        self.assertEqual("Temporary", detail["state"])
        self.assertNotIn("permanent_number", detail)
        self.assertEqual("Imported legacy note.", detail["old_system_notes"])
        self.assertEqual("ACME-T26-014", listed["display_reference"])
        self.assertEqual("Temporary", listed["state"])
        self.assertIn("ACME-T26-014", invoice_list_html((listed,)))
        self.assertIn("No number yet", invoice_detail_html(detail))

    def test_real_p5_receipt_retains_staff_reference_and_permanent_number(self):
        from apc_core.invoice_workflow_ui import (
            invoice_detail_html,
            invoice_list_html,
            p5_receipt_to_detail_view_model,
            p5_receipt_to_list_view_model,
        )

        receipt = {**self.TEMPORARY_RECEIPT, "state": "real", "version": 3, "permanent_number": "INV-2026-00102"}
        detail = p5_receipt_to_detail_view_model(
            receipt, staff_name="Niran P.", customer_name="ACME Laboratories",
            evidence_reference="accepted-snapshot: ACME-014", old_system_notes=None, lines=(),
        )
        listed = p5_receipt_to_list_view_model(
            receipt, customer_code="ACME", customer_name="ACME Laboratories",
            evidence_reference="accepted-snapshot: ACME-014", staff_name="Niran P.",
            recorded_at="2026-09-01 10:00", reviewed_at="2026-09-01 10:05",
        )

        self.assertEqual("INV-2026-00102", detail["permanent_number"])
        self.assertEqual("ACME-T26-014", listed["display_reference"])
        self.assertIn("INV-2026-00102", invoice_detail_html(detail))
        self.assertIn("ACME-T26-014", invoice_list_html((listed,)))
        self.assertNotIn("INV-2026-00102", invoice_list_html((listed,)))

    def test_p5_list_adapter_omits_reviewed_at_when_no_real_review_event_exists(self):
        from apc_core.invoice_workflow_ui import invoice_list_html, p5_receipt_to_list_view_model

        listed = p5_receipt_to_list_view_model(
            self.TEMPORARY_RECEIPT,
            customer_code="ACME",
            customer_name="ACME Laboratories",
            evidence_reference="accepted-snapshot: ACME-014",
            staff_name="WAT",
            recorded_at="2026-09-01 08:30",
            reviewed_at=None,
        )

        self.assertNotIn("reviewed_at", listed)
        html = invoice_list_html((listed,))
        self.assertIn("Recorded 2026-09-01 08:30", html)
        self.assertNotIn("Last reviewed", html)
        self.assertNotIn("Reviewed", html)

    def test_p5_receipt_keeps_consignee_and_delivery_reference_separate(self):
        from apc_core.invoice_workflow_ui import (
            invoice_list_html,
            p5_receipt_to_list_view_model,
        )

        listed = p5_receipt_to_list_view_model(
            self.TEMPORARY_RECEIPT, customer_code="ACME", customer_name="ACME Laboratories",
            evidence_reference="accepted-snapshot: ACME-014", staff_name="Mali S.",
            recorded_at="2026-09-01 08:30", reviewed_at="2026-09-01 08:30",
        )

        self.assertEqual("Dr. Anya Raman · North Wing", listed["consignee"])
        self.assertEqual("PO-ACME-441 · DEL-014", listed["delivery_po_reference"])
        self.assertIn("Dr. Anya Raman · North Wing", invoice_list_html((listed,)))
        self.assertIn("PO-ACME-441 · DEL-014", invoice_list_html((listed,)))


    def test_p5_correction_receipt_exposes_display_only_replacement_history(self):
        from apc_core.invoice_workflow_ui import p5_receipt_to_detail_view_model

        detail = p5_receipt_to_detail_view_model(
            {**self.TEMPORARY_RECEIPT, "invoice_id": "doc-p5-015", "correction_of": "doc-p5-014"},
            staff_name="Mali S.", customer_name="ACME Laboratories", evidence_reference="evidence",
            old_system_notes=None, lines=(),
        )

        self.assertEqual(
            {"document_id": "doc-p5-014", "label": "Corrects invoice"},
            detail["replaces"],
        )

    def test_p5_receipt_adapters_reject_missing_or_invalid_required_inputs(self):
        from apc_core.invoice_workflow_ui import (
            p5_receipt_to_detail_view_model,
            p5_receipt_to_list_view_model,
        )

        with self.assertRaisesRegex(ValueError, "temporary_reference is required"):
            p5_receipt_to_list_view_model(
                {key: value for key, value in self.TEMPORARY_RECEIPT.items() if key != "temporary_reference"},
                customer_code="ACME", customer_name="ACME Laboratories", evidence_reference="evidence",
                staff_name="Mali S.", recorded_at="2026-09-01", reviewed_at="2026-09-01",
            )
        with self.assertRaisesRegex(ValueError, "P5 receipt state"):
            p5_receipt_to_detail_view_model(
                {**self.TEMPORARY_RECEIPT, "state": "Temporary"}, staff_name="Mali S.",
                customer_name="ACME Laboratories", evidence_reference="evidence", old_system_notes=None, lines=(),
            )
        with self.assertRaisesRegex(ValueError, "customer_code is required"):
            p5_receipt_to_list_view_model(
                self.TEMPORARY_RECEIPT, customer_code="", customer_name="ACME Laboratories",
                evidence_reference="evidence", staff_name="Mali S.", recorded_at="2026-09-01", reviewed_at="2026-09-01",
            )

    def test_non_real_receipt_does_not_expose_a_stray_permanent_number(self):
        from apc_core.invoice_workflow_ui import (
            invoice_detail_html,
            invoice_list_html,
            p5_receipt_to_detail_view_model,
            p5_receipt_to_list_view_model,
        )

        receipt = {**self.TEMPORARY_RECEIPT, "permanent_number": "SHOULD-NOT-SHOW"}
        detail = p5_receipt_to_detail_view_model(
            receipt, staff_name="Mali S.", customer_name="ACME Laboratories",
            evidence_reference="evidence", old_system_notes=None, lines=(),
        )
        listed = p5_receipt_to_list_view_model(
            receipt, customer_code="ACME", customer_name="ACME Laboratories",
            evidence_reference="evidence", staff_name="Mali S.",
            recorded_at="2026-09-01", reviewed_at="2026-09-01",
        )

        self.assertNotIn("permanent_number", detail)
        self.assertNotIn("SHOULD-NOT-SHOW", invoice_detail_html(detail))
        self.assertNotIn("SHOULD-NOT-SHOW", invoice_list_html((listed,)))

    def test_p5_receipt_adapters_are_pure_and_have_no_persistence_or_runtime_imports(self):
        import apc_core.invoice_workflow_ui as module

        source = inspect.getsource(module).lower()
        for marker in (
            "coreinvoiceworkflowstore", "core_invoices", "sqlite", "requests", "urllib",
            "socket", "http", "pathlib", "open(", "<script", "<form", "<button",
        ):
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
