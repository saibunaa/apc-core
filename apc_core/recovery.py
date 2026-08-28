"""Isolated, local-only Core snapshot recovery engine.

The browser never supplies recovery authority.  This module has no HTTP routes and
operates only on Core-owned SQLite files in the supplied test/workspace directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class RecoveryError(ValueError):
    """A recovery request was rejected before any state switch."""


_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise RecoveryError("snapshot validation failed") from error
    if result != ("ok",):
        raise RecoveryError("snapshot validation failed")


_CORE_SCHEMA_REQUIREMENTS = {
    "item_overrides": {"item_id"},
    "core_users": {"username", "role", "active"},
    "activity": {"id", "item_id", "changes_json", "actor_username", "created_at"},
    "item_backfill_quarantine": {"id", "item_id", "reason", "active", "created_at"},
    "core_item_drafts": {"item_id", "original_item_id", "item_json", "created_at"},
    "core_items": {"item_id", "source_item_id", "core_created", "archived"},
}


def _validate_core_schema(path: Path) -> None:
    """Accept only integrity-valid snapshots that retain Core's mutable schema."""
    _validate_sqlite(path)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {
                row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            for table, required_columns in _CORE_SCHEMA_REQUIREMENTS.items():
                if table not in tables:
                    raise RecoveryError("snapshot validation failed")
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if not required_columns.issubset(columns):
                    raise RecoveryError("snapshot validation failed")
        finally:
            connection.close()
    except (sqlite3.Error, RecoveryError) as error:
        if isinstance(error, RecoveryError):
            raise
        raise RecoveryError("snapshot validation failed") from error


class RecoveryService:
    """Registry-backed atomic recovery for an isolated Core workspace only."""

    def __init__(self, *, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.current_path = self.data_dir / "apc_core.sqlite"
        self.snapshot_dir = self.data_dir / "accepted-snapshots"
        self.generation_dir = self.data_dir / "recovery-generations"
        self.snapshot_dir.mkdir(exist_ok=True)
        self.generation_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._registry = sqlite3.connect(self.data_dir / "recovery_registry.sqlite", check_same_thread=False)
        self._registry.execute(
            "CREATE TABLE IF NOT EXISTS accepted_snapshots ("
            "snapshot_id TEXT PRIMARY KEY, artifact_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL UNIQUE, "
            "provenance TEXT NOT NULL, registered_at TEXT NOT NULL)"
        )
        self._registry.execute(
            "CREATE TABLE IF NOT EXISTS recovery_audit ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, reason TEXT NOT NULL, "
            "snapshot_id TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, prior_generation_path TEXT NOT NULL, "
            "new_generation_path TEXT NOT NULL, validation_result TEXT NOT NULL, operation TEXT NOT NULL DEFAULT 'restore', "
            "created_at TEXT NOT NULL)"
        )
        audit_columns = {row[1] for row in self._registry.execute("PRAGMA table_info(recovery_audit)")}
        if "operation" not in audit_columns:
            self._registry.execute("ALTER TABLE recovery_audit ADD COLUMN operation TEXT NOT NULL DEFAULT 'restore'")
        self._registry.commit()

    def register_accepted_snapshot(self, *, snapshot_id: str, artifact_path: Path, sha256: str, provenance: str) -> None:
        if type(snapshot_id) is not str or _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
            raise RecoveryError("invalid snapshot id")
        if type(sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise RecoveryError("invalid snapshot hash")
        if type(provenance) is not str or not provenance.strip() or len(provenance) > 256:
            raise RecoveryError("invalid snapshot provenance")
        source = Path(artifact_path).resolve(strict=True)
        if not source.is_file() or _sha256(source) != sha256:
            raise RecoveryError("snapshot hash mismatch")
        _validate_core_schema(source)
        destination = self.snapshot_dir / f"{sha256}.sqlite"
        with self._lock:
            row = self._registry.execute(
                "SELECT snapshot_id, sha256, provenance FROM accepted_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is not None:
                if row == (snapshot_id, sha256, provenance):
                    return
                raise RecoveryError("snapshot id is immutable")
            if not destination.exists():
                with tempfile.NamedTemporaryFile(dir=self.snapshot_dir, delete=False) as staged:
                    staged_path = Path(staged.name)
                try:
                    shutil.copyfile(source, staged_path)
                    if _sha256(staged_path) != sha256:
                        raise RecoveryError("snapshot copy validation failed")
                    os.chmod(staged_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                    os.replace(staged_path, destination)
                finally:
                    staged_path.unlink(missing_ok=True)
            if _sha256(destination) != sha256:
                raise RecoveryError("snapshot registry validation failed")
            with self._registry:
                self._registry.execute(
                    "INSERT INTO accepted_snapshots VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, str(destination), sha256, provenance, datetime.now(timezone.utc).isoformat()),
                )

    def accepted_snapshot_path(self, snapshot_id: str) -> Path:
        row = self._registry.execute(
            "SELECT artifact_path FROM accepted_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise RecoveryError("unknown accepted snapshot")
        return Path(row[0])

    def prepare_restore(self, *, snapshot_id: str, actor: str, reason: str, confirmation: str, maintenance=None) -> dict[str, str | bool]:
        """Close every running Core module, then switch and require supervisor restart."""
        if not callable(maintenance):
            raise RecoveryError("recovery requires maintenance closure before switch")
        if type(actor) is not str or not actor or len(actor) > 32:
            raise RecoveryError("invalid actor")
        if type(reason) is not str or not reason.strip() or len(reason) > 500:
            raise RecoveryError("invalid recovery reason")
        if confirmation != snapshot_id:
            raise RecoveryError("recovery confirmation mismatch")
        with self._lock:
            try:
                maintenance()
            except Exception as error:
                raise RecoveryError("maintenance closure failed") from error
            row = self._registry.execute(
                "SELECT artifact_path, sha256 FROM accepted_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise RecoveryError("unknown accepted snapshot")
            snapshot_path, expected_hash = Path(row[0]), row[1]
            if not snapshot_path.is_file() or _sha256(snapshot_path) != expected_hash:
                raise RecoveryError("snapshot registry validation failed")
            _validate_core_schema(snapshot_path)
            if not self.current_path.is_file():
                raise RecoveryError("current Core generation is missing")
            _validate_core_schema(self.current_path)
            generation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            prior_dir = self.generation_dir / generation_id
            prior_dir.mkdir()
            prior_path = prior_dir / "apc_core.sqlite"
            shutil.copy2(self.current_path, prior_path)
            if _sha256(prior_path) != _sha256(self.current_path):
                raise RecoveryError("pre-restore backup validation failed")
            with tempfile.NamedTemporaryFile(dir=self.data_dir, delete=False) as staged:
                staged_path = Path(staged.name)
            try:
                shutil.copyfile(snapshot_path, staged_path)
                _validate_core_schema(staged_path)
                if _sha256(staged_path) != expected_hash:
                    raise RecoveryError("replacement validation failed")
                os.replace(staged_path, self.current_path)
            finally:
                staged_path.unlink(missing_ok=True)
            result = {
                "prior_generation_path": str(prior_path),
                "new_generation_path": str(self.current_path),
                "validation_result": "passed",
                "restart_required": True,
            }
            with self._registry:
                self._registry.execute(
                    "INSERT INTO recovery_audit (actor, reason, snapshot_id, snapshot_sha256, prior_generation_path, "
                    "new_generation_path, validation_result, operation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (actor, reason, snapshot_id, expected_hash, str(prior_path), str(self.current_path), "passed", "restore",
                     datetime.now(timezone.utc).isoformat()),
                )
            return result

    def rollback(self, *, actor: str, reason: str, confirmation: str, maintenance=None) -> dict[str, str | bool]:
        if not callable(maintenance):
            raise RecoveryError("rollback requires maintenance closure before switch")
        if type(actor) is not str or not actor or len(actor) > 32:
            raise RecoveryError("invalid actor")
        if type(reason) is not str or not reason.strip() or len(reason) > 500:
            raise RecoveryError("invalid recovery reason")
        with self._lock:
            try:
                maintenance()
            except Exception as error:
                raise RecoveryError("maintenance closure failed") from error
            row = self._registry.execute(
                "SELECT id, prior_generation_path, operation FROM recovery_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None or row[2] != "restore" or confirmation != row[1]:
                raise RecoveryError("rollback confirmation mismatch")
            prior_path = Path(row[1]).resolve(strict=True)
            if prior_path.parent.parent != self.generation_dir or not prior_path.is_file():
                raise RecoveryError("invalid prior generation")
            _validate_core_schema(prior_path)
            expected_hash = _sha256(prior_path)
            with tempfile.NamedTemporaryFile(dir=self.data_dir, delete=False) as staged:
                staged_path = Path(staged.name)
            try:
                shutil.copyfile(prior_path, staged_path)
                _validate_core_schema(staged_path)
                if _sha256(staged_path) != expected_hash:
                    raise RecoveryError("rollback validation failed")
                os.replace(staged_path, self.current_path)
            finally:
                staged_path.unlink(missing_ok=True)
            result = {"prior_generation_path": str(prior_path), "new_generation_path": str(self.current_path), "validation_result": "passed", "restart_required": True}
            with self._registry:
                self._registry.execute(
                    "INSERT INTO recovery_audit (actor, reason, snapshot_id, snapshot_sha256, prior_generation_path, "
                    "new_generation_path, validation_result, operation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (actor, reason, f"rollback:{row[0]}", expected_hash, str(prior_path), str(self.current_path), "passed", "rollback",
                     datetime.now(timezone.utc).isoformat()),
                )
            return result

    def accepted_snapshots(self) -> list[dict[str, str]]:
        columns = ("snapshot_id", "sha256", "provenance")
        return [dict(zip(columns, row)) for row in self._registry.execute(
            "SELECT snapshot_id, sha256, provenance FROM accepted_snapshots ORDER BY snapshot_id"
        )]

    def audit_entries(self) -> list[dict[str, str]]:
        columns = ("actor", "reason", "snapshot_id", "snapshot_sha256", "prior_generation_path", "new_generation_path", "validation_result", "operation")
        return [dict(zip(columns, row)) for row in self._registry.execute(
            "SELECT actor, reason, snapshot_id, snapshot_sha256, prior_generation_path, new_generation_path, validation_result, operation "
            "FROM recovery_audit ORDER BY id"
        )]


class RecoveryAuthorizer:
    """Short-lived server-side PIN sessions for the isolated test panel only."""

    _session_ttl_seconds = 10 * 60

    def __init__(self, *, pin_hash: bytes | None, salt: bytes | None, test_mode: bool, state_path: Path | None = None):
        if test_mode is not True:
            raise RecoveryError("production recovery authorization is not configured")
        self._pin_hash = pin_hash
        self._salt = salt
        self._state_path = state_path
        self._sessions: dict[str, float] = {}
        self._attempts: dict[str, tuple[int, float]] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_test_pin(cls, pin: str) -> "RecoveryAuthorizer":
        if type(pin) is not str or not re.fullmatch(r"[0-9]{6,32}", pin):
            raise RecoveryError("invalid test recovery PIN")
        salt = os.urandom(16)
        return cls(pin_hash=hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2**14, r=8, p=1), salt=salt, test_mode=True)

    @classmethod
    def from_state_file(cls, state_path: Path) -> "RecoveryAuthorizer":
        if not state_path.exists():
            return cls(pin_hash=None, salt=None, test_mode=True, state_path=state_path)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            pin_hash = bytes.fromhex(state["pin_hash"])
            salt = bytes.fromhex(state["salt"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RecoveryError("invalid recovery authorization state") from error
        return cls(pin_hash=pin_hash, salt=salt, test_mode=True, state_path=state_path)

    @property
    def needs_setup(self) -> bool:
        return self._pin_hash is None

    def setup(self, *, pin: object, confirmation: object) -> None:
        if self._state_path is None or not self.needs_setup:
            raise RecoveryError("recovery setup is unavailable")
        if type(pin) is not str or pin != confirmation or not re.fullmatch(r"[0-9]{6,32}", pin):
            raise RecoveryError("invalid recovery PIN setup")
        salt = os.urandom(16)
        pin_hash = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        claim_path = self._state_path.with_name(self._state_path.name + ".setup-lock")
        try:
            claim_fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, stat.S_IRUSR | stat.S_IWUSR)
        except FileExistsError as error:
            raise RecoveryError("recovery setup is unavailable") from error
        try:
            if self._state_path.exists():
                raise RecoveryError("recovery setup is unavailable")
            with tempfile.NamedTemporaryFile(dir=self._state_path.parent, mode="w", encoding="utf-8", delete=False) as staged:
                staged.write(json.dumps({"pin_hash": pin_hash.hex(), "salt": salt.hex()}))
                staged_path = Path(staged.name)
            try:
                os.chmod(staged_path, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(staged_path, self._state_path)
            finally:
                staged_path.unlink(missing_ok=True)
        finally:
            os.close(claim_fd)
            claim_path.unlink(missing_ok=True)
        self._pin_hash, self._salt = pin_hash, salt

    @staticmethod
    def setup_html() -> str:
        return """<!doctype html><title>Set up Admin PIN</title><main><h1>Set up Admin PIN</h1><p>Create the PIN needed to open the Admin panel on this computer.</p><form id=\"setup-form\"><label>Create PIN <input name=\"pin\" inputmode=\"numeric\" required autofocus></label><label>Confirm PIN <input name=\"confirmation\" inputmode=\"numeric\" required></label><button>Save Admin PIN</button></form><p id=\"result\"></p></main><script>document.getElementById('setup-form').addEventListener('submit',async e=>{e.preventDefault();const r=await fetch('setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.currentTarget)))});document.getElementById('result').textContent=r.ok?'PIN saved. Enter it to continue.':'PIN was not saved.';if(r.ok)location.reload()});</script>"""

    def authenticate(self, *, pin: object, client_id: str) -> str | None:
        if type(pin) is not str or type(client_id) is not str or self._salt is None or self._pin_hash is None:
            return None
        now = time.monotonic()
        with self._lock:
            failures, retry_at = self._attempts.get(client_id, (0, 0.0))
            if now < retry_at:
                return None
            candidate = hashlib.scrypt(pin.encode("utf-8"), salt=self._salt, n=2**14, r=8, p=1)
            if not hmac.compare_digest(candidate, self._pin_hash):
                failures += 1
                self._attempts[client_id] = (failures, now + min(60.0, 2.0 ** min(failures, 5)))
                return None
            self._attempts.pop(client_id, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now + self._session_ttl_seconds
            return token

    def is_authorized(self, token: object) -> bool:
        if type(token) is not str:
            return False
        now = time.monotonic()
        with self._lock:
            expires_at = self._sessions.get(token)
            if expires_at is None or now >= expires_at:
                self._sessions.pop(token, None)
                return False
            return True

    @staticmethod
    def login_html() -> str:
        return """<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>APC Core · Recovery access</title><style>body{margin:0;background:#faf7f2;color:#24272b;font:14px -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}main{max-width:420px;margin:12vh auto;padding:24px;background:#fffdfa;border:1px solid #e5e1db;border-radius:16px;box-shadow:0 8px 24px #24272b12}label{display:grid;gap:6px;font-weight:600}input,button{padding:10px;border-radius:8px;font:inherit}input{border:1px solid #e5e1db}button{margin-top:16px;border:0;background:#1d6b57;color:white;font-weight:700}</style><main><h1>Recovery access</h1><p>Admin PIN required.</p><form id=\"login-form\"><label>Admin PIN <input name=\"pin\" inputmode=\"numeric\" autocomplete=\"current-password\" required autofocus></label><button>Continue</button></form><p id=\"result\" role=\"status\"></p></main><script>document.getElementById('login-form').addEventListener('submit',async event=>{event.preventDefault();const result=document.getElementById('result');const pin=new FormData(event.currentTarget).get('pin');const response=await fetch('login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})});if(response.ok){location.reload()}else{result.textContent='Access refused.'}});</script></html>"""

    @staticmethod
    def panel_html() -> str:
        return """<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>APC Core · Recovery</title><style>:root{--ink:#24272b;--paper:#fffdfa;--cream:#faf7f2;--line:#e5e1db;--accent:#1d6b57}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}.shell{max-width:680px;margin:36px auto;padding:24px;background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 24px #24272b12}h1{margin-top:0}label{display:grid;gap:5px;margin:12px 0;font-weight:600}input{padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit}button{padding:10px 14px;border:0;border-radius:8px;background:var(--accent);color:white;font:inherit;font-weight:700}#result{min-height:1.4em}</style><main class=\"shell\"><h1>Admin panel</h1><p>Use this private panel to manage saved safe copies of the local test system. Your staff name is recorded in the audit trail.</p><form id=\"restore-form\"><label>Accepted snapshot ID <input name=\"snapshot_id\" required autocomplete=\"off\"></label><label>Admin actor <input name=\"actor\" required autocomplete=\"off\"></label><label>Reason <input name=\"reason\" required maxlength=\"500\"></label><label>Type snapshot ID again <input name=\"confirmation\" required autocomplete=\"off\"></label><button>Restore accepted snapshot</button></form><form id=\"rollback-form\"><h2>Rollback latest restore</h2><label>Admin actor <input name=\"actor\" required autocomplete=\"off\"></label><label>Reason <input name=\"reason\" required maxlength=\"500\"></label><label>Type prior-generation path <input name=\"confirmation\" required autocomplete=\"off\"></label><button>Rollback</button></form><button id=\"audit\" type=\"button\">View audit</button><pre id=\"audit-output\"></pre><p id=\"result\" role=\"status\"></p></main><script>document.getElementById('restore-form').addEventListener('submit',async event=>{event.preventDefault();const result=document.getElementById('result');result.textContent='Preparing isolated restore…';const payload=Object.fromEntries(new FormData(event.currentTarget));try{const response=await fetch('restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});result.textContent=response.ok?'Restore validated and switched.':'Restore refused.'}catch{result.textContent='Restore unavailable.'}});document.getElementById('rollback-form').addEventListener('submit',async event=>{event.preventDefault();const result=document.getElementById('result');const payload=Object.fromEntries(new FormData(event.currentTarget));const response=await fetch('rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});result.textContent=response.ok?'Rollback validated and switched.':'Rollback refused.'});document.getElementById('audit').addEventListener('click',async()=>{const actor=document.querySelector('#rollback-form [name=actor]').value||document.querySelector('#restore-form [name=actor]').value;const response=await fetch('audit?actor='+encodeURIComponent(actor));document.getElementById('audit-output').textContent=response.ok?JSON.stringify(await response.json(),null,2):'Audit unavailable.'});</script></html>"""
