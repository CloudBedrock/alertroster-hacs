"""Events socket tests (AHA-16), against the fake station over a real socket.

The connection runs as a background task on the config entry, and Home
Assistant's `async_block_till_done` deliberately does not wait for those -- so
these tests poll for the state they expect rather than pretending the loop is
synchronous. `_until` is that poll, and every wait in this module goes through
it so a failure says what it was waiting for instead of timing out namelessly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster import connection as connection_module
from custom_components.alertroster.api import AlertRosterClient
from custom_components.alertroster.connection import (
    BACKOFF_MAX,
    BACKOFF_START,
    StationConnection,
)
from custom_components.alertroster.const import CONF_SOURCE_ID, CONF_STATION_NAME, DOMAIN

from .conftest import FakeStation, until

# How long a test waits on the events task before calling it stuck.
_TIMEOUT = 10.0


# How long a poll waits before calling it a failure. Generous on purpose: every
# `_until` here is waiting on a loopback round-trip that normally takes
# microseconds, so this only ever fires when something is actually wrong.
@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shrink the reconnect delay so a test does not wait out a real one.

    Not autouse: `test_backoff_*` asserts the shipped numbers, and a fixture
    that quietly replaced them would leave that test asserting itself.
    """
    monkeypatch.setattr(connection_module, "BACKOFF_START", 0.01)
    monkeypatch.setattr(connection_module, "BACKOFF_MAX", 0.05)
    yield


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
    await until(lambda: connection.connected, "the events socket to open")
    return connection


def _seeds(station: FakeStation) -> int:
    """How many times the integration has read `GET /v1/alerts`."""
    return station.requests.count(("GET", "/v1/alerts"))


def _bare_connection(hass: HomeAssistant, station: FakeStation) -> StationConnection:
    """A connection that was never started, for asserting on policy directly.

    Reading the backoff off the object beats timing it: timing would mean
    sitting through a real minute of it to learn what arithmetic already says.
    """
    return StationConnection(
        hass,
        MockConfigEntry(domain=DOMAIN, title="studio"),  # type: ignore[arg-type]
        AlertRosterClient(async_get_clientsession(hass), station.host, station.port, "lat_x"),
    )


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
    """A caller that edits what it was given must not be editing the state.

    Nested as well as top-level: an alert carries the `cloud` object §2 passes
    through untouched, and a shallow copy would hand that out by reference.
    """
    _open_alert(station, "la_copy", "Mine")
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    connection.open_alerts[0]["title"] = "vandalised"
    connection.open_alerts[0]["cloud"]["synced"] = "vandalised"

    assert connection.open_alerts[0]["title"] == "Mine"
    assert connection.open_alerts[0]["cloud"] == {"synced": False}


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
    await until(lambda: bool(seen) and seen[-1] is False, "the disconnect to reach the listener")
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
    await until(
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
    await until(lambda: len(connection.open_alerts) == 1, "the alert to be added")

    # Somebody answered; the condition has not cleared, so it stays open.
    await station.push("alert.acknowledged", {**alert, "status": "acknowledged"})
    await until(
        lambda: connection.open_alerts[0]["status"] == "acknowledged",
        "the acknowledgement to be applied",
    )

    await station.push("alert.expired", {**alert, "status": "expired"})
    await until(lambda: connection.open_alerts == [], "the expiry to close the alert")


async def test_an_unknown_transition_does_not_kill_the_socket(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§9 makes new events additive: a newer station must not stop this one."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    await station.push("alert.snoozed", {"id": "la_new", "status": "snoozed"})
    await station.push("alert.triggered", _open_alert(station, "la_known", "Known"))

    await until(
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

    await until(lambda: _seeds(station) >= 2, "the re-seed after reconnecting")
    await until(lambda: connection.connected, "the socket to come back")
    assert [a["id"] for a in connection.open_alerts] == ["la_missed"]


async def test_the_shipped_stations_join_snapshot_is_applied_too(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A divergence from §4.6, so it is belt-and-braces rather than the route.

    Seed and snapshot are made to disagree on purpose. Left agreeing -- which
    is what a real station does -- this test would pass with the snapshot
    branch deleted, because the reconnect's `GET /v1/alerts` would deliver the
    same alert and nothing could tell the two paths apart.
    """
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    only_in_the_snapshot = {"id": "la_snap", "status": "triggered", "title": "Only pushed"}
    station.snapshot_alerts = [only_in_the_snapshot]
    await station.drop_sockets()

    await until(
        lambda: [a["id"] for a in connection.open_alerts] == ["la_snap"],
        "the join snapshot to be applied",
    )
    # And the seed really did not carry it -- otherwise the assertion above
    # proves nothing about the snapshot.
    assert station.alerts == {}


async def test_disconnected_while_the_socket_is_refused(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """Never show stale state: no socket means not connected, and it keeps trying."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    station.events_status = 503
    await station.drop_sockets()
    await until(lambda: not connection.connected, "the connection to be reported down")

    attempts = station.requests.count(("GET", "/v1/events"))
    await until(
        lambda: station.requests.count(("GET", "/v1/events")) > attempts,
        "another reconnect attempt",
    )
    assert connection.connected is False

    station.events_status = None
    await until(lambda: connection.connected, "the socket to come back on its own")


async def test_backoff_doubles_from_one_second_and_caps_at_sixty(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.6: jittered exponential backoff, 1 s to a 60 s cap.

    Read off the policy rather than timed: timing it would mean sitting through
    a minute of real backoff to learn what arithmetic already says.
    """
    connection = _bare_connection(hass, station)

    delays = [connection._next_backoff() for _ in range(10)]  # noqa: SLF001

    for delay, undelayed in zip(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 32.0] + [60.0] * 4, strict=True):
        # Jitter is a fraction of the delay, so the cap stays a cap and the
        # first retry never comes sooner than half a second.
        assert undelayed / 2 <= delay <= undelayed
    assert max(delays) <= BACKOFF_MAX


async def test_a_socket_that_drops_at_once_does_not_reset_the_backoff(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A station that accepts the upgrade and hangs up must still be backed off.

    Resetting on the handshake alone pinned the retry at ~1 s -- plus a full
    re-seed each time -- for as long as the station kept doing it, which is
    precisely the case the backoff exists for.
    """
    connection = _bare_connection(hass, station)

    # Six flaps, each connecting and dropping immediately.
    for _ in range(6):
        connection._async_on_connect()  # noqa: SLF001
        # The handshake must record when it happened -- without that the reset
        # below can never fire and this test would pass for the wrong reason.
        assert connection._connected_at is not None  # noqa: SLF001
        connection._next_backoff()  # noqa: SLF001

    assert connection._next_backoff() >= BACKOFF_MAX / 2  # noqa: SLF001


async def test_a_socket_that_held_resets_the_backoff(
    hass: HomeAssistant, station: FakeStation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection that worked means the next outage starts from 1 s again.

    The window is crossed by really waiting out a shortened `BACKOFF_MAX`
    rather than by writing `_connected_at` by hand: the point of the test is
    that `_async_on_connect` records the time at all, and a hand-written field
    would keep passing with that line deleted.
    """
    monkeypatch.setattr(connection_module, "BACKOFF_MAX", 0.05)
    connection = _bare_connection(hass, station)
    for _ in range(6):
        connection._next_backoff()  # noqa: SLF001

    connection._async_on_connect()  # noqa: SLF001
    await asyncio.sleep(0.06)

    assert connection._next_backoff() <= BACKOFF_START  # noqa: SLF001


async def test_a_flapping_station_is_backed_off_in_practice(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """The same thing end to end: accept, hang up, repeat -- and it slows down.

    `fast_backoff` shrinks `BACKOFF_MAX`, which is also the stability window,
    so a socket that is dropped the moment it opens never counts as one that
    worked and the delay climbs to the (shortened) cap.
    """
    entry = await _setup(hass, station)
    connection = _connection(entry)

    async def hang_up() -> None:
        while True:
            if station.sockets:
                await station.drop_sockets()
            await asyncio.sleep(0.005)

    hanging_up = asyncio.create_task(hang_up())
    try:
        await until(lambda: connection._failures >= 3, "the backoff to climb")  # noqa: SLF001
    finally:
        hanging_up.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hanging_up


# -- revocation -----------------------------------------------------------


async def test_a_revoked_token_starts_reauth_and_stops(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """§3.6: a `401` is reauth, never a reconnect loop."""
    entry = await _setup(hass, station)
    await _connected(entry)

    station.revoked = True
    await station.drop_sockets()

    await until(
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
    await until(
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

    await until(
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
    await until(lambda: station.sockets == [], "the station to see the socket close")


async def test_reload_leaves_one_socket(hass: HomeAssistant, station: FakeStation) -> None:
    """The old connection is gone before the new one opens."""
    entry = await _setup(hass, station)
    await _connected(entry)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _connected(entry)

    assert len(station.sockets) == 1


async def test_a_bug_in_the_loop_does_not_leave_the_entry_looking_connected(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The one state that must never happen (CLAUDE.md: never show stale state).

    Every failure the *station* can cause is handled by name; this stands in
    for one this file caused itself. It must be reported, it must not leave
    `connected` true with nobody listening, and it must not end the task --
    a station that pages people is worth retrying even when we are the broken
    end of it.
    """
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    exploded = asyncio.Event()

    async def explode() -> None:
        exploded.set()
        raise RuntimeError("a bug, not a station")

    connection._async_seed = explode  # type: ignore[method-assign]  # noqa: SLF001
    await station.drop_sockets()
    await asyncio.wait_for(exploded.wait(), _TIMEOUT)

    assert connection.connected is False
    assert "a bug, not a station" in caplog.text

    # Still trying: the task did not die with it.
    exploded.clear()
    await asyncio.wait_for(exploded.wait(), _TIMEOUT)

    # And it recovers once the bug stops happening.
    del connection._async_seed  # noqa: SLF001
    await _connected(entry)


async def test_a_listener_that_raises_does_not_take_the_connection_down(
    hass: HomeAssistant, station: FakeStation, caplog: pytest.LogCaptureFixture
) -> None:
    """Listeners are other platforms' entities; one of them is not the socket's problem."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    def bad() -> None:
        raise RuntimeError("a listener with a bug")

    notified: list[None] = []
    connection.async_add_listener(bad)
    connection.async_add_listener(lambda: notified.append(None))

    await station.push("alert.triggered", _open_alert(station, "la_guard", "Guarded"))

    await until(lambda: bool(notified), "the other listener to be notified anyway")
    assert connection.connected is True
    assert "a listener with a bug" in caplog.text


async def test_unload_still_works_after_a_bug_in_the_loop(
    hass: HomeAssistant, station: FakeStation, fast_backoff: None
) -> None:
    """An entry that cannot be removed is a worse failure than the one that caused it."""
    entry = await _setup(hass, station)
    connection = await _connected(entry)

    exploded = asyncio.Event()

    async def explode() -> None:
        exploded.set()
        raise RuntimeError("a bug, not a station")

    connection._async_seed = explode  # type: ignore[method-assign]  # noqa: SLF001
    await station.drop_sockets()
    await asyncio.wait_for(exploded.wait(), _TIMEOUT)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert connection.connected is False
