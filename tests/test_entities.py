"""The station's entities: alerting, open alerts, connected (AHA-20..24).

Driven the way `test_event.py` is -- frames off a real socket, through
`api.py` and `connection.py`, asserting the state Home Assistant ends up with.
The three entities here are what a dashboard shows, so a failure is a board
that would have been wrong rather than an internal detail.

The availability tests are the point of the module (AHA-23, and §1's "a stale
board is worse than a blank one"). They are written to fail two ways: an entity
frozen at its last value while the socket is down, and an entity that comes
back before the post-reconnect re-seed has landed -- which would show the board
as it was *before* the outage for as long as the seed took.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.const import (
    ATTR_ALERTS,
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    CONF_STATION_VERSION,
    DOMAIN,
)

from .conftest import FakeStation

_TIMEOUT = 10.0
_ALERTING = "binary_sensor.studio_alerting"
_CONNECTED = "binary_sensor.studio_connected"
_OPEN_ALERTS = "sensor.studio_open_alerts"


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
    hass: HomeAssistant,
    station: FakeStation,
    title: str = "studio",
    *,
    connect: bool = True,
    version: str | None = None,
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
            CONF_STATION_VERSION: version,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if connect:
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
        # Nested, so a test can prove the attribute is not the connection's own
        # copy: §2 passes this through untouched.
        "cloud": {"synced": True, "id": "cl_9", "tags": ["ops", "night"]},
    }


async def _push(
    hass: HomeAssistant, station: FakeStation, event: str, alert: dict[str, Any]
) -> None:
    """Send one transition and wait for the count to catch up with it."""
    before = _state(hass, _OPEN_ALERTS).state
    await station.push(event, alert)
    await _until(
        lambda: _state(hass, _OPEN_ALERTS).state != before,
        f"{event} to reach {_OPEN_ALERTS}",
    )
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> Any:
    """The entity's state, insisting it exists."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} has no state"
    return state


async def _drop(hass: HomeAssistant, station: FakeStation, entry: MockConfigEntry) -> None:
    """Take the events socket down and keep it down."""
    # Refuse the upgrade before dropping, so the reconnect cannot win the race
    # back before the assertion that follows.
    station.events_status = 503
    await station.drop_sockets()
    await _until(
        lambda: not entry.runtime_data.connection.connected, "the connection to notice the drop"
    )
    await hass.async_block_till_done()


# -- the entities themselves ----------------------------------------------


async def test_entities_are_created_on_the_station_device(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.5: three entities, one device, unique ids that survive a re-pair."""
    entry = await _setup(hass, station)

    registry = er.async_get(hass)
    expected = {
        _ALERTING: f"{entry.entry_id}_alerting",
        _CONNECTED: f"{entry.entry_id}_connected",
        _OPEN_ALERTS: f"{entry.entry_id}_open_alerts",
    }
    for entity_id, unique_id in expected.items():
        entity = registry.async_get(entity_id)
        assert entity is not None, f"{entity_id} was never created"
        # The entry id, not `source_id`: §6.2 remints the source row on every
        # pairing, so a unique id built from it would be replaced -- and the
        # entity rebuilt -- the first time a revoked token was re-paired.
        assert entity.unique_id == unique_id

        device = dr.async_get(hass).async_get(entity.device_id or "")
        assert device is not None
        assert device.identifiers == {(DOMAIN, entry.entry_id)}
        assert device.name == "studio"


async def test_the_device_carries_the_station_version(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """AHA-20: what the config flow learned off §4.1's probe, on the device page."""
    entry = await _setup(hass, station, version="2.0.0")

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.sw_version == "2.0.0"


async def test_a_station_with_no_version_gets_a_device_without_one(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A station too old to answer the probe: no version, not a guessed one."""
    entry = await _setup(hass, station)

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.sw_version is None


async def test_the_device_classes_are_the_ones_the_ui_renders(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """`problem` and `connectivity` are what make the UI say the right words."""
    await _setup(hass, station)

    assert _state(hass, _ALERTING).attributes["device_class"] == BinarySensorDeviceClass.PROBLEM
    assert (
        _state(hass, _CONNECTED).attributes["device_class"] == BinarySensorDeviceClass.CONNECTIVITY
    )
    assert _state(hass, _OPEN_ALERTS).attributes["state_class"] == SensorStateClass.MEASUREMENT


async def test_a_quiet_station_is_off_and_empty(hass: HomeAssistant, station: FakeStation) -> None:
    """Nothing raised: connected, not alerting, no open alerts."""
    await _setup(hass, station)

    assert _state(hass, _CONNECTED).state == STATE_ON
    assert _state(hass, _ALERTING).state == STATE_OFF
    assert _state(hass, _OPEN_ALERTS).state == "0"
    assert _state(hass, _OPEN_ALERTS).attributes[ATTR_ALERTS] == []


# -- what the alert lifecycle does to them --------------------------------


async def test_an_alert_turns_alerting_on_and_counts_it(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The whole lifecycle, one transition at a time (§4.6)."""
    await _setup(hass, station)

    await _push(hass, station, "alert.triggered", _alert())
    assert _state(hass, _ALERTING).state == STATE_ON
    assert _state(hass, _OPEN_ALERTS).state == "1"
    assert _state(hass, _OPEN_ALERTS).attributes[ATTR_ALERTS] == [_alert()]

    # Acknowledged is still open: somebody answered, but the condition has not
    # cleared and `GET /v1/alerts` keeps returning it.
    await station.push("alert.acknowledged", _alert(status="acknowledged"))
    await _until(
        lambda: _state(hass, _OPEN_ALERTS).attributes[ATTR_ALERTS][0]["status"] == "acknowledged",
        "the acknowledgement to reach the sensor",
    )
    await hass.async_block_till_done()
    assert _state(hass, _ALERTING).state == STATE_ON
    assert _state(hass, _OPEN_ALERTS).state == "1"

    await _push(hass, station, "alert.resolved", _alert(status="resolved"))
    assert _state(hass, _ALERTING).state == STATE_OFF
    assert _state(hass, _OPEN_ALERTS).state == "0"
    assert _state(hass, _OPEN_ALERTS).attributes[ATTR_ALERTS] == []


async def test_an_expiry_closes_the_alert_too(hass: HomeAssistant, station: FakeStation) -> None:
    """Nobody answered is still an alert the station stopped holding open."""
    await _setup(hass, station)

    await _push(hass, station, "alert.triggered", _alert())
    await _push(hass, station, "alert.expired", _alert(status="expired"))

    assert _state(hass, _ALERTING).state == STATE_OFF
    assert _state(hass, _OPEN_ALERTS).state == "0"


async def test_several_alerts_are_counted_and_listed(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The count is the board's size, and the attribute is the board."""
    await _setup(hass, station)

    await _push(hass, station, "alert.triggered", _alert())
    await _push(hass, station, "alert.triggered", _alert(alert_id="alt_2"))
    assert _state(hass, _OPEN_ALERTS).state == "2"
    assert {a["id"] for a in _state(hass, _OPEN_ALERTS).attributes[ATTR_ALERTS]} == {
        "alt_1",
        "alt_2",
    }

    await _push(hass, station, "alert.resolved", _alert(status="resolved"))
    assert _state(hass, _OPEN_ALERTS).state == "1"
    assert [a["id"] for a in _state(hass, _OPEN_ALERTS).attributes[ATTR_ALERTS]] == ["alt_2"]
    # One resolved of two is still somebody being paged.
    assert _state(hass, _ALERTING).state == STATE_ON


async def test_the_attribute_is_not_the_connections_own_alerts(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Editing the attribute through a template cannot corrupt the open set."""
    entry = await _setup(hass, station)

    await _push(hass, station, "alert.triggered", _alert())
    _state(hass, _OPEN_ALERTS).attributes[ATTR_ALERTS][0]["cloud"]["tags"].append("edited")

    open_alerts = entry.runtime_data.connection.open_alerts
    assert open_alerts[0]["cloud"]["tags"] == ["ops", "night"]


async def test_two_stations_count_only_their_own(
    hass: HomeAssistant, station: FakeStation, second_station: FakeStation
) -> None:
    """§6.2 scopes a token to its own source, and so do the entities."""
    await _setup(hass, station)
    await _setup(hass, second_station, title="kitchen")

    await _push(hass, station, "alert.triggered", _alert())

    assert _state(hass, _OPEN_ALERTS).state == "1"
    assert _state(hass, "sensor.kitchen_open_alerts").state == "0"
    assert _state(hass, "binary_sensor.kitchen_alerting").state == STATE_OFF


# -- availability ---------------------------------------------------------


async def test_socket_loss_takes_the_board_away(hass: HomeAssistant, station: FakeStation) -> None:
    """A stale board is worse than a blank one: everything but `connected` goes."""
    entry = await _setup(hass, station)
    await _push(hass, station, "alert.triggered", _alert())

    await _drop(hass, station, entry)

    assert _state(hass, _ALERTING).state == STATE_UNAVAILABLE
    assert _state(hass, _OPEN_ALERTS).state == STATE_UNAVAILABLE
    # Not `unavailable`: this is the entity the outage is reported by.
    assert _state(hass, _CONNECTED).state == STATE_OFF


async def test_unavailable_before_the_socket_ever_opens(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A station reachable over HTTP but not over the socket is still unknown."""
    station.events_status = 503
    station.alerts = {"alt_1": _alert()}
    entry = await _setup(hass, station, connect=False)

    assert entry.state is ConfigEntryState.LOADED
    # The seed runs before the socket, so the connection ends up holding an
    # alert with the socket still refused -- and the entities must not render
    # it, because nothing is listening for what happens to it next.
    connection = entry.runtime_data.connection
    await _until(lambda: connection.open_alert_count == 1, "the seed to land")
    await hass.async_block_till_done()
    assert not connection.connected
    assert _state(hass, _ALERTING).state == STATE_UNAVAILABLE
    assert _state(hass, _OPEN_ALERTS).state == STATE_UNAVAILABLE
    assert _state(hass, _CONNECTED).state == STATE_OFF


async def test_the_board_comes_back_re_seeded_after_a_reconnect(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§4.6: what changed during the outage is only in `GET /v1/alerts`.

    The alert is resolved on the station *while the socket is down*, so the
    transition that closed it is never replayed. An integration that restored
    availability from its pre-outage state would show the alert as still open;
    only the re-seed can know otherwise.
    """
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection

    await _push(hass, station, "alert.triggered", _alert())
    assert _state(hass, _OPEN_ALERTS).state == "1"

    await _drop(hass, station, entry)
    # The station clears the alert with nobody listening.
    station.alerts = {}

    station.events_status = None
    await _until(lambda: connection.connected, "the events socket to come back", timeout=30.0)
    await hass.async_block_till_done()

    assert _state(hass, _CONNECTED).state == STATE_ON
    assert _state(hass, _OPEN_ALERTS).state == "0"
    assert _state(hass, _ALERTING).state == STATE_OFF


async def test_the_seed_lands_before_the_entities_come_back(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Availability returns with the board already true, never a moment before.

    `connection.py` seeds before it reports `connected`, so an entity woken by
    the reconnect reads the station's current alerts. Asserting it from the
    outside: at the first write in which the entities are available again, the
    count is already the station's, not the one from before the outage.
    """
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection

    await _push(hass, station, "alert.triggered", _alert())
    await _drop(hass, station, entry)

    # Two alerts on the station while nothing is listening, so "re-seeded" is a
    # different number from both the pre-outage count and zero.
    station.alerts = {"alt_2": _alert(alert_id="alt_2"), "alt_3": _alert(alert_id="alt_3")}

    # Every write, in order, rather than the connection's own listeners: those
    # are a set, so an assertion made from inside one would pass or fail on
    # whether the entity's listener happened to run first.
    written: list[str] = []

    @callback
    def _note(event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None:
            written.append(state.state)

    unsubscribe = async_track_state_change_event(hass, [_OPEN_ALERTS], _note)
    station.events_status = None
    await _until(lambda: connection.connected, "the events socket to come back", timeout=30.0)
    await hass.async_block_till_done()
    unsubscribe()

    available = [state for state in written if state != STATE_UNAVAILABLE]
    assert available, "the entities never came back"
    # The first thing shown after the outage is the station's board, not the
    # one from before it.
    assert available[0] == "2"
