"""Volatile, one-use server-held invoice draft previews."""

import copy
import secrets
import threading
import time


class InvoiceDraftPreviewRegistry:
    """Bounded in-memory capabilities; never persists or exposes source paths."""

    def __init__(self, *, max_pending=32, ttl_seconds=300, clock=None, token_factory=None):
        if type(max_pending) is not int or max_pending < 1 or type(ttl_seconds) is not int or ttl_seconds < 1:
            raise ValueError("invalid preview registry bounds")
        self._max_pending = max_pending
        self._ttl_seconds = ttl_seconds
        self._clock = time.monotonic if clock is None else clock
        self._token_factory = secrets.token_urlsafe if token_factory is None else token_factory
        self._pending = {}
        self._lock = threading.RLock()

    def _prune(self, now):
        for ref, (_, _, expiry) in tuple(self._pending.items()):
            if expiry <= now:
                del self._pending[ref]

    def issue(self, proposal, accepted_snapshot_sha256):
        if type(proposal) is not dict or type(proposal.get("ready_to_save")) is not bool:
            raise ValueError("only server-built proposals may be previewed")
        if type(accepted_snapshot_sha256) is not str or len(accepted_snapshot_sha256) != 64:
            raise ValueError("invalid accepted snapshot")
        now = self._clock()
        if type(now) not in (int, float):
            raise ValueError("invalid preview clock")
        with self._lock:
            self._prune(now)
            if len(self._pending) >= self._max_pending:
                raise ValueError("preview capacity exhausted")
            ref = self._token_factory()
            if type(ref) is not str or len(ref) < 16 or ref in self._pending:
                raise ValueError("invalid preview reference")
            self._pending[ref] = (copy.deepcopy(proposal), accepted_snapshot_sha256, now + self._ttl_seconds)
            return ref

    def consume(self, preview_ref):
        if type(preview_ref) is not str:
            return None
        now = self._clock()
        if type(now) not in (int, float):
            return None
        with self._lock:
            self._prune(now)
            entry = self._pending.pop(preview_ref, None)
            if entry is None:
                return None
            proposal, snapshot, _ = entry
            return copy.deepcopy(proposal), snapshot

    def clear(self):
        with self._lock:
            self._pending.clear()
