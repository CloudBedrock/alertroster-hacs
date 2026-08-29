"""The station's transitions as an `event` entity (§3.4, §3.5).

The same four transitions `events.py` puts on the bus, exposed a second time as
an entity. That is not redundancy: an automation triggering on
`alertroster_unacknowledged` has to be told the event name, while this entity
is in the UI trigger picker, carries the last transition on a dashboard, and
gives the station's activity a place in the logbook.

Two decisions here:

* **The event types are the bus names without the domain.** `const.py` derives
  them rather than listing them again, so `alert.expired` is `unacknowledged`
  here for the same reason it is `alertroster_unacknowledged` there -- what an
  automation reacts to is that nobody answered, not the station's bookkeeping
  word for a timer running out.

* **A frame with no alert fires nothing.** `events.py` gives the long form of
  this: a transition whose alert is missing is the station contradicting §4.6,
  and firing it anyway would wake every automation watching the entity with
  nothing to act on. The snapshot frame is dropped by the same test -- it is
  state, not a transition, and re-firing it on every reconnect would announce
  hours-old alerts each time the socket blinked.
"""

from __future__ import annotations

import copy

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlertRosterConfigEntry
from .api import StationEvent
from .connection import StationConnection
from .const import ATTR_ALERT, ENTITY_EVENT_TYPES
from .entity import AlertRosterEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlertRosterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the station's alert event entity."""
    async_add_entities([AlertRosterAlertEvent(entry, entry.runtime_data.connection)])


class AlertRosterAlertEvent(AlertRosterEntity, EventEntity):
    """The last transition the station reported for this source's alerts."""

    _attr_translation_key = "alert"

    def __init__(self, entry: AlertRosterConfigEntry, connection: StationConnection) -> None:
        """Create the entity for `entry`'s station."""
        super().__init__(entry, connection, "alert")
        # A list off the mapping, so the picker offers them in the order §3.4's
        # table lists them rather than in whatever order a set iterates -- and
        # a fresh one per entity, because `capability_attributes` hands this
        # object straight to the state machine. A class attribute would be the
        # single list every station's entity exposes, and a template calling
        # `state_attr(..., 'event_types').append(...)` would edit all of them:
        # the same aliasing the `alert` attribute is deep-copied to prevent.
        self._attr_event_types = list(ENTITY_EVENT_TYPES.values())

    async def async_added_to_hass(self) -> None:
        """Also follow the transitions, not only the state they leave behind.

        `AlertRosterEntity` subscribes to the connection's state changes, which
        is what carries availability. A transition cannot be recovered from
        those: by the time an `alert.expired` has been folded in, the alert is
        simply gone from the open set, indistinguishable from one somebody
        resolved.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            self._connection.async_add_transition_listener(self._async_handle_transition)
        )

    @callback
    def _async_handle_transition(self, event: StationEvent) -> None:
        """Record one transition, if it is one this entity can render."""
        event_type = ENTITY_EVENT_TYPES.get(event.event)
        if event_type is None or not event.alert:
            return
        # Copied for the reason `events.py` copies: `connection.py` keeps the
        # very dict the frame arrived in as its open-alert state, and these
        # attributes are read by templates that must not be able to edit it.
        self._trigger_event(event_type, {ATTR_ALERT: copy.deepcopy(event.alert)})
        self.async_write_ha_state()
