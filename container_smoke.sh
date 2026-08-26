#!/usr/bin/env bash
set -euo pipefail
name="apc-core-smoke-${RANDOM}"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker build -q -t apc-core-readiness-test . >/dev/null
docker run -d --name "$name" --network none -v /tmp/apc-core-stage/state:/state:ro apc-core-readiness-test >/dev/null
for _ in $(seq 1 30); do
  if docker exec "$name" python3 -c 'from urllib.request import urlopen; r=urlopen("http://127.0.0.1:8769/items/api/items?limit=1", timeout=2); print("status=" + str(r.status)); print("body=" + r.read().decode()[:80])'; then
    docker inspect -f 'running={{.State.Running}} readonly_state={{range .Mounts}}{{if eq .Destination "/state"}}{{.RW}}{{end}}{{end}} network={{.HostConfig.NetworkMode}} ports={{json .NetworkSettings.Ports}}' "$name"
    exit 0
  fi
  sleep 1
done
docker logs "$name" >&2
exit 1
