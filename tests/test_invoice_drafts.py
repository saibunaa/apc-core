import hashlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


class TestInvoiceDraftsContract(unittest.TestCase):
    def make_source_fixture(self, root: Path) -> Path:
        source = root / "accepted-source.sqlite"
        connection = sqlite3.connect(source)
        connection.execute('CREATE TABLE "INVOICE" (id TEXT)')
        connection.execute('CREATE TABLE "INV ITEM" (id TEXT)')
        connection.execute('CREATE TABLE "AWB" (id TEXT)')
        connection.executemany('INSERT INTO "INVOICE" VALUES (?)', [("I-1",), ("I-2",)])
        connection.commit()
        connection.close()
        return source

    def test_create_replay_uses_local_tables_and_preserves_source_fixture(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source_fixture(root)
            source_bytes = source.read_bytes()
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            store = InvoiceDraftStore(root / "core-state")
            submission = [
                {"order_id": "ORD / 001", "order_line_no": "01", "item_id": "ITEM / A", "quantity": "2"},
                {"order_id": "ORD / 001", "order_line_no": "02", "item_id": "ITEM / B", "quantity": "3"},
            ]

            created = store.create_draft(source_hash, "YIM", "request / 001", submission)
            replayed = store.create_draft(source_hash, "YIM", "request / 001", submission)

            self.assertEqual(created, replayed)
            self.assertEqual(
                {"draft_id", "accepted_snapshot_sha256", "created_by", "created_at", "status", "lines"},
                set(created),
            )
            self.assertEqual("draft", created["status"])
            self.assertEqual(source_hash, created["accepted_snapshot_sha256"])
            self.assertEqual(
                [
                    {"line_no": 1, "order_id": "ORD / 001", "order_line_no": "01", "item_id": "ITEM / A", "quantity": "2"},
                    {"line_no": 2, "order_id": "ORD / 001", "order_line_no": "02", "item_id": "ITEM / B", "quantity": "3"},
                ],
                created["lines"],
            )
            self.assertEqual(source_bytes, source.read_bytes())
            self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).hexdigest())

            local = root / "core-state" / "apc_core.sqlite"
            self.assertTrue(local.is_file())
            self.assertEqual({local}, set((root / "core-state").iterdir()))
            connection = sqlite3.connect(local)
            table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"invoice_drafts", "invoice_draft_lines", "invoice_line_allocations", "invoice_draft_conflicts", "invoice_draft_audit"}.issubset(table_names))
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM invoice_line_allocations").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM invoice_draft_audit").fetchone()[0])
            self.assertFalse({"INVOICE", "INV ITEM", "AWB"} & table_names)
            connection.close()

    def test_allocation_identity_and_idempotency_mismatch_fail_closed(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            store = InvoiceDraftStore(Path(tmp) / "state")
            snapshot = "a" * 64
            line = {"order_id": "ORDER-1", "order_line_no": "1", "item_id": "ITEM-1", "quantity": "1"}
            with self.assertRaisesRegex(ValueError, "duplicate allocation"):
                store.create_draft(snapshot, "YIM", "key-1", [line, line.copy()])
            self.assertEqual(0, store.audit_count())

            store.create_draft(snapshot, "YIM", "key-1", [line])
            with self.assertRaisesRegex(ValueError, "idempotency key"):
                store.create_draft(snapshot, "YIM", "key-1", [{**line, "quantity": "2"}])
            self.assertEqual(1, store.audit_count())

    def test_audit_decisions_and_conflict_resolutions_are_append_only(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            store = InvoiceDraftStore(Path(tmp) / "state")
            draft = store.create_draft(
                "b" * 64,
                "YIM",
                "key-2",
                [{"order_id": "ORDER-2", "order_line_no": "1", "item_id": "ITEM-2", "quantity": "1"}],
            )
            decision = store.record_staff_decision(draft["draft_id"], "submit_for_review", "WAT")
            conflict = store.record_conflict(draft["draft_id"], "allocation_conflict", "WAT")
            resolution = store.resolve_conflict(conflict["conflict_id"], "return_to_draft", "YIM")

            self.assertEqual({"draft_id", "status", "audit_id"}, set(decision))
            self.assertEqual("review", decision["status"])
            self.assertEqual({"conflict_id", "draft_id", "status", "audit_id"}, set(conflict))
            self.assertEqual("conflicted", conflict["status"])
            self.assertEqual({"conflict_id", "draft_id", "status", "audit_id"}, set(resolution))
            self.assertEqual("draft", resolution["status"])
            self.assertEqual(4, store.audit_count())
            with self.assertRaisesRegex(ValueError, "unknown status transition"):
                store.record_staff_decision(draft["draft_id"], "issue", "YIM")
            with self.assertRaisesRegex(ValueError, "unknown status transition"):
                store.record_staff_decision(draft["draft_id"], "return_to_draft", "YIM")
            self.assertEqual(4, store.audit_count())

    def test_narrow_input_rejects_awb_empty_lines_and_non_draft_statuses(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            store = InvoiceDraftStore(Path(tmp) / "state")
            with self.assertRaisesRegex(ValueError, "invalid line"):
                store.create_draft(
                    "c" * 64,
                    "YIM",
                    "key-3",
                    [{"order_id": "ORDER-3", "order_line_no": "1", "item_id": "ITEM-3", "quantity": "1", "awb": "not-a-key"}],
                )
            with self.assertRaisesRegex(ValueError, "invalid accepted snapshot"):
                store.create_draft("not-a-hash", "YIM", "key-4", [{"order_id": "O", "order_line_no": "1", "item_id": "I", "quantity": "1"}])
            with self.assertRaisesRegex(ValueError, "invalid lines"):
                store.create_draft("c" * 64, "YIM", "key-5", [])

    def test_schema_enforces_provenance_and_audit_immutability_and_is_exact(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            store = InvoiceDraftStore(Path(tmp) / "state")
            draft = store.create_draft("d" * 64, "YIM", "key-6", [{"order_id": "O", "order_line_no": "1", "item_id": "I", "quantity": "1"}])
            tables = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual({"invoice_drafts", "invoice_draft_lines", "invoice_line_allocations", "invoice_draft_conflicts", "invoice_draft_audit", "sqlite_sequence"}, tables)
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    "INSERT INTO invoice_line_allocations(draft_id,order_id,order_line_no,line_no) VALUES (?,?,?,?)",
                    (draft["draft_id"], "orphan-order", "99", 99),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("UPDATE invoice_drafts SET accepted_snapshot_sha256=? WHERE draft_id=?", ("e" * 64, draft["draft_id"]))
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("UPDATE invoice_draft_audit SET action='changed' WHERE draft_id=?", (draft["draft_id"],))
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("DELETE FROM invoice_draft_audit WHERE draft_id=?", (draft["draft_id"],))

    def test_store_never_opens_source_fixture_and_rejects_incompatible_existing_schema(self):
        from apc_core import invoice_drafts
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source_fixture(root)
            local = root / "state" / "apc_core.sqlite"
            real_connect = sqlite3.connect
            with mock.patch.object(invoice_drafts.sqlite3, "connect", wraps=real_connect) as connect:
                store = InvoiceDraftStore(root / "state")
            self.assertEqual([mock.call(local, check_same_thread=False)], connect.call_args_list)
            store.close()
            old = real_connect(local)
            old.execute("DROP TABLE invoice_drafts")
            old.execute("CREATE TABLE invoice_drafts (draft_id TEXT PRIMARY KEY)")
            old.commit()
            old.close()
            with self.assertRaisesRegex(ValueError, "incompatible invoice draft schema"):
                InvoiceDraftStore(root / "state")
            self.assertTrue(source.is_file())

    def test_incompatible_existing_schema_is_rejected_without_partial_local_schema_changes(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            path = state / "apc_core.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE invoice_drafts (draft_id TEXT PRIMARY KEY)")
            connection.commit()
            connection.close()
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "incompatible invoice draft schema"):
                InvoiceDraftStore(state)
            self.assertEqual(before, path.read_bytes())

    def test_existing_schema_with_weakened_audit_table_is_rejected(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            store = InvoiceDraftStore(state)
            store.close()
            connection = sqlite3.connect(state / "apc_core.sqlite")
            connection.execute("DROP TABLE invoice_draft_audit")
            connection.execute(
                "CREATE TABLE invoice_draft_audit (audit_id INTEGER, draft_id TEXT NOT NULL REFERENCES invoice_drafts(draft_id), "
                "action TEXT NOT NULL, actor TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "incompatible invoice draft schema"):
                InvoiceDraftStore(state)

    def test_existing_schema_with_weakened_immutability_trigger_is_rejected(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            store = InvoiceDraftStore(state)
            store.close()
            connection = sqlite3.connect(state / "apc_core.sqlite")
            connection.execute("DROP TRIGGER invoice_drafts_snapshot_immutable")
            connection.execute(
                "CREATE TRIGGER invoice_drafts_snapshot_immutable BEFORE UPDATE OF accepted_snapshot_sha256 "
                "ON invoice_drafts BEGIN SELECT 1; END"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "incompatible invoice draft schema"):
                InvoiceDraftStore(state)

    def test_existing_schema_without_line_mapping_constraint_is_rejected(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            store = InvoiceDraftStore(state)
            store.close()
            connection = sqlite3.connect(state / "apc_core.sqlite")
            connection.execute("DROP TABLE invoice_line_allocations")
            connection.execute(
                "CREATE TABLE invoice_line_allocations ("
                "draft_id TEXT NOT NULL REFERENCES invoice_drafts(draft_id), order_id TEXT NOT NULL, "
                "order_line_no TEXT NOT NULL, line_no INTEGER NOT NULL, "
                "UNIQUE(draft_id,order_id,order_line_no))"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "incompatible invoice draft schema"):
                InvoiceDraftStore(state)

    def test_concurrent_replay_and_losing_transition_have_one_audit_effect(self):
        from apc_core.invoice_drafts import InvoiceDraftStore

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            first = InvoiceDraftStore(state)
            second = InvoiceDraftStore(state)
            barrier = threading.Barrier(2)
            results, errors = [], []
            line = {"order_id": "O", "order_line_no": "1", "item_id": "I", "quantity": "1"}

            def create(store):
                try:
                    barrier.wait()
                    results.append(store.create_draft("f" * 64, "YIM", "key-7", [line]))
                except Exception as error:
                    errors.append(error)

            workers = [threading.Thread(target=create, args=(store,)) for store in (first, second)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual([], errors)
            self.assertEqual(2, len(results))
            self.assertEqual(results[0]["draft_id"], results[1]["draft_id"])
            self.assertEqual(1, first.audit_count())
            draft_id = results[0]["draft_id"]
            barrier = threading.Barrier(2)
            errors = []

            def transition(store):
                try:
                    barrier.wait()
                    store.record_staff_decision(draft_id, "submit_for_review", "YIM")
                except Exception as error:
                    errors.append(error)

            workers = [threading.Thread(target=transition, args=(first,)) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(1, len(errors))
            self.assertEqual(2, first.audit_count())


if __name__ == "__main__":
    unittest.main()
