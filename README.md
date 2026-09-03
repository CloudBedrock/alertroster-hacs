# AlertRoster for Home Assistant

[![Validate](https://github.com/CloudBedrock/alertroster-hacs/actions/workflows/validate.yaml/badge.svg)](https://github.com/CloudBedrock/alertroster-hacs/actions/workflows/validate.yaml)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

Ring the panel from Home Assistant — and know when nobody answered.

[AlertRoster](https://alertroster.com) runs a **receiver station** on a Mac, Windows or Linux
machine (or a Raspberry Pi on the wall): it takes an alert, sounds it, drives relays and
wall displays, and waits for a person to acknowledge it. This integration pairs Home Assistant
with that station over your LAN so an automation can raise an alert, and so Home Assistant can
react when the alert is acknowledged, resolved — or **expires with nobody answering**.

Nothing here touches the internet. If the station has an AlertRoster cloud key, *it* escalates
off-site; Home Assistant just sees the result.

> **Status:** pre-release. Install as a HACS custom repository (below) — the default-store
> listing lands with v1.0.0.

## You'll need a receiver station

This integration raises alerts; the **receiver station** is what answers them. Without one on
your network there is nothing to sound the alarm, drive the relays, or acknowledge anything —
so grab it first. It's free, and it runs on a Mac, a Windows box, a Linux desktop or a
Raspberry Pi on the wall.

**[Download the AlertRoster receiver station →](https://github.com/CloudBedrock/alertroster-desktop-releases/releases/latest)**

Once it's installed, turn on **Accept sources from the LAN** under *Service → Pairing* so Home
Assistant can reach it.

## Requirements

- Home Assistant **2025.3** or newer.
- An [AlertRoster receiver station](https://github.com/CloudBedrock/alertroster-desktop-releases/releases/latest)
  on the same network, with **Accept sources from the LAN** turned on (Service → Pairing on
  the station).

## Install

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/CloudBedrock/alertroster-hacs`,
   category *Integration*. (Not needed once the integration is in the HACS default store.)
2. Search HACS for **AlertRoster**, install, restart Home Assistant.

### Manual

Copy `custom_components/alertroster` into `<config>/custom_components/` and restart.

## Pair with a station

1. On the station: **Service → Pairing**, tick **Accept sources from the LAN**, Apply.
2. Home Assistant → Settings → Devices & services: the station appears as discovered. Click
   **Configure**. (No discovery? *Add integration → AlertRoster* and enter the station's
   address. The port is 4747 unless the station was moved off it — the value to use is the one
   on the station's own Pairing screen.)
3. On the station click **Pair a new source**; it shows an 8-digit code for five minutes.
4. Type the code into Home Assistant. Done.

Home Assistant now holds a source token for that station. The station lists it under Pairing and
can revoke it at any time — if it does, Home Assistant asks you to pair again rather than
retrying forever.

## Use it

### Raise an alert

```yaml
action: alertroster.raise
data:
  title: Garage door open after midnight
  detail: Left open since 23:40
  urgency: high
  dedup_key: garage-door-night
  ack_timeout_seconds: 120
```

Only `title` is required. Leave `ack_timeout_seconds` out and the station's own default applies —
this integration holds no timers and has no opinion about when an alert has gone unanswered.

Repeating the same `dedup_key` while the alert is open is harmless — you get the existing alert
back — so retrying automations are safe. It is not an *update*, though: to change a live alert,
resolve it and raise a new one.

### Resolve it when the condition clears

```yaml
action: alertroster.resolve
data:
  dedup_key: garage-door-night
```

Or by id, which `raise` hands back if you ask for it:

```yaml
- action: alertroster.raise
  data:
    title: Pump room flooding
  response_variable: alert
- delay: "00:05:00"
- action: alertroster.resolve
  data:
    alert_id: "{{ alert.id }}"
```

### React when nobody answered

```yaml
trigger:
  - platform: event
    event_type: alertroster_unacknowledged
action:
  - action: notify.mobile_app_jims_phone
    data:
      title: "Nobody answered: {{ trigger.event.data.alert.title }}"
      message: The panel rang for {{ trigger.event.data.alert.ack_timeout_seconds }}s.
```

Also fired: `alertroster_triggered`, `alertroster_acknowledged`, `alertroster_resolved`. Each
carries the whole alert as `alert`, the station's name as `station`, and the config entry it came
from as `entry_id`.

There is no `acknowledge` action, and there will not be one: acknowledging is a person answering
at a surface, not a program deciding the page has been dealt with. Home Assistant raises and
resolves; people acknowledge.

### With more than one station

Both actions take an optional `config_entry` naming the station — the automation editor renders
it as a picker:

```yaml
action: alertroster.raise
data:
  config_entry: 01J9Z4Q0X8V2N7B3K5R6T1W8Y4
  title: Freezer over temperature
```

With exactly one station paired you can leave it out. With several, leaving it out is an error
that names them — which station should page someone is not a guess this integration gets to make.

Events tell you the same thing in reverse: match on `entry_id`, which is stable, where the name is
yours to change.

```yaml
trigger:
  - platform: event
    event_type: alertroster_unacknowledged
    event_data:
      entry_id: 01J9Z4Q0X8V2N7B3K5R6T1W8Y4
action:
  - action: notify.mobile_app_jims_phone
    data:
      message: "{{ trigger.event.data.station }}: {{ trigger.event.data.alert.title }}"
```

To find a station's `entry_id`, open **Developer tools → Events**, listen to
`alertroster_triggered`, and raise a test alert from that station. Use `station` for anything a
person reads — renaming the entry renames it in the next event.

### Entities

| Entity | Meaning |
|---|---|
| `binary_sensor.<station>_alerting` | an alert Home Assistant raised is open |
| `sensor.<station>_open_alerts` | how many; the list is in the `alerts` attribute |
| `binary_sensor.<station>_connected` | the live link to the station is up |
| `event.<station>_alert` | last transition, for the UI trigger picker |

All of them go **unavailable** when the station cannot be reached — except
`binary_sensor.<station>_connected`, which turns **off**: it is the one reporting the outage, so it
has to still be there during it. A stale board is worse than a blank one.

A source only sees its own alerts, so these describe what Home Assistant raised — not everything
the station is paging about.

## When the station escalates off-site

The cloud key lives on the **station**, never in Home Assistant — this integration makes no
requests that leave your network. When the station has one, it opens an incident off-site
alongside the local alert, and passes the result back on the alert as `alert.cloud`, exactly as
the station sent it. `alert.cloud.link` is `ok`, `pending` or `failed`.

**`link` is not settled when the alert is raised.** The station rings the panel first and opens
the off-site incident afterwards, without waiting for it, so an `alertroster_triggered` event
carries `cloud` as `null` or `link: pending` — nearly always. There is also no *update* event: a
station sends only the four transitions above, each with the whole alert, so a `pending` that
later becomes `failed` never arrives on its own. Branch on `link` in an event that comes later,
not in `alertroster_triggered`.

The one worth automating is the case the product exists for — nobody answered at the panel, and
the off-site page did not go out either:

```yaml
trigger:
  - platform: event
    event_type: alertroster_unacknowledged
condition: >
  {{ trigger.event.data.alert.cloud is not none
     and trigger.event.data.alert.cloud.link == 'failed' }}
action:
  - action: notify.persistent_notification
    data:
      message: >
        Nobody answered "{{ trigger.event.data.alert.title }}" at the panel, and the off-site
        page did not go out either.
```

By then the station has long since finished trying, so `link` says what actually happened. The
same goes for `alertroster_acknowledged` and `alertroster_resolved`.

While an alert is still open, Home Assistant's copy of it — including the copy in the `alerts`
attribute on `sensor.<station>_open_alerts` — is whatever the last event carried. The set is
re-read from the station when the integration starts and after every reconnect, and not otherwise,
because nothing here polls. So a `link` that settles while the alert stays open is not something
to build an automation on; wait for the transition.

`alert.cloud` is `null` for a purely local alert. When somebody acknowledges from their phone
instead of at the panel, it arrives here as an ordinary `alertroster_acknowledged` — the alert's
`acknowledged_by.surface` is `cloud` rather than the name of a machine in the building.

## Unpair

Deleting the integration in Home Assistant (Settings → Devices & services → ⋮ → **Delete**) tells
the station to revoke the token, so the row leaves its Pairing list too. If the station was off at
that moment the entry is still deleted — nothing traps you in an integration you cannot remove —
and the row is left behind. Clear it on the station: **Service → Pairing**, find *Home Assistant*,
revoke.

## Troubleshooting

Download diagnostics from the device page (⋮ → **Download diagnostics**) before opening an issue:
it carries the connection state and the open alerts, with the pairing token redacted. Turn on
debug logging with:

```yaml
logger:
  logs:
    custom_components.alertroster: debug
```

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_dev.txt
pytest
```

CI runs the [HACS action](https://hacs.xyz/docs/publish/action/) and
[hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest/) on every push.

## License

MIT © Cloud Bedrock LLC
