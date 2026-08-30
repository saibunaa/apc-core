import ast
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "apc_core" / "invoice_draft_service.py"


def proposal(**changes):
    value = {
        "selected_order_ids": ("ORD / 001",),
        "customer_id": "CUST-1",
        "document_family": "commercial",
        "lines": (
            {
                "order_id": "ORD / 001",
                "line_ref": "01",
                "item_id": "ITEM / A",
                "quantity": "2.00",
                "unit_price": "10.50",
                "source_annotation": "frozen label",
            },
        ),
        "annotations": ({"order_id": "ORD / 001", "line_ref": "01", "value": "frozen label"},),
        "decisions": ({"conflict_id": "ship-to", "chosen_existing_value": "BKK", "chosen_existing_source": "ORD / 001"},),
        "unresolved": (),
        "ready_to_save": True,
        "idempotency_material": "f" * 64,
    }
    value.update(changes)
    return value


class TestInvoiceDraftServiceContract(unittest.TestCase):
    def make_source_fixture(self, root):
        source = root / "accepted-source.sqlite"
        connection = sqlite3.connect(source)
        connection.execute('CREATE TABLE "INVOICE" (id TEXT)')
        connection.execute('CREATE TABLE "INV ITEM" (id TEXT)')
        connection.execute('CREATE TABLE "AWB" (id TEXT)')
        connection.execute('INSERT INTO "INVOICE" VALUES ("legacy")')
        connection.commit()
        connection.close()
        return source

    def service(self, root):
        from apc_core.invoice_draft_service import InvoiceDraftService
        from apc_core.invoice_drafts import InvoiceDraftStore

        return InvoiceDraftService(InvoiceDraftStore(root / "core-state"))

    def test_00_inv_1d_service_module_exists_before_behavior_contracts(self):
        self.assertTrue(MODULE_PATH.is_file())

    def test_save_persists_frozen_preview_provenance_and_audit_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source_fixture(root)
            source_bytes = source.read_bytes()
            snapshot = hashlib.sha256(source_bytes).hexdigest()
            service = self.service(root)

            saved = service.save(proposal(), snapshot, "YIM")

            self.assertEqual(
                {"draft_id", "accepted_snapshot_sha256", "created_by", "created_at", "status", "selected_order_ids", "lines"},
                set(saved),
            )
            self.assertEqual("draft", saved["status"])
            self.assertEqual(snapshot, saved["accepted_snapshot_sha256"])
            self.assertEqual(("ORD / 001",), saved["selected_order_ids"])
            self.assertEqual(proposal()["lines"], saved["lines"])
            self.assertEqual(source_bytes, source.read_bytes())

            local = root / "core-state" / "apc_core.sqlite"
            connection = sqlite3.connect(local)
            payload = json.loads(connection.execute("SELECT submission_json FROM invoice_drafts").fetchone()[0])
            audit = json.loads(connection.execute("SELECT details_json FROM invoice_draft_audit").fetchone()[0])
            self.assertEqual(snapshot, payload["accepted_snapshot_sha256"])
            self.assertEqual("CUST-1", payload["customer_id"])
            self.assertEqual("commercial", payload["document_family"])
            self.assertEqual(["ORD / 001"], payload["selected_order_ids"])
            self.assertEqual([proposal()["lines"][0]], payload["lines"])
            self.assertEqual([proposal()["annotations"][0]], payload["annotations"])
            self.assertEqual([proposal()["decisions"][0]], payload["decisions"])
            self.assertEqual("f" * 64, payload["idempotency_key"])
            self.assertEqual("YIM", audit["actor"])
            self.assertEqual(snapshot, audit["accepted_snapshot_sha256"])
            self.assertEqual(["ORD / 001"], audit["selected_order_ids"])
            self.assertEqual([proposal()["decisions"][0]], audit["conflict_decisions"])
            self.assertEqual("f" * 64, audit["idempotency_key"])
            connection.close()

    def test_frozen_converter_submission_lines_and_allocations_reject_direct_sql_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp))
            saved = service.save(proposal(), "a" * 64, "YIM")
            draft_id = saved["draft_id"]
            connection = service.store.connection

            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE invoice_drafts SET submission_json=? WHERE draft_id=?", ("{}", draft_id))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE invoice_drafts SET created_at=CURRENT_TIMESTAMP WHERE draft_id=?", (draft_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE invoice_draft_lines SET item_id='changed' WHERE draft_id=?", (draft_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("INSERT INTO invoice_draft_lines(draft_id,line_no,item_id,quantity) VALUES (?,?,?,?)", (draft_id, 2, "ITEM-B", "1"))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM invoice_draft_lines WHERE draft_id=?", (draft_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE invoice_line_allocations SET order_id='changed' WHERE draft_id=?", (draft_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("INSERT INTO invoice_line_allocations(draft_id,order_id,order_line_no,line_no) VALUES (?,?,?,?)", (draft_id, "ORD / 002", "01", 2))
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM invoice_line_allocations WHERE draft_id=?", (draft_id,))

    def test_same_valid_proposal_replays_without_second_draft_or_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp))
            first = service.save(proposal(), "a" * 64, "YIM")
            replayed = service.save(proposal(), "a" * 64, "YIM")

            self.assertEqual(first, replayed)
            self.assertEqual(1, service.store.audit_count())

    def test_idempotency_key_cannot_replay_changed_frozen_identity_or_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp))
            service.save(proposal(), "a" * 64, "YIM")
            for changed in (
                {"customer_id": "CUST-2"},
                {"document_family": "proforma"},
                {"annotations": ({"order_id": "ORD / 001", "line_ref": "01", "value": "different frozen label"},)},
            ):
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "idempotency key"):
                    service.save(proposal(**changed), "a" * 64, "YIM")
            self.assertEqual(1, service.store.audit_count())

    def test_unready_or_tampered_proposal_rolls_back_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp))
            with self.assertRaisesRegex(ValueError, "ready"):
                service.save(proposal(ready_to_save=False), "b" * 64, "YIM")
            with self.assertRaisesRegex(ValueError, "line"):
                service.save(proposal(lines=({"order_id": "ORD / 001", "line_ref": "01", "item_id": "ITEM / A", "quantity": "2.00", "unit_price": "10.50", "source_annotation": "changed", "raw": "forbidden"},)), "b" * 64, "YIM")
            self.assertEqual(0, service.store.audit_count())
            self.assertEqual(0, service.store.connection.execute("SELECT COUNT(*) FROM invoice_drafts").fetchone()[0])

    def test_existing_active_draft_allocation_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp))
            service.save(proposal(), "c" * 64, "YIM")
            with self.assertRaisesRegex(ValueError, "allocation"):
                service.save(proposal(idempotency_material="e" * 64), "c" * 64, "WAT")
            self.assertEqual(1, service.store.audit_count())
            self.assertEqual(1, service.store.connection.execute("SELECT COUNT(*) FROM invoice_drafts").fetchone()[0])
            self.assertEqual(1, service.store.connection.execute("SELECT COUNT(*) FROM invoice_line_allocations").fetchone()[0])

    def test_service_opens_only_core_local_sqlite_and_exposes_no_source_or_delivery_capabilities(self):
        from apc_core import invoice_drafts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source_fixture(root)
            local = root / "core-state" / "apc_core.sqlite"
            real_connect = sqlite3.connect
            with mock.patch.object(invoice_drafts.sqlite3, "connect", wraps=real_connect) as connect:
                service = self.service(root)
                service.save(proposal(), "d" * 64, "YIM")
            self.assertEqual([mock.call(local, check_same_thread=False)], connect.call_args_list)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), hashlib.sha256(source.read_bytes()).hexdigest())
            tree = ast.parse(MODULE_PATH.read_text())
            imports = {
                name.split(".")[0]
                for node in ast.walk(tree)
                for name in (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
            }
            self.assertFalse(imports & {"sqlite3", "pathlib", "socket", "requests", "urllib", "http"})
            source_text = MODULE_PATH.read_text().lower()
            for forbidden in ("awb", "issue", "print", "export", "sync", "mdb", "open(", "connect("):
                self.assertNotIn(forbidden, source_text)


if __name__ == "__main__":
    unittest.main()
