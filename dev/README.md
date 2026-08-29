# Dev loop

Home Assistant for developing this integration runs on **ubuntu-dev**, not on your
workstation: the container there is already host-networked, which is what lets
zeroconf discovery of `_alertroster-receiver._tcp.local.` work at all, and it sits on
the same LAN as the stations.

| | |
|---|---|
| HA UI | <http://ubuntu-dev:8123> |
| Stack | `/opt/homeassistant/docker-compose.yml` on ubuntu-dev (HA + esphome + mosquitto) |
| HA config | `/opt/homeassistant/homeassistant` -> `/config` in the container |
| This integration | `/opt/homeassistant/src/alertroster` -> `/config/custom_components/alertroster` |

That last bind-mount was added to the stack's compose file for this project. The
source under `src/` is a copy pushed by `dev/sync.sh`; it is not a checkout, so never
edit it there -- the next sync deletes whatever you wrote.

This is a **shared rig**: esphome and mosquitto run beside HA, and a ZHA entry errors
on startup for unrelated reasons (`Network settings do not match most recent backup`).
Restart only the `homeassistant` container, never the whole stack.

## The loop

```sh
./dev/sync.sh          # rsync the integration up, restart HA, wait for it to answer (<15s)
./dev/sync.sh -n       # sync without restarting
./dev/restart.sh       # restart without syncing
./dev/logs.sh          # follow the log, filtered to this integration
./dev/logs.sh 'zeroconf|websocket'   # ...or to anything else
./dev/stations.sh      # what stations are on the LAN, and do they answer
```

Python is not reloaded in place, so **every source change needs a restart** — there is
no faster path.

Override the target with `HA_HOST`, `HA_CONTAINER`, `HA_SRC`, `HA_URL` (see
`dev/_env.sh`); the defaults point at ubuntu-dev.

## Logging

`configuration.yaml` on the rig sets `custom_components.alertroster: debug` and
deliberately leaves the global default alone, because other people's integrations
share this instance. REQUIREMENTS.md §6 requires that the `lat_` pairing token never
reaches a log line — debug level here is where that regression would show up, so watch
for it while testing pairing.

## Stations

`./dev/stations.sh` finds them. Two facts it encodes, both easy to get wrong:

- **The port is not always 4747.** `studio` (the Mac) is on 4747; `om` is on 4798.
  Read the port off the mDNS announcement.
- **There is no unauthenticated endpoint yet.** `GET /v1/discover` is a 404 as of
  2026-08-28 (REQUIREMENTS.md §5 item 1 asks the station to add it). Every other
  endpoint answers `401 invalid_credentials` without a token, so today **a 401 is the
  liveness signal** — a reachable station answers 401, and only a connection failure
  means unreachable.

Before a pairing test, on the station: **Service -> Pairing**, tick *Accept sources
from the LAN*, Apply, then *Pair a new source* for the 8-digit code. The code is valid
for five minutes and three wrong attempts close the window.
