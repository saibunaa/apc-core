"""Contract tests for the canonical APC Core Mini candidate release runner."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tools" / "apc_mini_release.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("apc_mini_release", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MiniReleaseRunnerTests(unittest.TestCase):
    def test_default_cli_is_dry_run_plan_and_never_promotes(self):
        runner = load_runner()
        parser = runner.build_parser()
        args = parser.parse_args(
            [
                "preflight",
                "--github-archive-sha256",
                "a" * 64,
                "--legacy-mdb",
                "/input/legacy.mdb",
                "--accepted-state-source",
                "/input/accepted-state",
                "--allowed-origin",
                "https://mini.example.invalid",
                "--caddy-network",
                "mini-private",
                "--upstream-name",
                "apc-core-candidate",
            ]
        )

        self.assertTrue(args.dry_run)
        self.assertEqual(args.phase, "preflight")
        self.assertFalse(getattr(args, "promote", False))

    def test_candidate_identity_is_unique_and_deterministic_from_release_sha(self):
        runner = load_runner()

        identity = runner.candidate_identity("f" * 40, "20260906T120000Z")

        self.assertEqual(identity.root_name, "apc-core-mini-ffffffffff-20260906T120000Z")
        self.assertEqual(identity.project_name, "apc-core-mini-ffffffffff-20260906T120000Z")
        self.assertEqual(identity.container_name, "apc-core-mini-ffffffffff-20260906T120000Z")
        self.assertNotEqual(identity.root_name, "apc-core")

    def test_archive_digest_mismatch_fails_closed_before_extraction(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            archive.write_bytes(b"candidate archive")

            with self.assertRaisesRegex(runner.ReleaseError, "SHA-256 mismatch"):
                runner.verify_sha256(archive, "0" * 64, label="GitHub archive")

    def test_manifest_tree_verification_rejects_byte_change(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "accepted-state"
            root.mkdir()
            artifact = root / "accepted_snapshot.json"
            artifact.write_bytes(b'{"accepted":true}\n')
            manifest = runner.tree_manifest(root)
            artifact.write_bytes(b'{"accepted":false}\n')

            with self.assertRaisesRegex(runner.ReleaseError, "manifest tree mismatch"):
                runner.verify_tree_manifest(root, manifest)

    def test_readonly_snapshot_is_hash_pinned_and_sidecar_free(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "legacy.sqlite"
            snapshot.write_bytes(b"sqlite candidate")
            (root / "legacy.sqlite-wal").write_bytes(b"must reject")

            with self.assertRaisesRegex(runner.ReleaseError, "sidecar"):
                runner.assert_sidecar_free(snapshot)
            (root / "legacy.sqlite-wal").unlink()
            digest = runner.verify_sha256(
                snapshot,
                hashlib.sha256(b"sqlite candidate").hexdigest(),
                label="Legacy snapshot",
            )
            self.assertEqual(digest, hashlib.sha256(b"sqlite candidate").hexdigest())

    def test_release_source_has_required_safety_boundaries(self):
        source = RUNNER_PATH.read_text(encoding="utf-8")

        for required in (
            "sqlite3.Connection.backup(",
            "--github-archive-sha256",
            "--legacy-mdb",
            "--allowed-origin",
            "--caddy-network",
            "--upstream-name",
            "--caddyfile",
            "preflight",
            "build",
            "validate",
            "promote",
            "rollback",
            "--dry-run",
            "--execute",
            "docker", "compose", "config", "--format", "json",
            'service.get("user") != "1000:1000"',
            'service.get("read_only") is not True',
            'service.get("ports")',
            "no-new-privileges:true",
            "cap_drop",
            "curl",
            "browser-validation-command",
            "Caddy cutover is an explicit one-upstream action",
        ):
            self.assertIn(required, source)
        self.assertNotIn("rm -rf", source)
        self.assertNotIn("restart caddy", source.lower())
        self.assertNotIn("docker compose up", source.lower())


if __name__ == "__main__":
    unittest.main()
