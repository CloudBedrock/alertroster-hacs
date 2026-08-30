"""What every AlertRoster entity shares: its device, and when it is available.

Two decisions live here rather than in each platform:

* **The device is identified by the config entry, not by `source_id`.** The
  station's own identifier is the obvious candidate and the wrong one: §6.2
  mints a new source row for every pairing, so re-pairing through reauth after
  a revoked token gives the same physical station a new `source_id`
  (`config_flow.py` moves the entry's unique id with it). A device keyed on
  that would be abandoned and rebuilt on re-pairing, taking every entity id,
  rename and dashboard reference with it. `entry_id` survives both a re-pair
  and a rename -- the same argument `const.py` records for what bus events
  carry.

* **Availability follows the events socket.** With the socket down nothing is
  telling Home Assistant what the station is doing, so an entity that kept
  reporting its last value would be showing a board that stopped being true
  when the link dropped (CLAUDE.md: never show stale state). The one entity
  that must stay available is the connectivity sensor -- it is what reports
  the outage -- and that is why this is a property to override rather than a
  flag set at construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .connection import StationConnection
from .const import CONF_STATION_VERSION, DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN

if TYPE_CHECKING:
    from . import AlertRosterConfigEntry


class AlertRosterEntity(Entity):
    """One entity of one paired station."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: AlertRosterConfigEntry,
        connection: StationConnection,
        key: str,
    ) -> None:
        """Attach this entity to `entry`'s station, under `key`."""
        self._entry = entry
        self._connection = connection
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            # The entry's title, so the device is called whatever the station
            # called itself at pairing, and whatever the user renames the entry
            # to -- from the next reload. Renaming an entry does not reload it
            # and Home Assistant re-reads `DeviceInfo` only when the platform
            # sets the entity up again, so the device page keeps the old name
            # until then. Forcing a reload on rename would drop the events
            # socket, which is a worse trade than a stale label.
            name=entry.title,
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            # What §4.1's probe last reported, off the entry rather than off
            # the wire: `const.py` says why it is stored there. `None` on a
            # station too old to answer that probe, which leaves the device
            # page with no version rather than a guessed one.
            sw_version=entry.data.get(CONF_STATION_VERSION),
        )

    @property
    def available(self) -> bool:
        """Whether the station is currently telling us anything."""
        return self._connection.connected

    async def async_added_to_hass(self) -> None:
        """Start following the connection, and stop when the entity goes."""
        await super().async_added_to_hass()
        self.async_on_remove(self._connection.async_add_listener(self._async_handle_update))

    @callback
    def _async_handle_update(self) -> None:
        """Re-read the connection after anything it holds changed.

        Listeners are told only *that* something changed (`connection.py`), so
        every subclass renders from the connection at write time and this one
        method serves all of them.
        """
        self.async_write_ha_state()
