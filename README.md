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

## Canonical deterministic Mini release runner

`tools/apc_mini_release.py` is the only release runner for an APC Core Mini **candidate**. It is deliberately plan-only by default and has separately invoked `preflight`, `build`, `validate`, `promote`, and `rollback` phases. It never performs promotion as part of validation.

Every execution requires operator-provided provenance: an exact GitHub archive SHA-256, full release Git SHA, unique UTC candidate timestamp, exact Core SQLite source, accepted-state source, Legacy SQLite source, an approved WAL-aware logical-backup executable, Caddy network/upstream name, allowed HTTPS origin, and a semantic browser validation command. The archive URL is derived only from the full release Git SHA unless explicitly supplied as the equivalent immutable commit archive URL; branch URLs are rejected. After extraction, the archive root must identify that exact commit. The runner creates a unique owner-only candidate root and project/container name, verifies archive and state-copy manifests byte-for-byte, makes the Core copy exclusively with `sqlite3.Connection.backup()`, requires a sidecar-free hash-pinned Legacy SQLite snapshot, checks the image import as UID 1000, and validates rendered compose hardening/no-host-port conditions before it starts a candidate on the Caddy network.

Example **plan** (no writes, Docker, Caddy, or Mini changes):

```bash
python3 tools/apc_mini_release.py preflight \
  --github-archive-sha256 '<exact archive sha256>' \
  --release-git-sha '<exact 40-char git sha>' \
  --candidate-timestamp 'YYYYMMDDTHHMMSSZ' \
  --legacy-source-sqlite '/required/legacy.sqlite' \
  --accepted-state-source '/required/accepted-state' \
  --core-source-sqlite '/required/core.sqlite' \
  --allowed-origin 'https://mini.example.invalid' \
  --caddy-network 'required-caddy-network' \
  --upstream-name 'required-candidate-upstream'
```

Add `--execute` only to the specific approved phase. `promote` and `rollback` additionally require `--caddyfile`, `--current-upstream`, and `--replacement-upstream`; they stage and validate an exact **one-upstream** Caddyfile replacement before `caddy reload`. They never restart Caddy or any service. A new human approval is required for each of those explicit cutover commands.

## Developer-local invoice draft reconciliation

The invoice-draft reconciliation evidence is developer-local and uses only synthetic fixtures:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_invoice_draft_reconciliation -v
```

It verifies draft-only preview/save provenance, explicit staff resolution of opaque AWB conflicts, idempotent replay, and source-hash mismatch denial. It does **not** issue or number invoices, approve, print, export, account, sync, write AWB or legacy data, access MDB/NAS/live data, or claim deployment or production readiness.

## Shared Order/Invoice workspace

When its bounded readers are available, the main menu opens `Order/Invoice`: a keyboard-first browser for **SOURCE ORDER · READ-ONLY**, **SOURCE INVOICE · READ-ONLY**, and **CORE DRAFT · LOCAL** records. It uses only `GET /order-invoice/api/browse`; it has no source mutation controls and does not infer links between source orders and source invoices. Existing `/orders/` and `/invoices/` routes remain compatible.
