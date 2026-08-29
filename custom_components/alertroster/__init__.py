"""AlertRoster: raise alerts on a local receiver station, and react when nobody answers.

Entry setup builds the one client the entry owns, hands it to the services, and
starts the connection that holds the station's events socket open. The entities
that will listen to that connection are the M4 issues, which is why `PLATFORMS`
is still empty.

Setup does not wait for the station to answer -- `connection.py` explains why
that is a decision rather than an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import AlertRosterClient
from .connection import StationConnection
from .const import DOMAIN
from .services import async_setup_services

PLATFORMS: list[str] = []

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
