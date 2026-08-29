#!/usr/bin/env bash
# Follow the HA log, filtered to what matters for this integration.
# Pass a regex to filter on something else:  ./dev/logs.sh 'zeroconf|websocket'
set -euo pipefail
cd "$(dirname "$0")/.."
. dev/_env.sh
filter="${1:-alertroster|config_entries|zeroconf|ERROR|WARNING}"
exec ssh -t "$HA_HOST" "docker logs -f --tail=200 $HA_CONTAINER 2>&1 | grep --line-buffered -Ei '$filter'"
