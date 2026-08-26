# APC Core — Phase 1

Local, desktop-first, read-only Item Explorer pilot.

## Boundary

- Reads only a local SQLite snapshot using SQLite `mode=ro`.
- Certifies a byte-for-byte copied, permission-read-only accepted SQLite artifact under `state/`, plus its acceptance manifest.
- No NAS writes, live MDB access, Caddy route, scheduler, public endpoint, order workflow, or database mutation.
- The development server binds only to `127.0.0.1`.

## Commands

```bash
python3 -m apc_core.certify --source /data/hermes-scratch/apc-mdb-shadow-live-nas/sqlite/latest.sqlite --output state/accepted_snapshot.json
python3 -m apc_core.server --manifest state/accepted_snapshot.json --port 8769
```
