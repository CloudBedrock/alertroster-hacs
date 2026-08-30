"""AlertRoster: raise alerts on a local receiver station, and react when nobody answers.

Entry setup builds the one client the entry owns, hands it to the services,
puts the station's transitions on the Home Assistant bus, sets up the platforms
that render them, and starts the connection that holds the station's events
socket open. The transitions reach the bus whether or not any entity is
listening: an automation can trigger on `alertroster_unacknowledged` without
the station having a single entity set up.

Setup does not wait for the station to answer -- `connection.py` explains why
that is a decision rather than an oversight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import AlertRosterClient, AlertRosterError, InvalidAuth
from .connection import StationConnection
from .const import DOMAIN
from .events import async_setup_station_events
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]

# There is no YAML for this integration -- a station is paired through the
# config flow, because pairing needs a code off the station's screen. Saying so
# means `alertroster:` in configuration.yaml is refused rather than silently
# ignored.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class AlertRosterData:
    """What one paired station needs at runtime.

    Deliberately not carrying the station's name: §3.6 has every error name the
    station, and a name snapshotted here would keep naming the old one after
    somebody renames the entry in the UI. Callers read `entry.title`, which
    follows the rename.
    """

    client: AlertRosterClient
    connection: StationConnection


type AlertRosterConfigEntry = ConfigEntry[AlertRosterData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the service actions.

    Domain-level rather than per-entry: `alertroster.raise` exists once however
    many stations are paired, and registering it here means the automation
    editor can offer it before the first station is set up.
    """
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AlertRosterConfigEntry) -> bool:
    """Set up AlertRoster from a config entry."""
    client = AlertRosterClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
    )
    connection = StationConnection(hass, entry, client)
    entry.runtime_data = AlertRosterData(client=client, connection=connection)

    # Before the socket is started: a transition that arrived in the gap would
    # otherwise be lost, and §4.6 never replays one. The seed that follows a
    # reconnect can recover the *state* an alert is in, but never the fact that
    # it expired, which is the one thing automations are here for.
    entry.async_on_unload(async_setup_station_events(hass, entry, connection))

    # Platforms first: an entity that is added after the connection has already
    # seeded still reads the current state when it is added, but one added
    # before cannot miss the notification that follows.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    connection.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlertRosterConfigEntry) -> bool:
    """Unload a config entry, closing the events socket before returning.

    Only when the platforms actually unloaded: Home Assistant keeps the entry
    loaded if they did not, and an entry that is still loaded needs its socket.
    """
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.connection.async_stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: AlertRosterConfigEntry) -> None:
    """Revoke this source's token on the station as the entry is deleted (§3.7).

    Best effort on purpose. The entry goes away whatever happens here, because
    an entry that cannot be deleted while the station is off is a worse failure
    than a stale row on a Pairing screen -- and the README says how to clear
    that row by hand.

    The client is built here rather than taken from `runtime_data`: removal
    happens after unload, so the entry's client is already gone, and an entry
    removed while it never loaded at all has none to take.
    """
    token = entry.data.get(CONF_TOKEN)
    if token is None:
        return

    client = AlertRosterClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        token,
    )
    try:
        await client.revoke_self()
    except InvalidAuth:
        # The station revoked it first, which is the state this call wanted.
        # Not a warning: somebody deleting the row on the station and then the
        # entry in Home Assistant did nothing wrong.
        _LOGGER.debug("%s had already revoked this pairing", entry.title)
    except AlertRosterError as err:
        # `err` never carries the token -- see `api.py`'s second rule.
        _LOGGER.warning(
            "Could not unpair from the AlertRoster station %s (%s). "
            "The row can be removed on the station's Pairing screen",
            entry.title,
            err,
        )
