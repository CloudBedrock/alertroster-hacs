# Working on this integration

The deployment target — and the real test — is **the home Home Assistant**. It is HA OS with
Supervisor; the config directory is `/config`. There is no other Home Assistant in this project.

This repo is public, so the machine is not named here. Keep its URL and an admin token in
`~/dev/ha/.env` as `HA_URL` and `HA_TOKEN` — that file is outside the repo and stays out of it.
The station's address comes off `./dev/stations.sh`.

| | |
|---|---|
| HA | `$HA_URL` |
| Station | studio-1 (the Mac), port 4747 |
| Admin token | `$HA_TOKEN` |

## Getting a build onto it

A custom integration is just a folder, so HACS is not required to try one. Only 80/443 are
reachable and port 22 is unpublished, so the files go in through the **Terminal & SSH** add-on's
web terminal (`/hassio/addon/core_ssh/info` → *Open Web UI* — the add-on pages are not under
Settings in the sidebar):

```sh
cd /config && mkdir -p custom_components && \
wget -qO /tmp/ar.tar.gz https://github.com/CloudBedrock/alertroster-hacs/archive/refs/heads/main.tar.gz && \
tar -xzf /tmp/ar.tar.gz -C /tmp && rm -rf custom_components/alertroster && \
cp -r /tmp/alertroster-hacs-main/custom_components/alertroster custom_components/
```

Then restart Home Assistant — `POST /api/services/homeassistant/restart`, which times out by
design; poll `/api/config` until `state == "RUNNING"`. **This is a production instance: say so
before restarting it.**

Python is not reloaded in place, so every source change needs a restart.

## Pairing without touching the UI

`alertroster` does not appear in `/api/config` components until an entry exists, so its absence
proves nothing. Start the flow instead:

1. `POST /api/config/config_entries/flow` `{"handler": "alertroster"}` → the `user` step
2. `POST .../flow/<flow_id>` `{"host": "<station ip>", "port": 4747}` → the `pair` step, which
   echoes the station name back if it reached it
3. On the station: **Service → Pairing** → *Pair a new source* for the 8 digits. Five-minute
   window, three wrong tries closes it — only ask for the code once the flow is already sitting
   on the `pair` step.
4. `POST .../flow/<flow_id>` `{"code": "…"}` → `create_entry`

Entities land as `binary_sensor.studio_local_connected` / `_alerting`,
`sensor.studio_local_open_alerts`, `event.studio_local_alert`.

## Logging

```yaml
logger:
  logs:
    custom_components.alertroster: debug
```

REQUIREMENTS.md §6 requires that the `lat_` pairing token never reaches a log line. Debug level
is where that regression shows up, so watch for it while testing pairing.

## Releasing

`dev/RELEASE.md` is the runbook for cutting a version: what must be true first, the
bump-then-publish order HACS requires, the `home-assistant/brands` PR and the `hacs/default` PR.
`dev/release-notes-v1.0.0.md` is the draft body for the first one.

## Stations

`./dev/stations.sh` lists what is announcing on the LAN. Two facts it encodes, both easy to get
wrong:

- **The port is not always 4747.** Read it off the mDNS announcement.
- **A `401` means reachable.** `GET /v1/discover` is unauthenticated only on stations running a
  build with protocol §4.1; older ones answer `404`. Every other endpoint answers
  `401 invalid_credentials` without a token, so a connection failure — not a status code — is
  what "unreachable" looks like.

Before a pairing test, on the station: **Service → Pairing**, tick *Accept sources from the LAN*,
Apply, then *Pair a new source*.
