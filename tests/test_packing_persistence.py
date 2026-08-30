import hashlib
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


class TestPackingPersistenceContract(unittest.TestCase):
    def make_plan(self):
        from apc_core.order_invoice_workspace import SourceLineReference
        from apc_core.packing_state import PackingLine, PackingPlan

        provenance = "a" * 64
        first = SourceLineReference("source_order", "ORD/2026/001", "007", provenance)
        second = SourceLineReference("source_order", "ORD/2026/001", "008", provenance)
        return (
            PackingPlan.open(
                "packing-plan-1",
                provenance,
                (
                    PackingLine(first, Decimal("10"), "A1"),
                    PackingLine(second, Decimal("4"), ""),
                ),
            ),
            first,
            second,
        )

    def test_00_core_owned_packing_store_is_required(self):
        self.assertTrue((Path(__file__).parents[1] / "apc_core" / "packing_persistence.py").is_file())

    def test_storage_constraints_reject_invalid_provenance_status_and_over_allocation_directly(self):
        from apc_core.packing_persistence import PackingStore

        with tempfile.TemporaryDirectory() as directory:
            store = PackingStore(Path(directory))
            plan, first, _ = self.make_plan()
            store.create_plan(plan, actor="YIM", idempotency_key="create-1")
            store.create_box(plan.plan_id, 1, actor="YIM", idempotency_key="box-1", expected_version=0)
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    "INSERT INTO packing_plans(plan_id,provenance,status,version,created_by,idempotency_key) VALUES (?,?,?,?,?,?)",
                    ("bad", "not-a-hash", "OPEN", 0, "YIM", "bad-plan"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute("UPDATE packing_plans SET status='ISSUED' WHERE plan_id=?", (plan.plan_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute(
                    "INSERT INTO packing_mutations("
                    "mutation_id,plan_id,source_type,document_id,line_id,source_sha256,action,box_number,quantity,actor,idempotency_key,expected_version"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("raw-over", plan.plan_id, first.source_type, first.document_id, first.line_id, first.source_sha256, "ALLOCATE", 1, "10.01", "YIM", "raw-over-key", 1),
                )
            self.assertEqual(2, store.count_audit_events(), "rejected direct SQL must add no state or audit event")
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("UPDATE packing_plans SET version=99 WHERE plan_id=?", (plan.plan_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("DELETE FROM packing_plans WHERE plan_id=?", (plan.plan_id,))
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute(
                    "INSERT INTO packing_lines(plan_id,source_type,document_id,line_id,source_sha256,original_quantity,chapter) VALUES (?,?,?,?,?,?,?)",
                    (plan.plan_id, first.source_type, first.document_id, "raw-line", "b" * 64, "not-a-decimal", "A1"),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("INSERT INTO packing_boxes(plan_id,box_number) VALUES (?,?)", (plan.plan_id, 2))
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("DELETE FROM packing_boxes WHERE plan_id=? AND box_number=1", (plan.plan_id,))
            self.assertEqual(2, store.count_audit_events(), "direct state writes must never bypass the audit ledger")
            store.close()

    def test_create_plan_idempotency_rejects_materially_different_membership(self):
        from apc_core.packing_persistence import PackingStore
        from apc_core.packing_state import PackingLine, PackingPlan

        with tempfile.TemporaryDirectory() as directory:
            store = PackingStore(Path(directory))
            plan, first, _ = self.make_plan()
            store.create_plan(plan, actor="YIM", idempotency_key="create-1")
            changed = PackingPlan.open(
                plan.plan_id,
                plan.provenance,
                (PackingLine(first, Decimal("9"), "different chapter"),),
            )
            with self.assertRaisesRegex(ValueError, "idempotency key mismatch"):
                store.create_plan(changed, actor="YIM", idempotency_key="create-1")
            self.assertEqual(1, store.count_audit_events())
            store.close()

    def test_storage_preserves_exact_decimal_reconciliation_without_float_rounding(self):
        from apc_core.order_invoice_workspace import SourceLineReference
        from apc_core.packing_persistence import PackingStore
        from apc_core.packing_state import PackingLine, PackingPlan

        provenance = "b" * 64
        reference = SourceLineReference("source_order", "ORD/EXACT", "1", provenance)
        plan = PackingPlan.open("packing-exact", provenance, (PackingLine(reference, Decimal("0.3"), "A1"),))
        with tempfile.TemporaryDirectory() as directory:
            store = PackingStore(Path(directory))
            store.create_plan(plan, actor="YIM", idempotency_key="exact-create")
            boxed = store.create_box(plan.plan_id, 1, actor="YIM", idempotency_key="exact-box", expected_version=0)
            first = store.allocate(plan.plan_id, reference, 1, Decimal("0.1"), actor="YIM", idempotency_key="exact-one", expected_version=boxed.version)
            completed = store.allocate(plan.plan_id, reference, 1, Decimal("0.2"), actor="YIM", idempotency_key="exact-two", expected_version=first.version)
            self.assertEqual(Decimal("0"), completed.line_state(reference).remaining)
            store.close()

    def test_accepted_mutation_is_atomic_idempotent_and_failed_mutation_writes_neither_state_nor_audit(self):
        from apc_core.packing_persistence import PackingStore

        with tempfile.TemporaryDirectory() as directory:
            store = PackingStore(Path(directory))
            plan, first, _ = self.make_plan()
            created = store.create_plan(plan, actor="YIM", idempotency_key="create-1")
            self.assertEqual(1, store.count_audit_events())
            replayed = store.create_plan(plan, actor="YIM", idempotency_key="create-1")
            self.assertEqual(created, replayed)
            self.assertEqual(1, store.count_audit_events())

            boxed = store.create_box(plan.plan_id, 1, actor="YIM", idempotency_key="box-1", expected_version=0)
            self.assertEqual(2, store.count_audit_events())
            allocated = store.allocate(
                plan.plan_id,
                first,
                1,
                Decimal("3.25"),
                actor="YIM",
                idempotency_key="allocation-1",
                expected_version=boxed.version,
            )
            self.assertEqual(3, store.count_audit_events())
            self.assertEqual(Decimal("3.25"), allocated.line_state(first).allocated)
            self.assertEqual(
                allocated,
                store.allocate(
                    plan.plan_id,
                    first,
                    1,
                    Decimal("3.25"),
                    actor="YIM",
                    idempotency_key="allocation-1",
                    expected_version=boxed.version,
                ),
            )
            self.assertEqual(3, store.count_audit_events())
            with self.assertRaisesRegex(ValueError, "remaining|stale"):
                store.allocate(
                    plan.plan_id,
                    first,
                    1,
                    Decimal("7"),
                    actor="YIM",
                    idempotency_key="allocation-too-large",
                    expected_version=allocated.version,
                )
            self.assertEqual(3, store.count_audit_events())
            self.assertEqual(Decimal("3.25"), store.load_plan(plan.plan_id).line_state(first).allocated)
            store.close()

    def test_reversal_retains_original_mutation_and_source_copy_remains_readonly_unchanged(self):
        from apc_core.order_explorer import OrderExplorer
        from apc_core.packing_persistence import PackingStore
        from tests.test_order_explorer import TestOrderExplorerContract

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = TestOrderExplorerContract().make_snapshot(root)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            store = PackingStore(root / "core-state")
            plan, first, _ = self.make_plan()
            store.create_plan(plan, actor="YIM", idempotency_key="create-1")
            boxed = store.create_box(plan.plan_id, 1, actor="YIM", idempotency_key="box-1", expected_version=0)
            allocated = store.allocate(plan.plan_id, first, 1, Decimal("4"), actor="YIM", idempotency_key="allocation-1", expected_version=boxed.version)
            reversed_plan = store.reverse(
                plan.plan_id,
                "allocation-1",
                actor="YIM",
                idempotency_key="reverse-1",
                expected_version=allocated.version,
                reason="move to another box",
            )
            self.assertEqual(Decimal("0"), reversed_plan.line_state(first).allocated)
            self.assertEqual(3, reversed_plan.version, "a reversal advances the persisted optimistic-concurrency version")
            self.assertEqual(4, store.count_audit_events())
            rows = store.connection.execute(
                "SELECT action,outcome FROM packing_audit WHERE plan_id=? ORDER BY audit_id", (plan.plan_id,)
            ).fetchall()
            self.assertEqual([("CREATE_PLAN", "APPLIED"), ("CREATE_BOX", "APPLIED"), ("ALLOCATE", "APPLIED"), ("REVERSE", "REVERSED")], rows)
            with self.assertRaises(sqlite3.DatabaseError):
                store.connection.execute("DELETE FROM packing_audit WHERE plan_id=?", (plan.plan_id,))
            explorer = OrderExplorer(source)
            with self.assertRaises(sqlite3.OperationalError):
                explorer._connection.execute("DELETE FROM 'MainDB__ORDER'")
            explorer.close()
            self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
            store.close()
