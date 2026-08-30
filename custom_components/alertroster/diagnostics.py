"""Config entry diagnostics (REQUIREMENTS.md §6).

What a user downloads and attaches to a bug report: the entry's address and
identity, what the events connection is doing, and the alerts Home Assistant
currently has open on the station.

This is the one file in the integration written to be *shared*. Diagnostics
land in public issue trackers, and `CLAUDE.md` states the rule without
qualification -- the `lat_` pairing token must never reach logs, diagnostics or
error messages. So the token is kept out three times over, and the redundancy
is deliberate:

1. **An allowlist.** This names the fields it wants instead of dumping
   `entry.as_dict()` and redacting a denylist, which is the more common core
   idiom. The two differ only in the future: a key added to `entry.data` later
   is shared by default under a denylist and withheld by default under an
   allowlist. For the file whose failure mode is leaking a credential, the
   default has to be "not shared".
2. **`async_redact_data`.** Catches a `token` key anywhere in the payload,
   however deeply nested -- including inside an alert the station sent.
3. **A value sweep.** The one case the first two cannot see: the token turning
   up *as a value* under an innocent key. Nothing produces that today. It is
   here because "never" is the requirement, and because the thing that would
   produce it -- an alert whose `detail` somebody pasted a token into, a field
   a newer station echoes back -- is not something this integration controls.

Layers 2 and 3 are unreachable as the code stands. That is the point: they are
what keeps the rule true after somebody widens layer 1.

One trap worth naming. Home Assistant's diagnostics serializer renders a value
it cannot encode with `repr()` rather than raising (`helpers/json.py`), so a
live object placed in this payload would not fail the request -- it would
quietly print itself. Everything returned here is a scalar, a string, or a
plain dict built from them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN

from .const import CONF_SOURCE_ID, CONF_STATION_NAME, CONF_STATION_VERSION

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import AlertRosterConfigEntry

# `token` is never put into the payload by the code below, so this only ever
# fires on something a future edit added -- which is exactly when it matters.
TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AlertRosterConfigEntry
) -> dict[str, Any]:
    """Everything worth knowing about one paired station, minus the credential.

    `entry.state` is included because the most common report -- "the entities
    are all unavailable" -- has two very different causes: an entry that failed
    to load at all, and one that loaded fine and cannot reach the station. The
    connection section tells those apart at a glance.
    """
    payload: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "entry_id": entry.entry_id,
            "state": entry.state.value,
            # The LAN address is not redacted: "which address is it failing to
            # reach" is the first question support asks, and a station is not
            # reachable from outside the user's own network. `unique_id` is
            # left out as a duplicate of `source_id`.
            "host": entry.data.get(CONF_HOST),
            "port": entry.data.get(CONF_PORT),
            "source_id": entry.data.get(CONF_SOURCE_ID),
            "station_name": entry.data.get(CONF_STATION_NAME),
            "station_version": entry.data.get(CONF_STATION_VERSION),
        }
    }

    # An entry that failed to set up has no `runtime_data` at all, and
    # diagnostics for a broken entry is precisely when somebody is asking for
    # help -- so this answers rather than raising.
    data = getattr(entry, "runtime_data", None)
    if data is None:
        payload["connection"] = None
        payload["open_alerts"] = []
        payload["note"] = "this entry is not loaded, so it has no live connection"
    else:
        payload["connection"] = data.connection.diagnostics()
        # §6.2 scopes the token to this source, so these are the alerts Home
        # Assistant raised -- never everything the station is paging about.
        payload["open_alerts"] = data.connection.open_alerts

    return _scrubbed(async_redact_data(payload, TO_REDACT), entry.data.get(CONF_TOKEN))


def _scrubbed(payload: dict[str, Any], token: str | None) -> dict[str, Any]:
    """Replace the live token wherever it appears as a value.

    Key-based redaction cannot see a credential that arrives under an innocent
    key, and the strings in here are not all ours: an alert's `title` and
    `detail` are whatever an automation wrote, and the rest is whatever the
    station sent. Substring rather than equality, because a token pasted into
    a sentence is still a leaked token.
    """
    if not token:
        return payload
    # Rebuilt as a dict comprehension rather than handed to `_scrub` whole, so
    # the return type stays `dict[str, Any]` instead of widening to `Any`.
    return {key: _scrub(value, token) for key, value in payload.items()}


def _scrub(value: Any, token: str) -> Any:
    """Walk anything JSON-shaped, replacing `token` inside every string."""
    if isinstance(value, str):
        return value.replace(token, REDACTED)
    if isinstance(value, dict):
        return {key: _scrub(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, token) for item in value]
    return value
