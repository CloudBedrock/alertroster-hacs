"""Station transitions on the Home Assistant bus (AHA-17, AHA-19).

Driven through the whole stack rather than by calling `events.py` directly: the
frames come off a real socket from the fake station, through `api.py`'s parser
and `connection.py`'s loop, so a test failing here means an automation would
not have fired. Nothing is mocked between the station and `hass.bus`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.template import Template
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.api import StationEvent
from custom_components.alertroster.connection import _CLOSES_ALERT, _OPENS_ALERT
from custom_components.alertroster.const import (
    ATTR_ALERT,
    ATTR_STATION,
    BUS_EVENTS,
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DOMAIN,
    EVENT_ACKNOWLEDGED,
    EVENT_RESOLVED,
    EVENT_TRIGGERED,
    EVENT_UNACKNOWLEDGED,
)

from .conftest import FakeStation

_TIMEOUT = 10.0


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
    return entry


class _Bus:
    """Every AlertRoster event fired, in order."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.events: list[Event] = []
        for event_type in set(BUS_EVENTS.values()):

            @callback
            def record(event: Event) -> None:
                self.events.append(event)

            hass.bus.async_listen(event_type, record)

    def types(self) -> list[str]:
        # `event_type` is typed as `EventType[...] | str`; it is the plain
        # string for everything this integration fires.
        return [str(event.event_type) for event in self.events]

    def only(self) -> Event:
        assert len(self.events) == 1, f"expected one event, got {self.types()}"
        return self.events[0]


@pytest.fixture
def bus(hass: HomeAssistant) -> _Bus:
    """Record every bus event this integration fires."""
    return _Bus(hass)


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
        # Nested on purpose: §2 passes this through untouched, and a shallow
        # copy would share it.
        "cloud": {"synced": True, "id": "cl_9", "tags": ["ops", "night"]},
    }


# -- the mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("transition", "expected"),
    [
        ("alert.triggered", EVENT_TRIGGERED),
        ("alert.acknowledged", EVENT_ACKNOWLEDGED),
        ("alert.resolved", EVENT_RESOLVED),
        ("alert.expired", EVENT_UNACKNOWLEDGED),
    ],
)
async def test_each_transition_fires_its_bus_event(
    hass: HomeAssistant, station: FakeStation, bus: _Bus, transition: str, expected: str
) -> None:
    """§3.4's table, one row at a time, over a real socket."""
    await _setup(hass, station)

    await station.push(transition, _alert())
    await _until(lambda: bool(bus.events), f"{expected} to fire")
    await hass.async_block_till_done()

    assert bus.only().event_type == expected


async def test_an_expiry_is_named_unacknowledged_not_expired(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """The one name that is not the station's word for it.

    Called out separately because it is the event the integration exists for
    and the only one a reader could 'fix' into `alertroster_expired`.
    """
    await _setup(hass, station)

    await station.push("alert.expired", _alert(status="expired"))
    await _until(lambda: bool(bus.events), "the expiry event to fire")

    assert bus.only().event_type == "alertroster_unacknowledged"


def test_the_mapping_covers_every_transition_the_connection_knows() -> None:
    """`connection.py` and `const.py` must not drift apart.

    They hold the station's event names separately -- one to decide whether an
    alert is still open, the other to name the bus event. A transition in one
    and not the other is either an alert that changes state while firing
    nothing, or an event fired for something the open-alert set ignored.
    """
    assert set(BUS_EVENTS) == _OPENS_ALERT | _CLOSES_ALERT


# -- what the event carries -----------------------------------------------


async def test_the_event_carries_the_whole_alert_and_the_station(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """Every field the station sent, plus which station sent it."""
    await _setup(hass, station)
    sent = _alert()

    await station.push("alert.triggered", sent)
    await _until(lambda: bool(bus.events), "the trigger event to fire")

    data = bus.only().data
    assert data[ATTR_ALERT] == sent
    assert data[ATTR_STATION] == "studio"


async def test_the_cloud_field_is_passed_through_unmodified(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """REQUIREMENTS.md §2: the station owns the cloud link, this only relays it."""
    await _setup(hass, station)
    sent = _alert()

    await station.push("alert.triggered", sent)
    await _until(lambda: bool(bus.events), "the trigger event to fire")

    assert bus.only().data[ATTR_ALERT]["cloud"] == {
        "synced": True,
        "id": "cl_9",
        "tags": ["ops", "night"],
    }


async def test_the_station_name_follows_a_rename(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """Renaming the entry renames the station in the next event, not the next restart."""
    entry = await _setup(hass, station)

    hass.config_entries.async_update_entry(entry, title="kitchen")
    await hass.async_block_till_done()

    await station.push("alert.triggered", _alert())
    await _until(lambda: bool(bus.events), "the trigger event to fire")

    assert bus.only().data[ATTR_STATION] == "kitchen"


async def test_the_event_payload_cannot_be_edited_into_the_connection(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """A listener reaching into the event must not rewrite the open-alert set.

    The frame's dict *is* the connection's state, so this is a copy, not a
    courtesy: a template touching `trigger.event.data` would otherwise edit
    what the next entity reads back.
    """
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection

    await station.push("alert.triggered", _alert())
    await _until(lambda: bool(bus.events), "the trigger event to fire")

    payload = bus.only().data[ATTR_ALERT]
    payload["title"] = "rewritten"
    payload["cloud"]["synced"] = "rewritten"

    held = connection.open_alerts[0]
    assert held["title"] == "Front door"
    assert held["cloud"]["synced"] is True


async def test_the_readme_automation_renders(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """The "React when nobody answered" example in README.md, actually rendered.

    The README is the first thing anyone copies, and its templates reach into
    the event by path -- renaming `alert` or dropping a field would leave those
    rendering empty strings into somebody's notification rather than failing
    anywhere visible.
    """
    await _setup(hass, station)

    await station.push("alert.expired", _alert(status="expired"))
    await _until(lambda: bool(bus.events), "the expiry event to fire")

    trigger = {"trigger": {"event": {"data": bus.only().data}}}
    title = Template("Nobody answered: {{ trigger.event.data.alert.title }}", hass).async_render(
        trigger, parse_result=False
    )
    message = Template(
        "The panel rang for {{ trigger.event.data.alert.ack_timeout_seconds }}s.", hass
    ).async_render(trigger, parse_result=False)
    which = Template("{{ trigger.event.data.station }}", hass).async_render(
        trigger, parse_result=False
    )

    assert title == "Nobody answered: Front door"
    assert message == "The panel rang for 120s."
    assert which == "studio"


async def test_the_open_alerts_are_current_when_the_event_fires(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """An automation woken by the bus reads state through a template.

    So the transition has to be folded in *before* the event goes out: an
    automation triggered by `alertroster_triggered` that counts open alerts
    must see the one that woke it, and one triggered by a resolve must not.
    """
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection
    seen: list[list[str]] = []

    @callback
    def record(_event: Event) -> None:
        seen.append(sorted(alert["id"] for alert in connection.open_alerts))

    hass.bus.async_listen(EVENT_TRIGGERED, record)
    hass.bus.async_listen(EVENT_RESOLVED, record)

    await station.push("alert.triggered", _alert("alt_1"))
    await _until(lambda: len(seen) == 1, "the trigger event to fire")
    await station.push("alert.resolved", _alert("alt_1", status="resolved"))
    await _until(lambda: len(seen) == 2, "the resolve event to fire")

    assert seen == [["alt_1"], []]


# -- what must not fire ---------------------------------------------------


async def test_the_join_snapshot_fires_nothing(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """State, not a transition.

    The shipped service sends the source's open alerts on join, and the socket
    reconnects on its own. Firing `alertroster_triggered` for each of them
    would re-page for alerts raised hours ago every time the link blinked.
    """
    station.alerts["alt_old"] = _alert("alt_old")
    station.send_join_snapshot = True

    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection

    # The snapshot is what put the alert in the set, so waiting for the set
    # proves the frame was delivered and not merely slow.
    await _until(lambda: len(connection.open_alerts) == 1, "the snapshot to be applied")
    await hass.async_block_till_done()

    assert bus.types() == []


async def test_a_reconnect_re_seed_fires_nothing(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """The same argument for the HTTP seed: a re-read is not four new alerts."""
    station.alerts["alt_old"] = _alert("alt_old")

    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection
    await _until(lambda: len(connection.open_alerts) == 1, "the first seed")

    await station.drop_sockets()
    await _until(lambda: not connection.connected, "the socket to drop")
    await _until(lambda: connection.connected, "the socket to come back")
    await hass.async_block_till_done()

    assert bus.types() == []


async def test_an_unknown_transition_fires_nothing_and_keeps_the_socket(
    hass: HomeAssistant, station: FakeStation, bus: _Bus, caplog: pytest.LogCaptureFixture
) -> None:
    """§9 makes station events additive: a newer station must not break this one."""
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection

    await station.push("alert.snoozed", _alert())
    # Nothing observable follows an ignored frame, so prove the socket survived
    # by sending a known one after it and watching that arrive.
    await station.push("alert.expired", _alert())
    await _until(lambda: bool(bus.events), "the expiry after it to fire")
    await hass.async_block_till_done()

    assert bus.types() == [EVENT_UNACKNOWLEDGED]
    assert connection.connected
    assert "Ignoring an unknown station event 'alert.snoozed'" in caplog.text


async def test_a_transition_with_no_alert_fires_nothing(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """A station contradicting §4.6.

    Firing `alertroster_unacknowledged` with nothing attached would wake every
    automation listening for it and give them nothing to act on.
    """
    await _setup(hass, station)

    for socket in list(station.sockets):
        await socket.send_json({"event": "alert.expired"})
    await station.push("alert.triggered", _alert())
    await _until(lambda: bool(bus.events), "the trigger after it to fire")

    assert bus.types() == [EVENT_TRIGGERED]


async def test_a_transition_with_an_empty_alert_fires_nothing(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """`"alert": {}` is the same broken frame with a different spelling.

    It survives `api.py`'s parser, which only checks the value is a dict, and
    `connection.py` discards it for having no id -- so if this fired, every
    automation would wake to an empty alert and nothing would compensate.
    """
    await _setup(hass, station)

    await station.push("alert.expired", {})
    await station.push("alert.triggered", _alert())
    await _until(lambda: bool(bus.events), "the trigger after it to fire")

    assert bus.types() == [EVENT_TRIGGERED]


# -- the wiring -----------------------------------------------------------


async def test_unloading_the_entry_unregisters_the_listener(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """An unloaded entry must not still be firing on the bus.

    "No events after the unload" is on its own too weak to test the
    `entry.async_on_unload` wiring: `async_unload_entry` also stops the socket,
    so a listener that was never unregistered would look identical. So this
    fires one first as a positive control, and then asserts the registration
    itself is gone rather than merely quiet.
    """
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection

    await station.push("alert.triggered", _alert())
    await _until(lambda: bool(bus.events), "an event to fire while loaded")
    fired_while_loaded = bus.types()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await station.push("alert.triggered", _alert("alt_2"))
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert fired_while_loaded == [EVENT_TRIGGERED]
    assert bus.types() == fired_while_loaded  # nothing more arrived
    # Reaching into the private set on purpose: it is the only difference
    # between "the unload callback ran" and "the socket happened to be shut".
    assert connection._transitions == {}


async def test_reloading_the_entry_fires_each_transition_once(
    hass: HomeAssistant, station: FakeStation, bus: _Bus
) -> None:
    """A reload sets the entry up again; it must not leave two listeners behind."""
    entry = await _setup(hass, station)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    connection = entry.runtime_data.connection
    await _until(lambda: connection.connected, "the socket to come back after the reload")

    await station.push("alert.expired", _alert())
    await _until(lambda: bool(bus.events), "the expiry to fire")
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert bus.types() == [EVENT_UNACKNOWLEDGED]


async def test_a_listener_that_raises_does_not_stop_the_others(
    hass: HomeAssistant, station: FakeStation, bus: _Bus, caplog: pytest.LogCaptureFixture
) -> None:
    """The bus events are why this integration exists; one bad listener cannot cost them.

    Registered directly on the connection rather than on the bus, because it is
    `_async_dispatch`'s guard being tested -- a listener raising there would
    otherwise take the socket task down with it.

    The recorder is registered *after* the one that raises, and dispatch runs
    in registration order, so "the others" here means listeners that come after
    the failure -- the ones a guard that merely logged and stopped would lose.
    """
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection
    reached: list[str] = []

    def explode(_event: StationEvent) -> None:
        raise RuntimeError("listener is broken")

    def record(event: StationEvent) -> None:
        reached.append(event.event)

    connection.async_add_transition_listener(explode)
    connection.async_add_transition_listener(record)

    await station.push("alert.expired", _alert())
    await _until(lambda: bool(bus.events), "the expiry to fire anyway")
    await _until(lambda: bool(reached), "the listener after the broken one to run")
    await hass.async_block_till_done()

    assert reached == ["alert.expired"]
    assert bus.types() == [EVENT_UNACKNOWLEDGED]
    assert connection.connected
    assert "listener is broken" in caplog.text
