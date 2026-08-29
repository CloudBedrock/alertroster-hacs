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
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import AlertRosterClient
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
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlertRosterConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
