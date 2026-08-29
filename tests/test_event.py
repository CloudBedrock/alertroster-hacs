"""The `event` entity per station (AHA-18).

Driven end to end like `test_events.py`: frames leave the fake station over a
real socket, through `api.py` and `connection.py`, and what is asserted is the
entity state Home Assistant ends up with. Nothing between the station and the
state machine is mocked, so a failure here is a dashboard or a UI trigger that
would not have worked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.const import (
    ATTR_ALERT,
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
)

from .conftest import FakeStation

_TIMEOUT = 10.0
_ENTITY_ID = "event.studio_alert"


async def _until(check: Callable[[], bool], what: str, timeout: float = _TIMEOUT) -> None:
    """Wait for `check` to hold, or fail saying what never happened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _setup(
    hass: HomeAssistant, station: FakeStation, title: str = "studio"
) -> MockConfigEntry:
    """A paired station, set up the way the config flow leaves it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=station.source_id,
        data={
            CONF_HOST: station.host,
            CONF_PORT: station.port,
            CONF_TOKEN: station.issue_token(),
            CONF_SOURCE_ID: station.source_id,
            CONF_STATION_NAME: None,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    connection = entry.runtime_data.connection
    await _until(lambda: connection.connected, "the events socket to open")
    await hass.async_block_till_done()
    return entry


def _alert(alert_id: str = "alt_1", status: str = "triggered") -> dict[str, Any]:
    """A complete alert, shaped as §4.3 describes one."""
    return {
        "id": alert_id,
        "status": status,
        "title": "Front door",
        "detail": "Nobody at the desk",
        "urgency": "high",
        "dedup_key": None,
        "ack_timeout_seconds": 120,
        # Nested on purpose: §2 passes this through untouched, so an attribute
        # that shared it with the connection's state would be editable through
        # a template.
        "cloud": {"synced": True, "id": "cl_9", "tags": ["ops", "night"]},
    }


async def _push(hass: HomeAssistant, station: FakeStation, transition: str, **kwargs: Any) -> State:
    """Send one transition and return the entity state it produced."""
    await station.push(transition, _alert(**kwargs))
    await _until(
        lambda: (
            (state := hass.states.get(_ENTITY_ID)) is not None
            and state.attributes.get("event_type") is not None
        ),
        f"{transition} to reach {_ENTITY_ID}",
    )
    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    return state


# -- the entity itself ----------------------------------------------------


async def test_entity_is_created_on_the_station_device(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.5: one `event.<station>_alert`, on the paired station's device."""
    entry = await _setup(hass, station)

    entity = er.async_get(hass).async_get(_ENTITY_ID)
    assert entity is not None
    # The config entry id, not `source_id`: §6.2 remints the source row on
    # every pairing, so a unique id built from it would be replaced -- and the
    # entity rebuilt -- the first time a revoked token was re-paired.
    assert entity.unique_id == f"{entry.entry_id}_alert"

    device = dr.async_get(hass).async_get(entity.device_id or "")
    assert device is not None
    assert device.identifiers == {(DOMAIN, entry.entry_id)}
    assert device.name == "studio"
    assert device.manufacturer == DEVICE_MANUFACTURER
    assert device.model == DEVICE_MODEL
    assert device.config_entries == {entry.entry_id}


async def test_event_types_are_the_four_transitions(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The UI trigger picker offers §3.4's table, in its order."""
    await _setup(hass, station)

    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    assert state.attributes["event_types"] == [
        "triggered",
        "acknowledged",
        "resolved",
        "unacknowledged",
    ]
    # Nothing has happened yet, so there is no last event -- but the entity is
    # there, which is what makes it pickable before the first alert.
    assert state.attributes["event_type"] is None


async def test_two_stations_get_an_entity_each(
    hass: HomeAssistant, station: FakeStation, second_station: FakeStation
) -> None:
    """Two paired stations are two devices, and one entity does not shadow the other."""
    await _setup(hass, station)
    await _setup(hass, second_station, title="kitchen")

    await _push(hass, station, "alert.triggered")
    kitchen = hass.states.get("event.kitchen_alert")
    assert kitchen is not None
    # The transition went to the station that sent it, and only to it.
    assert kitchen.attributes["event_type"] is None


# -- what a transition does to it -----------------------------------------


@pytest.mark.parametrize(
    ("transition", "expected"),
    [
        ("alert.triggered", "triggered"),
        ("alert.acknowledged", "acknowledged"),
        ("alert.resolved", "resolved"),
        # The one rename: what an automation reacts to is that nobody
        # answered, not the station's word for a timer running out.
        ("alert.expired", "unacknowledged"),
    ],
)
async def test_each_transition_becomes_its_event_type(
    hass: HomeAssistant, station: FakeStation, transition: str, expected: str
) -> None:
    """§3.4's table, one row at a time, over a real socket."""
    await _setup(hass, station)

    state = await _push(hass, station, transition)
    assert state.attributes["event_type"] == expected


async def test_the_alert_rides_along_as_an_attribute(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The whole alert, nested `cloud` field and all, is on the entity."""
    await _setup(hass, station)

    state = await _push(hass, station, "alert.expired")
    assert state.attributes[ATTR_ALERT] == _alert()


async def test_the_attribute_is_not_the_connections_own_alert(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Editing the attribute through a template cannot corrupt the open set."""
    entry = await _setup(hass, station)

    state = await _push(hass, station, "alert.triggered")
    state.attributes[ATTR_ALERT]["cloud"]["tags"].append("edited")

    open_alerts = entry.runtime_data.connection.open_alerts
    assert open_alerts[0]["cloud"]["tags"] == ["ops", "night"]


async def test_a_later_transition_replaces_the_last(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The entity shows the last transition, not the first."""
    await _setup(hass, station)

    first = await _push(hass, station, "alert.triggered")
    await station.push("alert.expired", _alert())
    await _until(
        lambda: (
            (state := hass.states.get(_ENTITY_ID)) is not None
            and state.attributes["event_type"] == "unacknowledged"
        ),
        "the expiry to replace the trigger",
    )
    latest = hass.states.get(_ENTITY_ID)
    assert latest is not None
    # A distinct state, not an attribute edit in place: the timestamp is the
    # state, and a repeated one would not fire a state trigger.
    assert latest.state != first.state


async def test_the_join_snapshot_fires_nothing(hass: HomeAssistant, station: FakeStation) -> None:
    """A snapshot is state, not a transition (`event.py` says why)."""
    station.alerts = {"alt_1": _alert()}
    entry = await _setup(hass, station)

    # The connection has an open alert, so the seed and the join snapshot have
    # both been through it -- the entity heard them and fired nothing.
    connection = entry.runtime_data.connection
    await _until(lambda: len(connection.open_alerts) == 1, "the snapshot to reach the connection")
    await hass.async_block_till_done()

    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    assert state.attributes["event_type"] is None


async def test_a_transition_with_no_alert_fires_nothing(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§4.6 says a transition carries its alert; one that does not is dropped."""
    await _setup(hass, station)

    for socket in list(station.sockets):
        await socket.send_json({"event": "alert.expired"})
    # Then a frame that *is* usable, so the assertion is not merely racing the
    # first one through the socket.
    await _push(hass, station, "alert.triggered")

    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    assert state.attributes["event_type"] == "triggered"


async def test_an_unknown_station_event_fires_nothing(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§9 makes new events additive: an older integration ignores them."""
    await _setup(hass, station)

    await station.push("alert.snoozed", _alert())
    await _push(hass, station, "alert.triggered")

    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    assert state.attributes["event_type"] == "triggered"


# -- availability ---------------------------------------------------------


async def test_unavailable_while_the_socket_is_down(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Never a stale board: with nothing listening, the entity says so."""
    entry = await _setup(hass, station)
    await _push(hass, station, "alert.triggered")

    # Refuse the upgrade before dropping the socket, so the reconnect cannot
    # win the race back before the assertion.
    station.events_status = 503
    await station.drop_sockets()
    await _until(
        lambda: not entry.runtime_data.connection.connected, "the connection to notice the drop"
    )
    await hass.async_block_till_done()

    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_available_again_after_a_reconnect(hass: HomeAssistant, station: FakeStation) -> None:
    """And the next transition lands on the entity as before."""
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection

    station.events_status = 503
    await station.drop_sockets()
    await _until(lambda: not connection.connected, "the connection to notice the drop")
    await hass.async_block_till_done()
    assert (down := hass.states.get(_ENTITY_ID)) is not None
    assert down.state == STATE_UNAVAILABLE

    station.events_status = None
    await _until(lambda: connection.connected, "the events socket to come back", timeout=30.0)
    await hass.async_block_till_done()

    state = await _push(hass, station, "alert.expired")
    assert state.attributes["event_type"] == "unacknowledged"


async def test_unavailable_when_the_entry_unloads(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Unloading leaves a registry placeholder, never the last transition."""
    entry = await _setup(hass, station)
    await _push(hass, station, "alert.triggered")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_ENTITY_ID)
    assert state is not None
    # What Home Assistant leaves behind for a registered entity whose platform
    # has gone: the entity id keeps its place, showing nothing.
    assert state.state == STATE_UNAVAILABLE
    assert state.attributes.get("restored") is True
    assert ATTR_ALERT not in state.attributes
