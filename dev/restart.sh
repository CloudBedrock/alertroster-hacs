#!/usr/bin/env bash
# Restart HA without syncing (config change, or clearing a wedged entry).
set -euo pipefail
cd "$(dirname "$0")/.."
. dev/_env.sh
ssh "$HA_HOST" "docker restart $HA_CONTAINER >/dev/null; until curl -sS -o /dev/null -m 3 http://localhost:8123/ 2>/dev/null; do sleep 3; done"
echo "restarted, $HA_URL is answering"
