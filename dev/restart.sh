#!/usr/bin/env bash
# Restart HA without syncing (config change, or clearing a wedged entry).
set -euo pipefail
cd "$(dirname "$0")/.."
. dev/_env.sh
ssh "$HA_HOST" "docker restart $HA_CONTAINER >/dev/null"
wait_for_ha
echo "restarted, $HA_URL is answering"
