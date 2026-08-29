"""Client tests, driven against the fake station over a real socket."""

from __future__ import annotations

import contextlib
import logging

import aiohttp
import pytest

from custom_components.alertroster.api import (
    AlertRosterClient,
    CannotConnect,
    InvalidAlertId,
    InvalidAuth,
    InvalidCode,
    StationError,
)

from .conftest import FakeStation


@pytest.fixture
async def session() -> aiohttp.ClientSession:
    """A session for the client under test."""
    async with aiohttp.ClientSession() as client_session:
        yield client_session


def _client(
    session: aiohttp.ClientSession, station: FakeStation, token: str | None = None
) -> AlertRosterClient:
    """A client pointed at the fake station."""
    return AlertRosterClient(session, station.host, station.port, token)


# -- probing --------------------------------------------------------------


async def test_probe_treats_404_as_reachable(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """Today's station has no `/v1/discover`, and is still there."""
    assert station.discover_status == 404

    info = await _client(session, station).probe()

    assert info.name is None
    assert info.pairing_window_open is None
    assert ("GET", "/v1/discover") in station.requests


async def test_probe_reads_discover_once_the_station_ships_it(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """The same request starts carrying answers the day §5 item 1 lands."""
    station.discover_status = 200
    station.pairing_window_open = False

    info = await _client(session, station).probe()

    assert info.name == "studio"
    assert info.version == "1.4.0"
    # Distinguishable from "the station did not say", which is `None`.
    assert info.pairing_window_open is False


async def test_probe_reports_an_unreachable_station(
    session: aiohttp.ClientSession, socket_enabled: None, unused_tcp_port: int
) -> None:
    """A port with nothing behind it is the one thing that means unreachable.

    Which is also what a station with *Accept sources from the LAN* switched
    off looks like (§4.1) -- the case `cannot_connect`'s wording is aimed at.
    """
    with pytest.raises(CannotConnect):
        await AlertRosterClient(session, "127.0.0.1", unused_tcp_port).probe()


@pytest.mark.parametrize("host", ["", "http://10.0.0.4", "10.0.0.4:4747", "bad host"])
async def test_probe_rejects_a_host_that_cannot_form_a_url(
    session: aiohttp.ClientSession, station: FakeStation, host: str
) -> None:
    """A pasted URL must not escape as a `ValueError` from inside a request."""
    with pytest.raises(CannotConnect):
        await AlertRosterClient(session, host, station.port).probe()


# -- pairing --------------------------------------------------------------


async def test_pair_returns_a_token_and_the_station_s_id_spelling(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """The reply is `{token, id, kind}`; both spellings of the id are taken."""
    result = await _client(session, station).pair(station.valid_code, "Home Assistant")

    assert result.token.startswith("lat_")
    assert result.source_id.startswith("src_")
    assert result.kind == "source"


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("wrong code", "00000000"),
        ("window closed", None),
        ("no pairing authority", None),
    ],
)
async def test_pair_refusals_are_indistinguishable(
    session: aiohttp.ClientSession, station: FakeStation, setup: str, code: str | None
) -> None:
    """§8: all three refusals are the same `403`, so a guesser learns nothing."""
    if setup == "window closed":
        station.pairing_window_open = False
    elif setup == "no pairing authority":
        station.has_pairing_authority = False

    with pytest.raises(InvalidCode):
        await _client(session, station).pair(code or station.valid_code, "Home Assistant")


async def test_pair_never_puts_the_token_in_a_log_line(
    session: aiohttp.ClientSession,
    station: FakeStation,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REQUIREMENTS.md §6: the credential reaches no log, message or repr."""
    with caplog.at_level(logging.DEBUG):
        result = await _client(session, station).pair(station.valid_code, "Home Assistant")

    assert result.token not in caplog.text
    assert result.token not in repr(result)
    assert result.token not in repr(_client(session, station, result.token))


# -- alerts ---------------------------------------------------------------


async def test_a_token_is_required(session: aiohttp.ClientSession, station: FakeStation) -> None:
    """An unpaired client cannot reach an authenticated path at all."""
    with pytest.raises(InvalidAuth):
        await _client(session, station).list_alerts()


async def test_revoked_token_raises_invalid_auth(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """`401` is what a token revoked on the station looks like from here."""
    client = _client(session, station, station.issue_token())
    station.revoked = True

    with pytest.raises(InvalidAuth):
        await client.list_alerts()


async def test_create_list_and_resolve_an_alert(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """The round trip an automation makes."""
    client = _client(session, station, station.issue_token())

    alert = await client.create_alert(title="Garage door open", urgency="high")
    assert alert["status"] == "triggered"
    assert [a["id"] for a in await client.list_alerts()] == [alert["id"]]

    resolved = await client.resolve_alert(alert["id"])
    assert resolved["status"] == "resolved"
    assert await client.list_alerts() == []


async def test_repeat_with_a_live_dedup_key_is_not_an_update(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """Verified on a real station: the second call's fields are ignored."""
    client = _client(session, station, station.issue_token())

    first = await client.create_alert(title="Garage door open", dedup_key="garage")
    second = await client.create_alert(title="Something else entirely", dedup_key="garage")

    assert second["id"] == first["id"]
    assert second["title"] == "Garage door open"


async def test_ack_timeout_is_passed_through_untouched(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """The integration holds no opinion about expiry (REQUIREMENTS.md §2)."""
    client = _client(session, station, station.issue_token())

    passed = await client.create_alert(title="A", ack_timeout_seconds=45)
    defaulted = await client.create_alert(title="B")

    assert passed["ack_timeout_seconds"] == 45
    # Omitted rather than sent as null, so the station applies its own default.
    assert defaulted["ack_timeout_seconds"] == 120


async def test_unknown_alert_surfaces_the_station_s_status(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """A real HTTP failure arrives as `StationError`, carrying the status."""
    client = _client(session, station, station.issue_token())

    with pytest.raises(StationError) as err:
        await client.resolve_alert("la_nope")

    assert err.value.status == 404
    assert err.value.error == "not_found"


@pytest.mark.parametrize("alert_id", ["", "..", "../../admin", "a/b", "a\\b"])
async def test_alert_ids_that_would_redirect_the_request_are_refused(
    session: aiohttp.ClientSession, station: FakeStation, alert_id: str
) -> None:
    """Alert ids come from automations, so they are user input."""
    client = _client(session, station, station.issue_token())

    with pytest.raises(InvalidAlertId):
        await client.resolve_alert(alert_id)

    # The point of the check: no authenticated request was aimed anywhere.
    assert not [r for r in station.requests if r[0] == "POST"]


# -- events ---------------------------------------------------------------


async def test_events_yields_the_join_snapshot_then_transitions(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """The shipped service sends a snapshot on join, then transitions."""
    client = _client(session, station, station.issue_token())
    alert = {"id": "la_1", "status": "expired", "title": "Nobody answered"}

    seen = []
    async with contextlib.aclosing(client.events()) as events:
        async for event in events:
            seen.append(event)
            if event.event == "snapshot":
                await station.push("alert.expired", alert)
            else:
                # Ended by the station rather than by breaking out: abandoning
                # the iterator part-way leaves aiohttp's heartbeat timer
                # uncancelled, which is AHA-34 and is the events task's problem
                # to solve, not something to assert here.
                await station.drop_sockets()

    assert seen[0].event == "snapshot"
    assert seen[0].alerts == []
    assert seen[1].event == "alert.expired"
    assert seen[1].alert == alert


async def test_events_rejects_a_revoked_token_at_the_handshake(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """A `401` on the upgrade must be reauth, never a reconnect loop."""
    client = _client(session, station, station.issue_token())
    station.revoked = True

    with pytest.raises(InvalidAuth):
        async with contextlib.aclosing(client.events()) as events:
            async for _event in events:
                pass


async def test_events_ends_when_the_station_closes_the_socket(
    session: aiohttp.ClientSession, station: FakeStation
) -> None:
    """A clean close ends the iterator; the caller treats that as reconnect."""
    client = _client(session, station, station.issue_token())

    seen = []
    async with contextlib.aclosing(client.events()) as events:
        async for event in events:
            seen.append(event)
            await station.drop_sockets()

    assert [e.event for e in seen] == ["snapshot"]
