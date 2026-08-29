# AlertRoster for Home Assistant — Requirements

A Home Assistant integration that pairs with a local **AlertRoster receiver station**
(`alertroster-receiverd`, shipped with the AlertRoster desktop app), raises alerts on it from
automations, and reacts when nobody answers. Distributed through HACS.

The wire contract is `alertroster/docs/LOCAL_ACK_PROTOCOL.md` (§4 source ↔ service, §6 pairing).
This integration is a *source* in that document's terms. It is also a thin client: it renders what
the service sends and sends requests back. It holds no timers, no escalation rules and no opinion
about urgency — those live in the station and, when a key is configured, in the AlertRoster core.

## 1. Goals

- **Discoverable.** A station that has turned on *Accept sources from the LAN* appears in
  Settings → Devices & services as a discovered integration. Nobody types an IP address.
- **Pairable in one step.** The only thing the user enters is the 8-digit code shown on the
  station's Pairing screen.
- **Usable without reading an API.** Raising and resolving an alert are typed service actions
  in the automation editor; the station's state is entities; "nobody answered" is an event on the
  bus.
- **Honest about its link.** When the station cannot be reached the integration says so on a
  dashboard-visible entity; it never shows a stale state as current.
- **Installable from HACS**, and eligible for the HACS default store.

## 2. Non-goals

- No cloud traffic. The integration talks to the station on the LAN only; the station owns the
  cloud link (§7). If the station has an AlertRoster key the alert's `cloud` field is passed
  through as attributes, unmodified.
- No acknowledging. A source may not acknowledge (§4.4) — acknowledging is a person answering
  at a surface. The integration will not offer an `acknowledge` action.
- No local model of escalation, retry or expiry. `ack_timeout_seconds` is passed through to
  the station and the station decides when an alert has expired.

## 3. Functional requirements

### 3.1 Discovery (zeroconf)

- `manifest.json` declares `"zeroconf": ["_alertroster-receiver._tcp.local."]`.
- TXT records `v=1` and `name=<station name>` are read; the name becomes the flow title
  ("Pair with jims-mac").
- Discovery is keyed on the station's host+port; a second announcement of the same station does
  not create a second discovery card. Once paired, the entry's `unique_id` is the `source_id`
  returned by the pair step, and rediscovery of a paired station is ignored.
- A manual path (host, port; default port 4747) exists for stations without mDNS.

### 3.2 Pairing (config flow)

1. Discovery or manual host → the flow probes the station. If the station cannot be reached the
   error names the likely cause: *Accept sources from the LAN* is off.
2. The **pair** step shows one field, *Pairing code*, with instructions naming the station's menu
   path (Service → Pairing → Pair a new source).
3. The flow POSTs `/v1/pair` with `{"code", "name": "Home Assistant", "kind": "homeassistant"}`.
   - `201` → store `token` (`lat_…`), `source_id`, host, port, station name; create the entry.
   - `403` → `invalid_code`; the user may retry (the station closes the window after three
     wrong codes — the error text says so on the third).
   - Window not open → `window_closed`, with the instruction to open Pairing on the station.
4. The token is shown nowhere in the UI after the entry is created, and is not logged.

### 3.3 Service actions

Registered under the `alertroster` domain with full `services.yaml` field definitions (typed
selectors so the automation editor renders a proper form):

| Action | Fields | Station call |
|---|---|---|
| `alertroster.raise` | `title` (required), `detail`, `urgency` (`high`/`low`, default `high`), `dedup_key`, `ack_timeout_seconds` (default: station default) | `POST /v1/alerts` |
| `alertroster.resolve` | `alert_id` **or** `dedup_key` | `POST /v1/alerts/:id/resolve` |

- `raise` returns the alert object (`response_variable`), so a later `resolve` can use the id.
- A repeat `raise` with the same `dedup_key` is a success and returns the existing alert (§4.2),
  so retrying automations are safe.
- With more than one paired station, actions take an optional `config_entry` / device target;
  with exactly one, it is implied.

### 3.4 Events on the Home Assistant bus

Every transition received on the station's `GET /v1/events` WebSocket is fired as an HA event,
carrying the complete alert object as `alert` plus `station` (entry name):

| Station event | HA event |
|---|---|
| `alert.triggered` | `alertroster_triggered` |
| `alert.acknowledged` | `alertroster_acknowledged` |
| `alert.resolved` | `alertroster_resolved` |
| `alert.expired` | `alertroster_unacknowledged` |

`alertroster_unacknowledged` is the reason the integration exists: "the panel rang for two
minutes and nobody answered — now do X". It is documented first in the README, with a worked
automation.

Events are also exposed as an `event` entity per station (HA 2023.8+ event platform) so they are
usable from the UI trigger picker without typing an event name.

### 3.5 Entities

Per paired station, one device ("AlertRoster station <name>") with:

| Entity | Type | Meaning |
|---|---|---|
| `binary_sensor.<station>_alerting` | `binary_sensor` (device class `problem`) | any alert this source raised is open (`triggered` or `acknowledged`) |
| `sensor.<station>_open_alerts` | `sensor` | count of open alerts; attribute `alerts` lists them |
| `binary_sensor.<station>_connected` | `binary_sensor` (device class `connectivity`) | the events socket is up |
| `event.<station>_alert` | `event` | last transition (see 3.4) |

All entities become `unavailable` when the socket is down, never frozen at the last value.

Scope note: a source token sees only the alerts *it* raised (§6.2). The entities describe HA's
alerts on the station, not everything the station is paging about. The README says this.

### 3.6 Connection handling

- On entry setup: `GET /v1/alerts` to seed state, then hold `GET /v1/events` open (token as
  `Authorization: Bearer`, falling back to `?token=` only if the client cannot set headers).
- On socket loss: reconnect with jittered exponential backoff (1 s → 60 s cap); on reconnect
  re-seed from `GET /v1/alerts` because the socket carries transitions, not a snapshot.
- Connected/disconnected transitions update `binary_sensor.<station>_connected` at once.
- `401` on any request means the token was revoked on the station: the entry raises a
  reauth flow ("This station no longer recognises Home Assistant — pair again"), not a reconnect
  loop.
- A `raise` while the socket is down is still attempted over HTTP; a failed request raises a
  `HomeAssistantError` with a message that names the station, so the automation trace shows it.

### 3.7 Removal

Deleting the config entry calls the station to revoke its own token (requires the service
change in §5), then removes the entry. If the station is unreachable the entry is removed anyway
and the README explains that the row can be revoked from the station's Pairing screen.

## 4. HACS and Home Assistant requirements (verified against hacs.xyz, 2026-08-28)

Hosting and repo:

- Public repository on **GitHub** (HACS does not support any other host — hence this project is
  not on CodeCommit like the rest of AlertRoster).
- GitHub **description** set, **topics** set (`home-assistant`, `hacs`, `hacs-integration`,
  `alertroster`), **issues enabled**.
- `README.md` with usage instructions.
- `hacs.json` at the root with at least `name`.
- Exactly one integration under `custom_components/<domain>/`.
- `manifest.json` with `domain`, `name`, `version`, `documentation`, `issue_tracker`,
  `codeowners`, `integration_type`, `iot_class`, `config_flow`, `zeroconf`.
- `brand/icon.png` (256×256) and `brand/icon@2x.png` (512×512) in the repo. (HA ≥ 2026.3 reads
  these from the integration itself; for the default store and older HA, the same images are
  also submitted to `home-assistant/brands` under `custom_integrations/alertroster/`.)
- A GitHub **release** (not just a tag) per version; HACS shows the last five.

CI, both must pass with no `ignore`s before default-store submission:

- `hacs/action@main` with `category: integration`
- `home-assistant/actions/hassfest@master`

Both are in `.github/workflows/validate.yaml`.

Default-store inclusion (after the above are green and a release exists):

1. Confirm the repo installs as a HACS **custom repository** (HACS → ⋮ → Custom repositories →
   this URL, category Integration).
2. Open a PR against `hacs/default` adding `CloudBedrock/alertroster-hacs` to the `integration`
   file, alphabetically. Only the owner or a major contributor may open it.
3. Automated checks on that PR: brands, manifest, HACS validation, active repo, ≥1 release,
   contributor, description/issues/topics, JSON sorting.
4. After merge the repo appears in the next scheduled scan.

## 5. Changes needed on the station (`alertroster-desktop`)

Tracked there as issues; none block installing as a custom repository, all make the flow nicer:

1. **Unauthenticated `GET /v1/discover`** on the LAN listener returning
   `{name, version, pairing_window_open}` so the config flow can tell the user the window is
   not open *before* asking for a code. (Today `/v1/status` is surface-only.)
2. **A token may revoke itself** (`DELETE /v1/sources/self` or `DELETE /v1/sources/:own_id`
   with a source token) so removing the integration in HA removes the row on the station.
3. **Pairing dialog hint**: "Enter this code in Home Assistant → Settings → Devices & services".
4. **Document reconnect** in `LOCAL_ACK_PROTOCOL.md` §4.6: the events socket carries transitions
   only; a client re-reads `GET /v1/alerts` after reconnecting.

## 6. Quality

- Type-checked (`mypy --strict` on the component), `ruff` formatted, following the Home Assistant
  developer style (`async_*` naming, `DataUpdateCoordinator` not used — this is push, so a
  small per-entry client with listeners).
- Tests with `pytest-homeassistant-custom-component`: config flow (discovery, manual, wrong code,
  closed window, already configured, reauth on 401), services (raise/resolve, response data,
  idempotent repeat), events (each station event → HA event), entity availability on socket loss.
- No network in tests: the station is a fake `aiohttp` server.
- Every user-visible string in `strings.json` / `translations/en.json`.
- The `lat_` token never appears in logs, diagnostics or error messages (diagnostics redacts it).

## 7. Milestones

| # | Deliverable | Done when |
|---|---|---|
| M0 | Skeleton, CI green, installs as a custom repository | HACS and hassfest actions pass; integration shows in HACS |
| M1 | Config flow: zeroconf + manual + pairing code | Pairs with a real station; token stored; reauth on 401 |
| M2 | `raise` / `resolve` services | Automation raises an alert on the Mac station, resolve clears it |
| M3 | Events socket → HA events + `event` entity | `alertroster_unacknowledged` fires when the station expires an alert |
| M4 | Entities and device | Dashboard shows alerting / open count / connected; unavailable on socket loss |
| M5 | Station-side changes (§5), removal revokes | Remove in HA → row gone on station |
| M6 | v1.0.0 release, brands PR, `hacs/default` PR | Listed in the HACS store |

## 8. Open questions

- Should `raise` accept a `wake_room: true` hint? Today which outputs fire is the station's
  configuration; passing a hint would be the source expressing a preference the station is free
  to ignore. Leaning no until a real automation needs it.
- Multiple HA instances pairing with one station each get their own source token and see only
  their own alerts — fine, but the station's Pairing list shows them all as "Home Assistant".
  The pair request could send the HA instance name (`hass.config.location_name`) as `name`.
