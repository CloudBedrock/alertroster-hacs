"""How many alerts this source has open, and which ones (§3.5).

The count is the state so it charts and so a template can compare it; the
alerts themselves ride along as an attribute, which is what a dashboard card
renders and what an automation reads to name the alert somebody is being paged
about.

Two things worth saying about the attribute:

* **It is the whole alert, not a summary.** §2 passes the `cloud` field through
  untouched and the station is free to add fields under §9; picking out three
  of them here would mean an automation could not reach the rest without a
  change to this integration.

* **`connection.open_alerts` already hands out deep copies**, so nothing here
  copies again. That property exists in that shape precisely because this
  attribute is read by templates: the alternative is a template reaching into
  the nested `cloud` object and editing the state the next listener reads.

The count is `MEASUREMENT`: it goes up and down with the station's board and
means nothing cumulative. Long-term statistics on it answer "how often was
anybody being paged", which is a question worth being able to ask.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlertRosterConfigEntry
from .connection import StationConnection
from .const import ATTR_ALERTS
from .entity import AlertRosterEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlertRosterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the station's open-alert sensor."""
    async_add_entities([AlertRosterOpenAlertsSensor(entry, entry.runtime_data.connection)])


class AlertRosterOpenAlertsSensor(AlertRosterEntity, SensorEntity):
    """The alerts this source has open on the station."""

    _attr_translation_key = "open_alerts"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: AlertRosterConfigEntry, connection: StationConnection) -> None:
        """Create the entity for `entry`'s station."""
        super().__init__(entry, connection, "open_alerts")

    @property
    def native_value(self) -> int:
        """How many alerts are open, counted without copying them."""
        return self._connection.open_alert_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The open alerts themselves, as the station last described them."""
        return {ATTR_ALERTS: self._connection.open_alerts}
