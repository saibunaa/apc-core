import inspect
import unittest


class TestInvoiceReadProjection(unittest.TestCase):
    def test_projection_prepares_temporary_models_for_existing_pure_adapters(self):
        from apc_core.invoice_read_projection import project_invoice_detail, project_invoice_list

        core_invoice = {
            "receipt": {
                "invoice_id": "core-invoice-014",
                "state": "temporary",
                "version": 1,
                "permanent_number": None,
                "temporary_reference": "ACME-T26-014",
                "consignee": "Dr. Anya Raman · North Wing",
                "delivery_reference": "PO-ACME-441 · DEL-014",
            },
            "created_by": "WAT",
            "created_at": "2026-09-01 08:30",
            "customer": {"customer_code": "ACME", "approved_name": "ACME Laboratories"},
            "evidence_reference": "accepted-snapshot: ACME-014",
            "order_number": "ORD-ACME-441",
            "lines": (
                {"item": "Specimen tube", "quantity": "12", "current_price": None, "line_note": "Price pending."},
            ),
        }

        detail = project_invoice_detail(core_invoice)
        listed = project_invoice_list(core_invoice)

        self.assertEqual("core-invoice-014", detail["document_id"])
        self.assertEqual("Temporary", detail["state"])
        self.assertEqual("WAT", detail["staff_name"])
        self.assertEqual("ACME Laboratories", detail["customer_name"])
        self.assertEqual(None, detail["lines"][0]["price"])
        self.assertEqual("ACME-T26-014", listed["display_reference"])
        self.assertEqual("ACME", listed["customer_code"])
        self.assertEqual("WAT", listed["staff_name"])
        self.assertEqual("2026-09-01 08:30", listed["recorded_at"])
        self.assertEqual("ORD-ACME-441", listed["order_number"])
        self.assertNotIn("reviewed_at", listed)
        self.assertEqual("Dr. Anya Raman · North Wing", listed["consignee"])
        self.assertEqual("PO-ACME-441 · DEL-014", listed["delivery_po_reference"])

        from apc_core.invoice_workflow_ui import invoice_detail_html

        html = invoice_detail_html(detail)
        self.assertIn("<dt>Created by</dt><dd>WAT</dd>", html)
        self.assertNotIn("<dt>Staff</dt>", html)

    def test_projection_keeps_real_number_detail_only_and_exposes_both_correction_links(self):
        from apc_core.invoice_read_projection import project_invoice_detail, project_invoice_list

        core_invoice = {
            "receipt": {
                "invoice_id": "core-invoice-015",
                "state": "real",
                "version": 3,
                "permanent_number": "INV-2026-00102",
                "temporary_reference": "ACME-T26-015",
                "consignee": "ACME Receiving",
                "delivery_reference": "PO-ACME-442",
                "correction_of": "core-invoice-014",
            },
            "created_by": "WAT",
            "created_at": "2026-09-01 10:00",
            "customer": {"customer_code": "ACME", "approved_name": None},
            "evidence_reference": "accepted-snapshot: ACME-015",
            "lines": (),
            "replaced_by": {"document_id": "core-invoice-016", "label": "ACME-T26-016"},
        }

        detail = project_invoice_detail(core_invoice)
        listed = project_invoice_list(core_invoice)

        self.assertEqual("INV-2026-00102", detail["permanent_number"])
        self.assertEqual({"document_id": "core-invoice-014", "label": "Corrects invoice"}, detail["replaces"])
        self.assertEqual({"document_id": "core-invoice-016", "label": "Replaced by ACME-T26-016"}, detail["replaced_by"])
        self.assertEqual("ACME-T26-015", listed["display_reference"])
        self.assertEqual("ACME", listed["customer_name"])
        self.assertNotIn("INV-2026-00102", listed.values())

    def test_projection_falls_back_to_customer_code_when_approved_name_is_blank(self):
        from apc_core.invoice_read_projection import project_invoice_list

        core_invoice = {
            "receipt": {
                "invoice_id": "core-invoice-blank-name",
                "state": "temporary",
                "version": 1,
                "permanent_number": None,
                "temporary_reference": "ACME-T26-099",
                "consignee": "Consignee",
                "delivery_reference": "PO-099",
            },
            "created_by": "WAT",
            "created_at": "2026-09-01 08:30",
            "customer": {"customer_code": "ACME", "approved_name": ""},
            "evidence_reference": "accepted-snapshot: ACME-099",
            "lines": (),
        }

        self.assertEqual("ACME", project_invoice_list(core_invoice)["customer_name"])

    def test_projection_accepts_review_timestamp_only_from_an_explicit_core_event(self):
        from apc_core.invoice_read_projection import project_invoice_list

        core_invoice = {
            "receipt": {
                "invoice_id": "core-invoice-014",
                "state": "temporary",
                "version": 1,
                "permanent_number": None,
                "temporary_reference": "ACME-T26-014",
                "consignee": "Consignee",
                "delivery_reference": "PO-014",
            },
            "created_by": "WAT",
            "created_at": "2026-09-01 08:30",
            "customer": {"customer_code": "ACME", "approved_name": "ACME Laboratories"},
            "evidence_reference": "accepted-snapshot: ACME-014",
            "lines": (),
            "review_event": {"recorded_at": "2026-09-01 09:00"},
        }

        listed = project_invoice_list(core_invoice)

        self.assertEqual("2026-09-01 09:00", listed["reviewed_at"])

    def test_projection_is_pure_and_does_not_import_store_or_runtime_boundaries(self):
        import apc_core.invoice_read_projection as module

        source = inspect.getsource(module).lower()
        for marker in (
            "sqlite", "coreinvoiceworkflowstore", "coreinvoicestore", "requests", "urllib",
            "socket", "http", "pathlib", "open(", "flask", "fastapi",
        ):
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
