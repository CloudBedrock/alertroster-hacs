"""AlertRoster: raise alerts on a local receiver station, and react when nobody answers.

Entry setup is deliberately thin at this milestone: it builds the one client
the entry owns and hands it to the services. The events socket, the reconnect
loop and the entities are AHA-16 and the M4 issues, which is why `PLATFORMS`
is still empty and nothing here holds state that could go stale.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import AlertRosterClient
from .const import CONF_STATION_NAME
from .services import async_setup_services

PLATFORMS: list[str] = []


@dataclass
class AlertRosterData:
    """What one paired station needs at runtime.

    `station_name` is carried alongside the client because every error this
    integration raises has to name the station -- an automation trace saying
    "the request failed" is not worth reading.
    """

    client: AlertRosterClient
    station_name: str


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
    entry.runtime_data = AlertRosterData(
        client=AlertRosterClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_TOKEN],
        ),
        # The station cannot tell us its name until §5 item 1 ships, so the
        # entry title -- which is the address until then -- is the best label.
        station_name=entry.data.get(CONF_STATION_NAME) or entry.title,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlertRosterConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
