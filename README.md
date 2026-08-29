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

> **Status:** requirements and skeleton. See [REQUIREMENTS.md](REQUIREMENTS.md) for what is being
> built and in what order.

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
   address; the port is 4747.)
3. On the station click **Pair a new source**; it shows an 8-digit code for five minutes.
4. Type the code into Home Assistant. Done.

Home Assistant now holds a source token for that station. The station lists it under Pairing and
can revoke it at any time.

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

Repeating the same `dedup_key` while the alert is open is harmless — you get the existing alert
back — so retrying automations are safe.

### Resolve it when the condition clears

```yaml
action: alertroster.resolve
data:
  dedup_key: garage-door-night
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
carries the whole alert as `alert`.

### Entities

| Entity | Meaning |
|---|---|
| `binary_sensor.<station>_alerting` | an alert Home Assistant raised is open |
| `sensor.<station>_open_alerts` | how many; the list is in the `alerts` attribute |
| `binary_sensor.<station>_connected` | the live link to the station is up |
| `event.<station>_alert` | last transition, for the UI trigger picker |

All of them go **unavailable** when the station cannot be reached. A stale board is worse than a
blank one.

A source only sees its own alerts, so these describe what Home Assistant raised — not everything
the station is paging about.

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
