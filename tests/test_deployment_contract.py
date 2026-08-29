"""Static release-contract checks; dependency-free so they also run in CI."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
CI_REQUIREMENTS_PATH = ROOT / "requirements-ci.txt"


class DeploymentContractTests(unittest.TestCase):
    def test_compose_requires_and_passes_explicit_mutation_origin_seam(self):
        compose = COMPOSE_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "APC_CORE_ALLOWED_MUTATION_ORIGINS: ${APC_CORE_ALLOWED_MUTATION_ORIGINS:?set APC_CORE_ALLOWED_MUTATION_ORIGINS}",
            compose,
        )
        self.assertNotIn("APC_CORE_ALLOWED_MUTATION_ORIGINS: http", compose)
        self.assertNotIn("APC_CORE_ALLOWED_MUTATION_ORIGINS: https", compose)

    def test_compose_retains_private_hardening_and_no_host_port_contract(self):
        compose = COMPOSE_PATH.read_text(encoding="utf-8")

        for required in (
            'user: "1000:1000"',
            "read_only: true",
            "- /tmp:mode=1777",
            "- /home/sai/projects/apc-core/state:/state:ro",
            "- /home/sai/projects/apc-core/core-data:/core-data:rw",
            "- apc-program-preview-net",
            "external: true",
            "- no-new-privileges:true",
            "cap_drop:",
            "- ALL",
        ):
            self.assertIn(required, compose)
        self.assertNotIn("ports:", compose)

    def test_ci_runs_pytest_from_hash_checked_dependencies(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        ci_requirements = CI_REQUIREMENTS_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "python -m pip install --require-hashes -r requirements-ci.txt", workflow
        )
        self.assertNotIn("pip install --upgrade pip pytest", workflow)
        self.assertRegex(ci_requirements, r"(?m)^pytest==\d+\.\d+\.\d+ \\$")
        self.assertGreaterEqual(ci_requirements.count("--hash=sha256:"), 2)

    def test_ci_requirements_include_the_pytest_linux_dependency_closure(self):
        ci_requirements = CI_REQUIREMENTS_PATH.read_text(encoding="utf-8")
        pinned_names = {
            line.split("==", 1)[0]
            for line in ci_requirements.splitlines()
            if "==" in line
        }

        self.assertSetEqual(
            {"pytest", "iniconfig", "packaging", "pluggy", "Pygments"},
            pinned_names,
        )
        self.assertRegex(
            ci_requirements,
            r"(?m)^Pygments==\d+\.\d+\.\d+ \\\n    --hash=sha256:[0-9a-f]{64}$",
        )

    def test_ci_uses_least_privilege_pinned_actions_and_nonpersistent_checkout(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertRegex(
            workflow,
            r"uses: actions/checkout@[0-9a-f]{40} # v4\.\d+\.\d+",
        )
        self.assertIn("persist-credentials: false", workflow)
        self.assertRegex(
            workflow,
            r"uses: actions/setup-python@[0-9a-f]{40} # v5\.\d+\.\d+",
        )

    def test_ci_fails_compose_config_when_required_origin_is_unset_or_empty(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn(
            "env -u APC_CORE_ALLOWED_MUTATION_ORIGINS docker compose config --quiet",
            workflow,
        )
        self.assertIn(
            "APC_CORE_ALLOWED_MUTATION_ORIGINS= docker compose config --quiet",
            workflow,
        )

    def test_ci_asserts_effective_rendered_compose_dummy_origin_and_no_host_ports(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("APC_CORE_ALLOWED_MUTATION_ORIGINS=https://ci.invalid docker compose config", workflow)
        self.assertIn("APC_CORE_ALLOWED_MUTATION_ORIGINS: https://ci.invalid", workflow)
        self.assertIn("grep -Eq '^[[:space:]]*ports:'", workflow)
        self.assertNotIn("${{ secrets.", workflow)


if __name__ == "__main__":
    unittest.main()
