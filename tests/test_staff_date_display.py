import subprocess
import unittest



class TestStaffDateDisplay(unittest.TestCase):
    def test_date_only_and_timestamps_use_staff_format(self):
        from apc_core.staff_dates import format_staff_date, format_staff_timestamp

        self.assertEqual("29/08/2026", format_staff_date("2026-08-29"))
        self.assertEqual("29/08/2026 14:05:09", format_staff_timestamp("2026-08-29T14:05:09Z"))
        self.assertEqual("29/08/2026 14:05:09", format_staff_timestamp("2026-08-29 14:05:09"))

    def test_legacy_month_first_dates_are_normalized_only_for_ui(self):
        from apc_core.staff_dates import format_staff_date, format_staff_timestamp

        self.assertEqual("04/01/2026", format_staff_date("01/04/26"))
        self.assertEqual("04/01/2026 23:59:59", format_staff_timestamp("01/04/26 23:59:59"))

    def test_invalid_or_missing_values_are_safe_and_not_fabricated(self):
        from apc_core.staff_dates import format_staff_date, format_staff_timestamp

        self.assertEqual("", format_staff_date(None))
        self.assertEqual("2026-99-99", format_staff_date("2026-99-99"))
        self.assertEqual("not-a-date", format_staff_timestamp("not-a-date"))

    def test_static_staff_surfaces_format_display_values_without_mutating_payload_contracts(self):
        from apc_core.invoice_workflow_ui import invoice_list_html
        from apc_core.order_invoice_ui import order_invoice_html

        invoice_html = invoice_list_html(({
            "display_reference": "INV-1", "customer_code": "C-1", "customer_name": "Customer",
            "consignee": "Consignee", "delivery_po_reference": "PO-1", "evidence_reference": "Evidence",
            "state": "Temporary", "staff_name": "Mali", "recorded_at": "2026-08-29 14:05:09",
            "reviewed_at": "01/04/26 23:59:59",
        },))
        self.assertIn("Recorded 29/08/2026 14:05:09", invoice_html)
        self.assertIn("Last reviewed 04/01/2026 23:59:59", invoice_html)

        workspace = order_invoice_html(include_core_drafts=True)
        self.assertIn("function formatStaffDate", workspace)
        self.assertIn("formatStaffDate(value)", workspace)
        self.assertIn("formatStaffTimestamp(value)", workspace)
        self.assertIn("date_from", workspace)
        self.assertIn("date_to", workspace)

        from apc_core.order_invoice_workspace import map_source_order_browse
        raw = map_source_order_browse({"order_id": "ORD-1", "order_date": "2026-08-29", "customer_id": "C-1"})
        self.assertIn(("order_date", "2026-08-29"), raw.fields)

    def test_order_invoice_rendering_formats_all_record_families_in_the_browser(self):
        from apc_core.order_invoice_ui import order_invoice_html

        html = order_invoice_html(include_core_drafts=True)
        script = html[html.index("function formatStaffDate"):html.index("function render(payload)")]
        harness = script + """
const sourceOrder={record_type:'source_order',order_id:'ORD-1',order_date:'01/04/26',customer_id:'C-1'};
const sourceInvoice={record_type:'source_invoice',source_invoice_number:'INV-1',invoice_date:'2026-08-29',customer_id:'C-1',customer_name:'Customer'};
const draft={record_type:'core_draft',draft_id:'D-1',status:'draft',created_by:'Mali',created_at:'2026-08-29 14:05:09'};
if(textFor(sourceOrder)!=='ORD-1 · 04/01/2026 · C-1') throw new Error(textFor(sourceOrder));
if(textFor(sourceInvoice)!=='INV-1 · 29/08/2026 · C-1 · Customer') throw new Error(textFor(sourceInvoice));
if(textFor(draft)!=='D-1 · draft · Mali · 29/08/2026 14:05:09') throw new Error(textFor(draft));
if(formatStaffTimestamp('2026-09-01 08:30')!=='01/09/2026 08:30:00') throw new Error('minute timestamp was not normalized');
if(formatStaffTimestamp('2026-99-99')!=='2026-99-99') throw new Error('invalid date was fabricated');
"""
        subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
