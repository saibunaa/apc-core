#!/usr/bin/env python3
"""Deterministic, candidate-only APC Core Mini release runner.

This runner deliberately separates preflight/build/validate from the explicit
one-upstream Caddy promote/rollback operations.  It plans by default; every
side effect requires ``--execute``.  It never creates host ports, restarts a
service, or deletes a directory tree.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
from typing import Iterable

SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
GITHUB_COMMIT_ARCHIVE = re.compile(
    r"^https://github\.com/saibunaa/apc-core/archive/([0-9a-f]{40})\.tar\.gz$"
)


class ReleaseError(RuntimeError):
    """A failed closed release precondition."""


@dataclass(frozen=True)
class CandidateIdentity:
    root_name: str
    project_name: str
    container_name: str


def candidate_identity(git_sha: str, stamp: str) -> CandidateIdentity:
    if not GIT_SHA.fullmatch(git_sha):
        raise ReleaseError("release Git SHA must be exactly 40 lower-case hexadecimal characters")
    if not re.fullmatch(r"\d{8}T\d{6}Z", stamp):
        raise ReleaseError("candidate timestamp must be UTC in YYYYMMDDTHHMMSSZ form")
    name = f"apc-core-mini-{git_sha[:10]}-{stamp}"
    return CandidateIdentity(name, name, name)


def resolve_archive_url(archive_url: str | None, release_git_sha: str) -> str:
    """Accept only an immutable archive for the declared full release commit."""
    if not GIT_SHA.fullmatch(release_git_sha):
        raise ReleaseError("release Git SHA must be exactly 40 lower-case hexadecimal characters")
    if archive_url is None:
        return f"https://github.com/saibunaa/apc-core/archive/{release_git_sha}.tar.gz"
    match = GITHUB_COMMIT_ARCHIVE.fullmatch(archive_url)
    if not match or match.group(1) != release_git_sha:
        raise ReleaseError("GitHub archive URL must identify the exact release Git SHA")
    return archive_url


def verify_sha256(path: Path, expected: str, *, label: str) -> str:
    if not SHA256.fullmatch(expected):
        raise ReleaseError(f"{label} SHA-256 must be exactly 64 lower-case hexadecimal characters")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise ReleaseError(f"{label} SHA-256 mismatch: expected {expected}, got {digest}")
    return digest


def tree_manifest(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ReleaseError(f"manifest root is not a directory: {root}")
    return {
        file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(root.rglob("*"))
        if file.is_file()
    }


def verify_tree_manifest(root: Path, expected: dict[str, str]) -> None:
    actual = tree_manifest(root)
    if actual != expected:
        raise ReleaseError("manifest tree mismatch; accepted state is not byte-exact")


def assert_sidecar_free(snapshot: Path) -> None:
    offenders = [Path(str(snapshot) + suffix) for suffix in ("-wal", "-shm", ".wal", ".shm")]
    present = [str(path) for path in offenders if path.exists()]
    if present:
        raise ReleaseError("legacy snapshot sidecar files are forbidden: " + ", ".join(present))


def safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ReleaseError("GitHub archive contains an unsafe extraction path")
        tar.extractall(destination, filter="data")


def run(
    command: Iterable[str], *, execute: bool, cwd: Path | None = None, env: dict[str, str] | None = None
) -> None:
    rendered = shlex.join(list(command))
    print(f"PLAN: {rendered}")
    if execute:
        subprocess.run(list(command), cwd=cwd, env=env, check=True)


def require_owner_only(root: Path) -> None:
    mode = stat.S_IMODE(root.stat().st_mode)
    if mode != 0o700:
        raise ReleaseError(f"candidate root must be owner-only (0700), got {oct(mode)}: {root}")


def require_code_readable(root: Path) -> None:
    for path in root.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir() and mode & 0o005 != 0o005:
            raise ReleaseError(f"extracted code directory is not readable/traversable: {path}")
        if path.is_file() and mode & 0o004 == 0:
            raise ReleaseError(f"extracted code file is not world-readable: {path}")


def discover_core_source(extracted_root: Path) -> Path:
    candidates = [
        path
        for path in extracted_root.iterdir()
        if path.is_dir() and (path / "Dockerfile").is_file() and (path / "apc_core").is_dir()
    ]
    if len(candidates) != 1:
        raise ReleaseError("GitHub archive must contain exactly one APC Core source root")
    return candidates[0]


def verify_archive_commit_identity(code_root: Path, release_git_sha: str) -> None:
    expected_name = f"apc-core-{release_git_sha}"
    if code_root.name != expected_name:
        raise ReleaseError(
            f"GitHub archive commit identity mismatch: expected {expected_name}, got {code_root.name}"
        )


def copy_accepted_state(source: Path, destination: Path) -> dict[str, str]:
    if not source.is_dir():
        raise ReleaseError(f"accepted-state source is not a directory: {source}")
    source_manifest = tree_manifest(source)
    if not source_manifest:
        raise ReleaseError("accepted-state source must not be empty")
    shutil.copytree(source, destination, copy_function=shutil.copyfile)
    verify_tree_manifest(destination, source_manifest)
    return source_manifest


def backup_core_source(source: Path, output: Path) -> str:
    """Make a candidate-only SQLite copy using sqlite3.Connection.backup() only."""
    if not source.is_file():
        raise ReleaseError(f"exact Core source SQLite file does not exist: {source}")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as source_connection:
        with sqlite3.connect(output) as candidate_connection:
            sqlite3.Connection.backup(source_connection, candidate_connection)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def verify_rendered_compose(rendered: dict) -> None:
    service = rendered["services"]["apc-core"]
    if service.get("user") != "1000:1000":
        raise ReleaseError("candidate image must run with UID:GID 1000:1000")
    if service.get("read_only") is not True:
        raise ReleaseError("candidate root filesystem must be read-only")
    if service.get("ports"):
        raise ReleaseError("candidate must not expose host ports")
    if "no-new-privileges:true" not in service.get("security_opt", []):
        raise ReleaseError("candidate must enable no-new-privileges")
    if "ALL" not in service.get("cap_drop", []):
        raise ReleaseError("candidate must drop all capabilities")


def compose_env(args: argparse.Namespace, candidate: Path, identity: CandidateIdentity, legacy_sha: str) -> dict[str, str]:
    return {
        "APC_CORE_IMAGE_TAG": identity.root_name,
        "APC_CORE_CONTAINER_NAME": identity.container_name,
        "APC_CORE_ACCEPTED_STATE_DIR": str(candidate / "accepted-state"),
        "APC_CORE_DATA_DIR": str(candidate / "core-data"),
        "APC_CORE_LEGACY_INVOICE_SNAPSHOT": str(candidate / "legacy" / "legacy.sqlite"),
        "APC_CORE_LEGACY_INVOICE_SHA256": legacy_sha,
        "APC_CORE_ALLOWED_MUTATION_ORIGINS": args.allowed_origin,
        "APC_CORE_DOCKER_NETWORK": args.caddy_network,
        "APC_CORE_CANDIDATE_RESTART_POLICY": "no",
    }


def require_common(args: argparse.Namespace) -> None:
    if not args.allowed_origin.startswith("https://"):
        raise ReleaseError("allowed origin must be an explicit HTTPS origin")
    if not Path(args.legacy_source_sqlite).is_file() and not args.dry_run:
        raise ReleaseError("legacy SQLite source must exist before execution")
    if not args.caddy_network or not args.upstream_name:
        raise ReleaseError("Caddy network and one upstream name are required provenance inputs")


def plan_preflight(args: argparse.Namespace, candidate: Path, identity: CandidateIdentity) -> None:
    require_common(args)
    archive_url = resolve_archive_url(args.github_archive_url, args.release_git_sha)
    archive = candidate / "github-source.tar.gz"
    source_root = candidate / "source"
    print(f"candidate root: {candidate} (0700 owner-only); project/container: {identity.project_name}")
    if not args.dry_run:
        candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
        require_owner_only(candidate)
    run(["curl", "--fail", "--location", "--proto", "=https", "--tlsv1.2", "--output", str(archive), archive_url], execute=not args.dry_run)
    if not args.dry_run:
        # The archive bytes are accepted only after the caller-provided exact GitHub archive SHA matches.
        verify_sha256(archive, args.github_archive_sha256, label="GitHub archive")
        source_root.mkdir(mode=0o755)
        safe_extract(archive, source_root)
        code_root = discover_core_source(source_root)
        verify_archive_commit_identity(code_root, args.release_git_sha)
        require_code_readable(code_root)
        (candidate / "source-root.txt").write_text(str(code_root.relative_to(candidate)) + "\n", encoding="utf-8")


def plan_build(args: argparse.Namespace, candidate: Path, identity: CandidateIdentity) -> None:
    require_common(args)
    source_root = candidate / "source"
    code_root = source_root
    if not args.dry_run:
        code_root = discover_core_source(source_root)
    run(["docker", "build", "--pull=false", "--tag", identity.root_name, str(code_root)], execute=not args.dry_run)
    # Image-level UID 1000 import gate: import must work under the production container identity.
    run(["docker", "run", "--rm", "--user", "1000:1000", identity.root_name, "python3", "-c", "import apc_core.server"], execute=not args.dry_run)


def plan_validate(args: argparse.Namespace, candidate: Path, identity: CandidateIdentity) -> None:
    require_common(args)
    accepted = candidate / "accepted-state"
    core_copy = candidate / "core-data" / "core.sqlite"
    legacy = candidate / "legacy" / "legacy.sqlite"
    source = Path(args.core_source_sqlite)
    if not args.dry_run:
        accepted_manifest = copy_accepted_state(Path(args.accepted_state_source), accepted)
        exact_source_digest = backup_core_source(source, core_copy)
        if not args.legacy_logical_backup_command:
            raise ReleaseError("legacy logical backup command is required for execution")
        # Command receives only named source/output arguments and must produce a WAL-aware logical SQLite snapshot.
        legacy.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        run([*args.legacy_logical_backup_command, "--source", args.legacy_source_sqlite, "--output", str(legacy)], execute=True)
        assert_sidecar_free(legacy)
        legacy_sha = hashlib.sha256(legacy.read_bytes()).hexdigest()
        verify_tree_manifest(accepted, accepted_manifest)
        provenance = {"core_source": str(source), "core_copy_sha256": exact_source_digest, "legacy_source_sqlite": args.legacy_source_sqlite, "legacy_snapshot_sha256": legacy_sha}
        (candidate / "provenance.json").write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
    else:
        legacy_sha = "0" * 64
    environment = compose_env(args, candidate, identity, legacy_sha)
    rendered_command = ["docker", "compose", "--project-name", identity.project_name, "-f", "docker-compose.mini.yml", "config", "--format", "json"]
    print("PLAN ENV: " + json.dumps(environment, sort_keys=True))
    runtime_env = {**os.environ, **environment}
    run(rendered_command, execute=not args.dry_run, env=runtime_env)
    if not args.dry_run:
        rendered = subprocess.check_output(rendered_command, text=True, env=runtime_env)
        verify_rendered_compose(json.loads(rendered))
        run(["docker", "compose", "--project-name", identity.project_name, "-f", "docker-compose.mini.yml", "create"], execute=True, env=runtime_env)
        run(["docker", "compose", "--project-name", identity.project_name, "-f", "docker-compose.mini.yml", "start"], execute=True, env=runtime_env)
        # Candidate readiness runs from the Caddy network, never a host port.
        run(["docker", "run", "--rm", "--network", args.caddy_network, "curlimages/curl:8.10.1", "--fail", "--silent", "--show-error", f"http://{identity.container_name}:8769/items/api/items?limit=1"], execute=True)
        run(args.browser_validation_command, execute=True)


def replace_one_upstream(caddyfile: Path, current: str, replacement: str) -> Path:
    contents = caddyfile.read_text(encoding="utf-8")
    if contents.count(current) != 1:
        raise ReleaseError("Caddyfile must contain exactly one declared upstream to change")
    updated = contents.replace(current, replacement, 1)
    staged = caddyfile.with_suffix(caddyfile.suffix + ".candidate")
    staged.write_text(updated, encoding="utf-8")
    return staged


def plan_promote_or_rollback(args: argparse.Namespace, candidate: Path, identity: CandidateIdentity, *, rollback: bool) -> None:
    require_common(args)
    if not args.caddyfile or not args.current_upstream or not args.replacement_upstream:
        raise ReleaseError("Caddyfile and exact current/replacement upstream values are required")
    action = "rollback" if rollback else "promote"
    print(f"{action}: Caddy cutover is an explicit one-upstream action; it is never called by validation.")
    staged = Path(args.caddyfile).with_suffix(Path(args.caddyfile).suffix + ".candidate")
    if not args.dry_run:
        staged = replace_one_upstream(Path(args.caddyfile), args.current_upstream, args.replacement_upstream)
    run(["caddy", "validate", "--config", str(staged)], execute=not args.dry_run)
    if not args.dry_run:
        shutil.copyfile(staged, args.caddyfile)
        run(["caddy", "reload", "--config", args.caddyfile], execute=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--github-archive-url")
    common.add_argument("--github-archive-sha256", required=True)
    common.add_argument("--release-git-sha", required=True)
    common.add_argument("--candidate-base", default="/srv/apc-core-candidates")
    common.add_argument("--candidate-timestamp", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    common.add_argument("--legacy-source-sqlite", required=True)
    common.add_argument("--accepted-state-source", required=True)
    common.add_argument("--core-source-sqlite", default="/REQUIRED/EXACT/CORE.sqlite")
    common.add_argument("--legacy-logical-backup-command", nargs="+", default=[])
    common.add_argument("--allowed-origin", required=True)
    common.add_argument("--caddy-network", required=True)
    common.add_argument("--upstream-name", required=True)
    common.add_argument("--browser-validation-command", nargs="+", default=["false"])
    common.add_argument("--dry-run", action="store_true", default=True)
    common.add_argument("--execute", action="store_false", dest="dry_run")
    for phase in ("preflight", "build", "validate"):
        subparsers.add_parser(phase, parents=[common])
    for phase in ("promote", "rollback"):
        promotion = subparsers.add_parser(phase, parents=[common])
        promotion.add_argument("--caddyfile", required=True)
        promotion.add_argument("--current-upstream", required=True)
        promotion.add_argument("--replacement-upstream", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        candidate_base = Path(args.candidate_base)
        if not candidate_base.is_absolute():
            raise ReleaseError("candidate base must be an absolute, operator-owned path")
        identity = candidate_identity(args.release_git_sha, args.candidate_timestamp)
        candidate = candidate_base / identity.root_name
        if args.phase == "preflight":
            plan_preflight(args, candidate, identity)
        elif args.phase == "build":
            plan_build(args, candidate, identity)
        elif args.phase == "validate":
            plan_validate(args, candidate, identity)
        else:
            plan_promote_or_rollback(args, candidate, identity, rollback=args.phase == "rollback")
    except (ReleaseError, OSError, subprocess.CalledProcessError, sqlite3.Error) as error:
        print(f"FAIL-CLOSED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
