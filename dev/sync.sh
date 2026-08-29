#!/usr/bin/env bash
# The edit loop: push this checkout's integration to the HA rig and restart it.
#
# Python is not reloaded in place, so a restart is required after every source
# change -- there is no way to skip it. Pass -n to sync without restarting.
set -euo pipefail
cd "$(dirname "$0")/.."
. dev/_env.sh

restart=1
[ "${1:-}" = "-n" ] && restart=0

rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  custom_components/alertroster/ "$HA_HOST:$HA_SRC/"
echo "synced -> $HA_HOST:$HA_SRC"

if [ "$restart" = 1 ]; then
  ssh "$HA_HOST" "docker restart $HA_CONTAINER >/dev/null"
  # Wait for the API to answer rather than guessing: it comes back in well
  # under a minute, but a log tail started too early shows the previous run's
  # tail instead of this one.
  wait_for_ha
  echo "restarted, $HA_URL is answering"
fi
