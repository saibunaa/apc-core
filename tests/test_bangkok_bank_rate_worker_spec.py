from pathlib import Path
import unittest


class BangkokBankRateWorkerSpecTests(unittest.TestCase):
    def test_00_worker_spec_module_exists_before_boundary_contracts(self):
        source = Path(__file__).resolve().parents[1] / "apc_core" / "bangkok_bank_rate_worker_spec.py"
        self.assertTrue(source.is_file())

    def test_declares_disabled_separate_worker_storage_boundary(self):
        from apc_core.bangkok_bank_rate_worker_spec import separate_worker_spec

        spec = separate_worker_spec(
            worker_state_dir=Path("/var/lib/apc-rate-worker"),
            core_data_dir=Path("/var/lib/apc-core"),
        )

        self.assertFalse(spec.enabled_by_default)
        self.assertFalse(spec.opens_listener)
        self.assertFalse(spec.starts_timer)
        self.assertFalse(spec.core_may_fetch_bank)
        self.assertEqual(Path("/var/lib/apc-rate-worker"), spec.worker_state_dir)
        self.assertEqual(Path("/var/lib/apc-core"), spec.core_data_dir)

    def test_rejects_shared_or_relative_state_paths_before_any_runtime_wiring(self):
        from apc_core.bangkok_bank_rate_worker_spec import BangkokBankRateWorkerSpecError, separate_worker_spec

        with self.assertRaises(BangkokBankRateWorkerSpecError):
            separate_worker_spec(worker_state_dir=Path("rates"), core_data_dir=Path("/var/lib/apc-core"))
        with self.assertRaises(BangkokBankRateWorkerSpecError):
            separate_worker_spec(worker_state_dir=Path("/var/lib/apc-core"), core_data_dir=Path("/var/lib/apc-core"))

    def test_rejects_dotdot_and_symlink_aliases_of_core_data(self):
        from tempfile import TemporaryDirectory

        from apc_core.bangkok_bank_rate_worker_spec import BangkokBankRateWorkerSpecError, separate_worker_spec

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            core = root / "core"
            core.mkdir()
            dotdot_alias = root / "worker" / ".." / "core"
            with self.assertRaises(BangkokBankRateWorkerSpecError):
                separate_worker_spec(worker_state_dir=dotdot_alias, core_data_dir=core)
            alias = root / "worker-alias"
            alias.symlink_to(core, target_is_directory=True)
            with self.assertRaises(BangkokBankRateWorkerSpecError):
                separate_worker_spec(worker_state_dir=alias, core_data_dir=core)
