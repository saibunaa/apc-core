# APC Core — Phase 1

Local, desktop-first APC Core pilot.

## Boundary

- Reads only a local SQLite snapshot using SQLite `mode=ro`.
- Certifies a byte-for-byte copied, permission-read-only accepted SQLite artifact under `state/`, plus its acceptance manifest.
- Core-owned operational state is local only; no NAS writes, live MDB access, scheduler, public endpoint, or deployment is included.
- The development server binds only to `127.0.0.1` unless explicit container ingress is selected.
- Recovery is **disabled by default**. The test-only Recovery / Snapshot Restore panel requires an explicit server-side Admin PIN environment value and an explicit `APC_CORE_DATA_DIR`. Browser staff selection remains attribution only, never recovery authorization.
- Production PIN provisioning, live ingress exposure, and deployment remain separate approvals.

## Commands

```bash
python3 -m apc_core.certify --source /data/hermes-scratch/apc-mdb-shadow-live-nas/sqlite/latest.sqlite --output state/accepted_snapshot.json
python3 -m apc_core.server --manifest state/accepted_snapshot.json --port 8769
```

For an isolated recovery test workspace only, supply the PIN through the process environment; never commit or log it:

```bash
APC_CORE_DATA_DIR=/tmp/apc-core-test-state APC_CORE_RECOVERY_TEST_PIN='<test PIN>' \
  python3 -m apc_core.server --manifest state/accepted_snapshot.json --port 8769
```

## Mini candidate manifest

`docker-compose.mini.yml` is a **candidate-only** deployment contract for Mini; it is not a live deployment instruction. It remains inert until an operator explicitly supplies a candidate image tag, container name, accepted-state directory, Core-data directory, canonical allowed mutation origin, and (optionally, defaulting to `mini-host`) Docker network. It has no host-port mapping, uses an external private network, runs non-root with a read-only root filesystem, and defaults its candidate restart policy to `no`.

Use only non-secret environment provisioning outside the repository. Any use or promotion of this candidate requires a **fresh promotion gate**; do not reuse an earlier approval or infer live readiness from this manifest.
