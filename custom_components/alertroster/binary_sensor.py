"""Is anybody being paged, and is the station still talking to us (§3.5).

Two sensors that answer different questions, and only one of them is about the
alerts:

* **`alerting`** is the dashboard's "is anybody being paged right now". On
  while this source has an alert the station still considers open --
  `triggered` or `acknowledged`, because somebody answering does not clear the
  condition (`connection.py` keeps that set). Device class `problem`, so Home
  Assistant renders it as a state worth noticing rather than as a plain on/off.

* **`connected`** is about the link, not the station's alerts, and it is the
  one entity in this integration that stays available while the events socket
  is down. Everything else goes `unavailable` there, which is what stops a
  dashboard showing a board that stopped being true (CLAUDE.md) -- but an
  outage sensor that went unavailable during the outage would be reporting
  nothing at the one moment it exists for.

`connected` is deliberately *not* an `EntityCategory.DIAGNOSTIC` entity. That
is the idiomatic category for a connectivity sensor, and it would hide this one
from the dashboard the M4 exit criterion asks for: §3.5 lists it beside the
other two as something a person looks at, because on this integration a station
that has gone quiet is not a diagnostic detail -- it means an automation's page
would not reach anyone.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlertRosterConfigEntry
from .connection import StationConnection
from .entity import AlertRosterEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlertRosterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the station's binary sensors."""
    connection = entry.runtime_data.connection
    async_add_entities(
        [
            AlertRosterAlertingBinarySensor(entry, connection),
            AlertRosterConnectedBinarySensor(entry, connection),
        ]
    )


class AlertRosterAlertingBinarySensor(AlertRosterEntity, BinarySensorEntity):
    """Whether any alert Home Assistant raised is open on the station."""

    _attr_translation_key = "alerting"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry: AlertRosterConfigEntry, connection: StationConnection) -> None:
        """Create the entity for `entry`'s station."""
        super().__init__(entry, connection, "alerting")

    @property
    def is_on(self) -> bool:
        """Whether the station has an open alert from this source."""
        return self._connection.open_alert_count > 0


class AlertRosterConnectedBinarySensor(AlertRosterEntity, BinarySensorEntity):
    """Whether the station's events socket is up."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: AlertRosterConfigEntry, connection: StationConnection) -> None:
        """Create the entity for `entry`'s station."""
        super().__init__(entry, connection, "connected")

    @property
    def available(self) -> bool:
        """Always: this is the entity that reports the socket being down.

        The base class ties availability to the socket, which is right for
        every entity that renders what the station pushed. This one renders
        the socket itself, so tying it to the socket would leave the outage
        reported by an entity that is unavailable for the duration of it.
        """
        return True

    @property
    def is_on(self) -> bool:
        """Whether the events socket is open right now."""
        return self._connection.connected
