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
3. **A sweep of the serialized payload.** The cases the first two cannot see:
   the token as a *value* under an innocent key (an alert's `detail` is
   whatever an automation wrote), as a dict *key*, or inside a container
   neither layer walks. Layer 2 walks only mappings and lists; a tuple, a set,
   a `MappingProxyType` -- which is exactly what `entry.data` is -- or an
   object rendered by its `repr` would pass both of the first two layers
   untouched.

   So this layer does not walk the payload at all. It encodes it with the
   same encoder Home Assistant is about to use, scrubs the *text*, and parses
   it back. Whatever the encoder can emit, this has already seen.

Layers 2 and 3 are unreachable as the code stands. That is the point: they are
what keeps the rule true after somebody widens layer 1 -- and the widening to
expect is `entry.data` or `entry.as_dict()`, both of which carry the token
under a `MappingProxyType` that a hand-rolled walk would step straight past.

The round trip earns its keep twice over. Home Assistant's serializer renders a
value it cannot encode with `repr()` rather than raising (`helpers/json.py`),
so a live object placed in this payload would not fail the request -- it would
quietly print itself. After the round trip there are no live objects left to
print: what this returns is what the encoder made of them, already scrubbed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.helpers.json import ExtendedJSONEncoder

from .const import CONF_SOURCE_ID, CONF_STATION_NAME, CONF_STATION_VERSION

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import AlertRosterConfigEntry, AlertRosterData

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
    # Annotated rather than left as `getattr`'s `Any`, so `mypy --strict` still
    # checks the payload path that matters. `runtime_data` is a bare annotation
    # that Home Assistant deletes on unload, so this really is `None` for an
    # entry that never loaded.
    data: AlertRosterData | None = getattr(entry, "runtime_data", None)
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
    """Serialize, replace the token in the text, and parse the result back.

    Deliberately not a recursive walk. A walk has to know every container it
    might meet, and the ones it would miss are not exotic -- `entry.data` is a
    `MappingProxyType`, a station could send a tuple, a key can be a string
    too. Encoding first collapses all of that into text, so the substitution
    sees every byte the download would have carried, whatever shape it arrived
    in.

    `ExtendedJSONEncoder` is the encoder Home Assistant serves diagnostics
    with, and it never raises: a value it cannot encode becomes a `repr`
    string, which this then scrubs like any other.

    Substring rather than equality, because a token pasted into a sentence is
    still a leaked token.
    """
    encoded = json.dumps(payload, cls=ExtendedJSONEncoder)
    if token:
        # The token as it appears *inside* JSON, which is the same as the token
        # itself for the `lat_` + hex the station issues, and not the same if a
        # future one carries a character JSON escapes.
        encoded = encoded.replace(json.dumps(token)[1:-1], REDACTED)
    decoded: dict[str, Any] = json.loads(encoded)
    return decoded
