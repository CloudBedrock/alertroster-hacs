Ring the panel from Home Assistant — and know when nobody answered.

This integration pairs Home Assistant with a local AlertRoster receiver station over your LAN.
Automations raise alerts on it; Home Assistant hears back when one is acknowledged, resolved, or
**expires with nobody answering**. Nothing in this integration talks to the internet.

**Pairing**
- Stations announcing on mDNS show up as discovered — pair with the 8-digit code from the
  station's Pairing screen. A manual host/port path covers stations without mDNS.
- If the station revokes the token, Home Assistant asks you to pair again rather than retrying
  forever.

**Actions**
- `alertroster.raise` — title, detail, urgency, `dedup_key`, `ack_timeout_seconds`. Returns the
  alert, so a later `resolve` can use its id. A repeat with a live `dedup_key` is a safe no-op.
- `alertroster.resolve` — by `alert_id` or `dedup_key`.
- With several stations paired, both take a `config_entry` naming the one to use.

**Events**
- `alertroster_triggered`, `alertroster_acknowledged`, `alertroster_resolved` and
  `alertroster_unacknowledged` — the last one being the point of the whole thing. Each carries the
  full alert, the station name, and the config entry id.

**Entities**, one device per station: `binary_sensor.<station>_alerting`,
`sensor.<station>_open_alerts`, `binary_sensor.<station>_connected`, `event.<station>_alert`.
Everything but `_connected` goes unavailable when the station cannot be reached — a stale board is
worse than a blank one.

**Removing** the integration asks the station to revoke its token, so the row leaves the station's
Pairing list too. If the station is off at that moment the entry is still removed — nothing traps
you in an integration you cannot delete — and the row stays on the station until you clear it
there.

Diagnostics are available from the device page, with the pairing token redacted.

Requires Home Assistant 2025.1 or newer.
