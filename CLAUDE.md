# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (HACS-distributed) that pairs with a local **AlertRoster
receiver station** (`alertroster-receiverd`, part of `alertroster-desktop`) over the LAN, raises
alerts on it from automations, and fires HA events when an alert is acknowledged, resolved, or
**expires with nobody answering**.

`REQUIREMENTS.md` is the working spec: functional requirements, HACS store requirements, and the
M0–M6 milestone table. Read it before adding anything — it records decisions (and non-goals)
that are not visible in the code.

**Current state: M5 complete, starting M6.** `api.py` is the wire client; `config_flow.py` pairs
manually — `user` (host/port + reachability probe) then `pair` (8-digit code → source token) —
and re-pairs through `reauth_confirm` when a token is revoked; `services.py` has `raise` and
`resolve`; `connection.py` holds the events socket open per entry, seeding from
`GET /v1/alerts` on setup and after every reconnect; `events.py` turns each transition into the
bus event §3.4 names, so an automation can trigger on `alertroster_unacknowledged` today;
`config_flow.py`'s `zeroconf` step turns an announcement into a discovery card, keyed on the
station's name rather than its address for the reasons §3.1 now records; every entity platform is
in — `event.<station>_alert` (`event.py`), `binary_sensor.<station>_alerting` and `_connected`
(`binary_sensor.py`), and `sensor.<station>_open_alerts` (`sensor.py`), all on one device per
station and all on the `AlertRosterEntity` base in `entity.py` (device keyed on `entry_id`,
`unavailable` whenever the socket is down — `_connected` is the deliberate exception, being what
reports the outage); `__init__.py`'s `async_remove_entry` revokes the source token on the station
(`DELETE /v1/sources/self`, protocol §6.4) as the entry is deleted, best effort — the entry goes
whatever the station says; `diagnostics.py` dumps the entry, the connection's state and the open
alerts, keeping the token out three ways (an allowlist, `TO_REDACT`, and a value sweep for the
token as a *value*, which key-based redaction cannot see). What is left is M6, which is release
mechanics rather than code: the README's status banner, a v1.0.0 release, the
`home-assistant/brands` PR and the `hacs/default` PR.

Tests run against a fake station that is a **real `aiohttp` server** on a loopback port
(`tests/conftest.py`), not a mocked client — `pytest-socket` blocks everything else, which is
what enforces the no-network rule below. `requirements_dev.txt` is pinned on purpose:
`pytest-homeassistant-custom-component` pins an exact Home Assistant in turn.

## Commands

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_dev.txt

pytest                                   # all tests
pytest tests/test_config_flow.py::test_x  # one test
ruff check . && ruff format .
mypy --strict custom_components/alertroster
mypy tests                               # CI runs this too, and it is not --strict
```

`pyproject.toml` sets `asyncio_mode = "auto"`, so async tests need no marker. CI
(`.github/workflows/validate.yaml`) runs `hacs/action` and `hassfest`; both must pass with no
`ignore`s before default-store submission.

## Running it against a real station

The target is **the home Home Assistant**, `homeassistant.cow-elnath.ts.net` (HA OS with
Supervisor, config at `/config`). That is where this integration is installed, paired and
verified — see `dev/README.md` for getting a build onto it, driving the config flow over the
REST API, and `./dev/stations.sh` for what is announcing on the LAN.

Two things that bite: the station port is **not always 4747** — read it off the mDNS
announcement — and a station running a build older than protocol §4.1 answers `404` to
`GET /v1/discover`, so a `401` from any other endpoint is what "reachable" looks like there, and
such a station shows no software version once paired.

Restarting the home instance interrupts a production Home Assistant. Say so before doing it.

## The wire contract lives in a sibling repo

`../alertroster/docs/LOCAL_ACK_PROTOCOL.md` is authoritative for every station call — §4 is
source ↔ service, §6 is pairing and token scope. Consult it rather than inferring the shape of a
request; if the integration needs something the protocol does not offer, that is a change request
against `alertroster-desktop` (tracked in `REQUIREMENTS.md` §5), not something to invent here.

This repo is on GitHub while the rest of AlertRoster is on CodeCommit, purely because HACS
supports no other host.

## Architecture constraints

These are decided; don't relitigate them in code.

- **The integration is a *source*, and a thin one.** It raises and resolves alerts and renders
  what the station pushes. It holds no timers, no escalation rules, and no opinion about urgency —
  `ack_timeout_seconds` is passed through and the station decides when an alert expired.
- **No acknowledging.** Protocol §4.4 forbids a source acknowledging; acknowledgment is a person
  at a surface. Never add an `acknowledge` action.
- **Push, not poll.** `iot_class` is `local_push`. Deliberately **no `DataUpdateCoordinator`** —
  a small per-entry client holding the `GET /v1/events` WebSocket, with entity listeners. Seed
  state from `GET /v1/alerts` on setup *and* after every reconnect (the socket carries transitions,
  not snapshots).
- **Never show stale state.** All entities go `unavailable` when the socket is down.
- **`401` means the token was revoked**, so it starts a reauth flow — not a reconnect loop.
- **No cloud traffic from HA.** The station owns the cloud link; the alert's `cloud` field is
  passed through as attributes unmodified.
- **A source token only sees its own alerts.** Entities describe what HA raised, not everything
  the station is paging about.
- The pairing token (`lat_…`) must never reach logs, diagnostics, or error messages.

## Repo-specific gotchas

- `strings.json` and `translations/en.json` are separate files with the same content; hassfest
  checks them, so update both.
- Brand icons live at `custom_components/alertroster/brand/icon.png` (256×256) and `icon@2x.png`
  (512×512) — **inside** the integration directory, not the repo root. The same images also go to
  `home-assistant/brands` under `custom_integrations/alertroster/`.
- HACS shows GitHub **releases**, not tags: bumping `manifest.json`'s `version` requires cutting a
  release to be visible.
- Tests must not touch the network — stand up a fake `aiohttp` station instead.
