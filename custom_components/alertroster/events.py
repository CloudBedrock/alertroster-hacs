"""Station transitions on the Home Assistant bus (§3.4).

This is the integration's reason to exist. The station decides when an alert
was triggered, answered, cleared, or reached its `ack_timeout_seconds` with
nobody answering; this module is the one place those decisions become something
an automation can trigger on.

Two things here are decisions rather than implementation detail:

* **A `snapshot` frame fires nothing.** The shipped service sends the source's
  open alerts on join (`api.py` records the divergence), and the re-seed after
  every reconnect reads the same set over HTTP. Both describe *state*. Firing
  `alertroster_triggered` for each of them would re-announce alerts that were
  raised hours ago every time the socket blinked -- waking automations, and on
  a `notify` action paging people a second time for something already handled.
  Only a frame carrying a single `alert` is a transition.

* **The alert is copied onto the bus.** `connection.py` keeps the very dict the
  frame arrived in as its open-alert state, and event data reaches arbitrary
  listeners. Without a copy, a template that reached into `trigger.event.data`
  would be editing what the next entity reads back. `open_alerts` copies for
  the same reason.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .api import StationEvent
from .connection import StationConnection
from .const import ATTR_ALERT, ATTR_STATION, BUS_EVENTS

if TYPE_CHECKING:
    from . import AlertRosterConfigEntry

_LOGGER = logging.getLogger(__name__)


@callback
def async_setup_station_events(
    hass: HomeAssistant,
    entry: AlertRosterConfigEntry,
    connection: StationConnection,
) -> CALLBACK_TYPE:
    """Fire a bus event for every transition `connection` hears.

    Returns the callable that stops doing so, for `entry.async_on_unload`.
    """

    @callback
    def fire(event: StationEvent) -> None:
        bus_event = BUS_EVENTS.get(event.event)
        if bus_event is None:
            # Either a `snapshot`, or an event §9 added to a station newer
            # than this integration. Not logged here: `connection.py` already
            # names the unknown ones it sees, and repeating that per frame
            # would be noise on a station that keeps sending one.
            return
        if not event.alert:
            # A transition without its alert is the station contradicting §4.6.
            # Firing `alertroster_unacknowledged` with no alert attached would
            # trip every automation listening for it and give them nothing to
            # act on, which is worse than the frame being dropped.
            #
            # Falsy rather than `is None`, because `"alert": {}` is the same
            # frame with a different spelling: `_async_apply` discards it too
            # (no id), so nothing downstream would make up for firing it.
            _LOGGER.debug(
                "The AlertRoster station %s sent %r with no alert; ignoring it",
                entry.title,
                event.event,
            )
            return

        hass.bus.async_fire(
            bus_event,
            {
                ATTR_ALERT: copy.deepcopy(event.alert),
                # Read now rather than captured at setup, so an entry renamed
                # in the UI is named correctly by the next event rather than
                # from the next restart.
                ATTR_STATION: entry.title,
            },
        )

    return connection.async_add_transition_listener(fire)
