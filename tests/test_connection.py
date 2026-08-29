"""Events socket tests (AHA-16), against the fake station over a real socket.

The connection runs as a background task on the config entry, and Home
Assistant's `async_block_till_done` deliberately does not wait for those -- so
these tests poll for the state they expect rather than pretending the loop is
synchronous. `_until` is that poll, and every wait in this module goes through
it so a failure says what it was waiting for instead of timing out namelessly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster import connection as connection_module
from custom_components.alertroster.api import AlertRosterClient
from custom_components.alertroster.connection import BACKOFF_MAX, StationConnection
from custom_components.alertroster.const import CONF_SOURCE_ID, CONF_STATION_NAME, DOMAIN

from .conftest import FakeStation

# How long a poll waits before calling it a failure. Generous on purpose: every
# `_until` here is waiting on a loopback round-trip that normally takes
# microseconds, so this only ever fires when something is actually wrong.
_TIMEOUT = 10.0


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shrink the reconnect delay so a test does not wait out a real one.

    Not autouse: `test_backoff_*` asserts the shipped numbers, and a fixture
    that quietly replaced them would leave that test asserting itself.
    """
    monkeypatch.setattr(connection_module, "BACKOFF_START", 0.01)
    monkeypatch.setattr(connection_module, "BACKOFF_MAX", 0.05)
    yield


async def _until(check: Callable[[], bool], what: str, timeout: float = _TIMEOUT) -> None:
    """Wait for `check` to hold, or fail saying what never happened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def _setup(hass: HomeAssistant, station: FakeStation) -> MockConfigEntry:
    """A paired station, set up the way the config flow leaves it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="studio",
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
    return entry


def _connection(entry: MockConfigEntry) -> StationConnection:
    """The connection the entry set up."""
    connection: StationConnection = entry.runtime_data.connection
    return connection


async def _connected(entry: MockConfigEntry) -> StationConnection:
    """Wait until the entry's socket is open, then hand back the connection."""
    connection = _connection(entry)
    await _until(lambda: connection.connected, "the events socket to open")
    return connection


def _seeds(station: FakeStation) -> int:
    """How many times the integration has read `GET /v1/alerts`."""
    return station.requests.count(("GET", "/v1/alerts"))


def _open_alert(station: FakeStation, alert_id: str, title: str) -> dict[str, Any]:
    """Put an open alert on the station without going through HTTP.

    Deliberately not `POST /v1/alerts`: these tests are about what the
    connection *learns*, and raising it over HTTP would leave the count of
    seeds and requests carrying traffic the assertions are not about.
    """
    alert = {
        "id": alert_id,
        "status": "triggered",
        "title": title,
        "detail": None,
        "urgency": "high",
        "dedup_key": None,
        "ack_timeout_seconds": 120,
        "cloud": {"synced": False},
    }
    station.alerts[alert_id] = alert
    return alert


# -- setup, seed and connect ----------------------------------------------


async def test_setup_seeds_from_the_alerts_endpoint_then_holds_the_socket(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.6's first bullet: `GET /v1/alerts`, then `GET /v1/events` held open."""
    existing = _open_alert(station, "la_seeded", "Already open")
    station.send_join_snapshot = False

    entry = await _setup(hass, station)
    connection = await _connected(entry)

    assert _seeds(station) == 1
    assert ("GET", "/v1/events") in station.requests
    assert [a["id"] for a in connection.open_alerts] == [existing["id"]]
    assert station.sockets, "the station should still be holding the socket open"


async def test_setup_does_not_wait_for_an_unreachable_station(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The entry loads anyway, so `raise` can still be attempted over HTTP (§3.6).

    The obvious alternative -- `ConfigEntryNotReady` -- would leave the actions
    unavailable exactly when an automation is trying to page somebody about the
    thing that took the station down.
    """
    await station.stop()

    entry = await _setup(hass, station)

    assert entry.state is ConfigEntryState.LOADED
    assert _connection(entry).connected is False


async def test_open_alerts_hands_out_a_copy(hass: HomeAssistant, station: FakeStation) -> None:
    """A caller that edits what it was given must not be editing the state."""
    _open_alert(station, "la_copy", "Mine")
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    connection.open_alerts[0]["title"] = "vandalised"

    assert connection.open_alerts[0]["title"] == "Mine"


async def test_an_alert_with_no_id_is_dropped(hass: HomeAssistant, station: FakeStation) -> None:
    """Un-addressable, un-resolvable, and impossible to match later."""
    station.alerts["la_ok"] = {"id": "la_ok", "status": "triggered", "title": "Fine"}
    station.alerts["broken"] = {"status": "triggered", "title": "No id"}

    entry = await _setup(hass, station)
    connection = await _connected(entry)

    assert [a["id"] for a in connection.open_alerts] == ["la_ok"]


# -- listeners ------------------------------------------------------------


async def test_connecting_notifies_listeners(hass: HomeAssistant, station: FakeStation) -> None:
    """§3.6: connected/disconnected transitions reach listeners at once."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    seen: list[bool] = []
    remove = connection.async_add_listener(lambda: seen.append(connection.connected))

    await station.drop_sockets()
    await _until(lambda: seen and seen[-1] is False, "the disconnect to reach the listener")
    remove()

    assert False in seen


async def test_a_removed_listener_stops_hearing(hass: HomeAssistant, station: FakeStation) -> None:
    """The handle `async_add_listener` returns actually unregisters."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    calls: list[None] = []
    remove = connection.async_add_listener(lambda: calls.append(None))
    remove()

    await station.push("alert.triggered", _open_alert(station, "la_after", "After"))
    await _until(
        lambda: any(a["id"] == "la_after" for a in connection.open_alerts),
        "the transition to be applied",
    )

    assert calls == []


async def test_a_transition_updates_the_open_alerts(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Triggered adds, acknowledged keeps, resolved and expired remove."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    alert = _open_alert(station, "la_live", "Freezer")
    await station.push("alert.triggered", alert)
    await _until(lambda: len(connection.open_alerts) == 1, "the alert to be added")

    # Somebody answered; the condition has not cleared, so it stays open.
    await station.push("alert.acknowledged", {**alert, "status": "acknowledged"})
    await _until(
        lambda: connection.open_alerts[0]["status"] == "acknowledged",
        "the acknowledgement to be applied",
    )

    await station.push("alert.expired", {**alert, "status": "expired"})
    await _until(lambda: connection.open_alerts == [], "the expiry to close the alert")


async def test_an_unknown_transition_does_not_kill_the_socket(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§9 makes new events additive: a newer station must not stop this one."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    await station.push("alert.snoozed", {"id": "la_new", "status": "snoozed"})
    await station.push("alert.triggered", _open_alert(station, "la_known", "Known"))

    await _until(
        lambda: [a["id"] for a in connection.open_alerts] == ["la_known"],
        "the known transition after the unknown one",
    )
    assert connection.connected is True


# -- reconnect and re-seed ------------------------------------------------


async def test_reconnect_re_seeds_from_the_alerts_endpoint(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """§3.6's headline: the socket carries transitions, so re-read the snapshot.

    The station here is the one the protocol document describes -- no join
    snapshot -- so the re-seed is the only way this alert can be learned. That
    is the point: with the shipped service's snapshot left on, this test would
    pass even if the re-seed were deleted.
    """
    station.send_join_snapshot = False
    entry = await _setup(hass, station)
    connection = await _connected(entry)
    assert connection.open_alerts == []

    # Raised on the station with nobody listening -- no transition is pushed,
    # so nothing about it can reach Home Assistant except a fresh `GET`.
    _open_alert(station, "la_missed", "Happened while we were away")
    await station.drop_sockets()

    await _until(lambda: _seeds(station) >= 2, "the re-seed after reconnecting")
    await _until(lambda: connection.connected, "the socket to come back")
    assert [a["id"] for a in connection.open_alerts] == ["la_missed"]


async def test_the_shipped_stations_join_snapshot_is_applied_too(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A divergence from §4.6, so it is belt-and-braces rather than the route."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    _open_alert(station, "la_snap", "In the snapshot")
    await station.drop_sockets()

    await _until(
        lambda: [a["id"] for a in connection.open_alerts] == ["la_snap"],
        "the join snapshot to be applied",
    )


async def test_disconnected_while_the_socket_is_refused(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """Never show stale state: no socket means not connected, and it keeps trying."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    station.events_status = 503
    await station.drop_sockets()
    await _until(lambda: not connection.connected, "the connection to be reported down")

    attempts = station.requests.count(("GET", "/v1/events"))
    await _until(
        lambda: station.requests.count(("GET", "/v1/events")) > attempts,
        "another reconnect attempt",
    )
    assert connection.connected is False

    station.events_status = None
    await _until(lambda: connection.connected, "the socket to come back on its own")


async def test_backoff_doubles_from_one_second_and_caps_at_sixty(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.6: jittered exponential backoff, 1 s to a 60 s cap.

    Read off the policy rather than timed: timing it would mean sitting through
    a minute of real backoff to learn what arithmetic already says.
    """
    connection = StationConnection(
        hass,
        MockConfigEntry(domain=DOMAIN, title="studio"),  # type: ignore[arg-type]
        AlertRosterClient(async_get_clientsession(hass), station.host, station.port, "lat_x"),
    )

    delays = [connection._next_backoff() for _ in range(10)]  # noqa: SLF001

    for delay, undelayed in zip(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 32.0] + [60.0] * 4, strict=True):
        # Jitter is a fraction of the delay, so the cap stays a cap and the
        # first retry never comes sooner than half a second.
        assert undelayed / 2 <= delay <= undelayed
    assert max(delays) <= BACKOFF_MAX


# -- revocation -----------------------------------------------------------


async def test_a_revoked_token_starts_reauth_and_stops(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """§3.6: a `401` is reauth, never a reconnect loop."""
    entry = await _setup(hass, station)
    await _connected(entry)

    station.revoked = True
    await station.drop_sockets()

    await _until(
        lambda: bool(hass.config_entries.flow.async_progress_by_handler(DOMAIN)),
        "the reauth flow to start",
    )
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert _connection(entry).connected is False

    # And then it stops asking. The backoff is 10 ms under `fast_backoff`, so a
    # loop that was still retrying would have made several attempts by now.
    settled = len(station.requests)
    await asyncio.sleep(0.2)
    assert len(station.requests) == settled


async def test_the_reauth_a_revoked_token_starts_can_be_finished(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """The whole way round: `401` -> reauth form -> new token -> connected again.

    AHA-16 and AHA-9 meet here. Each is tested on its own either side of this,
    but only this says the entry actually recovers -- which is the thing the
    person whose station stopped paging them cares about.
    """
    entry = await _setup(hass, station)
    await _connected(entry)
    revoked_token = entry.data[CONF_TOKEN]

    station.revoked = True
    await station.drop_sockets()
    await _until(
        lambda: bool(hass.config_entries.flow.async_progress_by_handler(DOMAIN)),
        "the reauth flow to start",
    )

    # The station is put back the way re-opening its pairing window puts it.
    station.revoked = False
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], None)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"code": station.valid_code}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] != revoked_token
    # The reload that reauth triggers brings a fresh connection up on the new
    # token; nothing further is asked of the user.
    await _connected(entry)


async def test_a_token_revoked_before_setup_starts_reauth(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """The seed is where a `401` shows up first, and it is treated the same."""
    station.revoked = True

    await _setup(hass, station)

    await _until(
        lambda: bool(hass.config_entries.flow.async_progress_by_handler(DOMAIN)),
        "the reauth flow to start from the seed",
    )


# -- unload ---------------------------------------------------------------


async def test_unload_closes_the_socket(hass: HomeAssistant, station: FakeStation) -> None:
    """§3.6's last bullet, and what stops a reload holding two sockets open."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Immediate, because `async_stop` awaits the task rather than firing the
    # cancel and returning. The station drops its half a round-trip later,
    # which is as prompt as an observation made from the other end can be.
    assert connection.connected is False
    await _until(lambda: station.sockets == [], "the station to see the socket close")


async def test_reload_leaves_one_socket(hass: HomeAssistant, station: FakeStation) -> None:
    """The old connection is gone before the new one opens."""
    entry = await _setup(hass, station)
    await _connected(entry)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _connected(entry)

    assert len(station.sockets) == 1


async def test_unload_survives_an_events_task_that_died(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in the loop must not leave an entry that cannot be removed.

    Every failure the *station* can cause is already handled, so this stands in
    for one this file caused itself; what is being asserted is that the user
    still has a way out of it, and that it was not swallowed on the way.
    """
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    exploded = asyncio.Event()

    async def explode() -> None:
        exploded.set()
        raise RuntimeError("a bug, not a station")

    connection._async_seed = explode  # type: ignore[method-assign]  # noqa: SLF001
    await station.drop_sockets()
    # The crash comes after the backoff, so waiting on `connected` would race
    # the sleep and cancel the task before it ever got to fall over.
    await asyncio.wait_for(exploded.wait(), _TIMEOUT)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "a bug, not a station" in caplog.text
