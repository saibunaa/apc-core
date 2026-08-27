import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apc_core.item_explorer import CoreStore
from apc_core.recovery import RecoveryError, RecoveryService


def _core_db(path: Path, marker: str) -> None:
    build_dir = path.parent / f".build-{path.stem}"
    store = CoreStore(build_dir)
    store.connection.execute("CREATE TABLE IF NOT EXISTS state_marker (value TEXT NOT NULL)")
    store.connection.execute("DELETE FROM state_marker")
    store.connection.execute("INSERT INTO state_marker VALUES (?)", (marker,))
    store.connection.commit()
    store.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((build_dir / "apc_core.sqlite").read_bytes())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecoveryServiceTests(unittest.TestCase):
    def test_restore_requires_registered_immutable_snapshot_and_preserves_prior_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "apc_core.sqlite"
            accepted = root / "accepted.sqlite"
            _core_db(current, "before")
            _core_db(accepted, "after")
            service = RecoveryService(data_dir=root)

            with self.assertRaises(RecoveryError):
                service.prepare_restore(
                    snapshot_id="accepted-1",
                    actor="BIAS",
                    reason="test reset",
                    confirmation="accepted-1",
                )

            service.register_accepted_snapshot(
                snapshot_id="accepted-1",
                artifact_path=accepted,
                sha256=_sha256(accepted),
                provenance="isolated fixture acceptance",
            )
            result = service.prepare_restore(
                snapshot_id="accepted-1",
                actor="BIAS",
                reason="test reset",
                confirmation="accepted-1",
                maintenance=lambda: None,
            )

            self.assertEqual("after", sqlite3.connect(current).execute("SELECT value FROM state_marker").fetchone()[0])
            self.assertEqual("before", sqlite3.connect(Path(result["prior_generation_path"])).execute("SELECT value FROM state_marker").fetchone()[0])
            self.assertEqual("passed", result["validation_result"])
            self.assertEqual("BIAS", service.audit_entries()[0]["actor"])
            self.assertEqual("accepted-1", service.audit_entries()[0]["snapshot_id"])

    def test_restore_refuses_tampered_snapshot_without_switching_current_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "apc_core.sqlite"
            accepted = root / "accepted.sqlite"
            _core_db(current, "before")
            _core_db(accepted, "after")
            service = RecoveryService(data_dir=root)
            service.register_accepted_snapshot(
                snapshot_id="accepted-1",
                artifact_path=accepted,
                sha256=_sha256(accepted),
                provenance="isolated fixture acceptance",
            )
            registered = service.accepted_snapshot_path("accepted-1")
            registered.chmod(0o600)
            registered.write_bytes(registered.read_bytes() + b"tampered")

            with self.assertRaises(RecoveryError):
                service.prepare_restore(
                    snapshot_id="accepted-1", actor="BIAS", reason="test reset", confirmation="accepted-1"
                )

            self.assertEqual("before", sqlite3.connect(current).execute("SELECT value FROM state_marker").fetchone()[0])
            self.assertEqual([], service.audit_entries())
    def test_authorized_rollback_restores_the_immediately_prior_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "apc_core.sqlite"
            accepted = root / "accepted.sqlite"
            _core_db(current, "before")
            _core_db(accepted, "after")
            service = RecoveryService(data_dir=root)
            service.register_accepted_snapshot(
                snapshot_id="accepted-1", artifact_path=accepted, sha256=_sha256(accepted), provenance="isolated fixture"
            )
            restore = service.prepare_restore(
                snapshot_id="accepted-1", actor="BIAS", reason="test reset", confirmation="accepted-1", maintenance=lambda: None
            )

            rollback = service.rollback(actor="BIAS", reason="validation reversal", confirmation=restore["prior_generation_path"], maintenance=lambda: None)

            self.assertEqual("before", sqlite3.connect(current).execute("SELECT value FROM state_marker").fetchone()[0])
            self.assertEqual("passed", rollback["validation_result"])
            self.assertEqual(["restore", "rollback"], [entry["operation"] for entry in service.audit_entries()])

    def test_integrity_valid_wrong_schema_snapshot_is_rejected_at_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_schema = root / "wrong.sqlite"
            connection = sqlite3.connect(wrong_schema)
            connection.execute("CREATE TABLE state_marker (value TEXT NOT NULL)")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RecoveryError, "snapshot validation failed"):
                RecoveryService(data_dir=root / "state").register_accepted_snapshot(
                    snapshot_id="wrong-schema", artifact_path=wrong_schema, sha256=_sha256(wrong_schema),
                    provenance="integrity-valid fixture",
                )

    def test_restore_requires_maintenance_closure_before_switch_and_reopened_store_persists_restored_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            current = state / "apc_core.sqlite"
            accepted = root / "accepted.sqlite"
            _core_db(current, "before")
            _core_db(accepted, "after")
            service = RecoveryService(data_dir=state)
            service.register_accepted_snapshot(
                snapshot_id="accepted-1", artifact_path=accepted, sha256=_sha256(accepted), provenance="fixture"
            )
            live_store = CoreStore(state)
            with self.assertRaisesRegex(RecoveryError, "maintenance"):
                service.prepare_restore(
                    snapshot_id="accepted-1", actor="BIAS", reason="test", confirmation="accepted-1"
                )
            self.assertEqual("before", live_store.connection.execute("SELECT value FROM state_marker").fetchone()[0])

            closed = []
            result = service.prepare_restore(
                snapshot_id="accepted-1", actor="BIAS", reason="test", confirmation="accepted-1",
                maintenance=lambda: (live_store.close(), closed.append("all-core-modules-closed")),
            )
            self.assertEqual(["all-core-modules-closed"], closed)
            self.assertTrue(result["restart_required"])
            reopened = CoreStore(state)
            self.assertEqual("after", reopened.connection.execute("SELECT value FROM state_marker").fetchone()[0])
            reopened.connection.execute("INSERT INTO state_marker VALUES ('post-restore-write')")
            reopened.connection.commit()
            reopened.close()
            self.assertEqual(
                "post-restore-write",
                sqlite3.connect(current).execute("SELECT value FROM state_marker ORDER BY rowid DESC LIMIT 1").fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
