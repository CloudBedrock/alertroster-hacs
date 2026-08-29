"""Config flow tests (AHA-10, AHA-6), against the fake station over a real socket.

One box on AHA-10 is still not ticked here because the code it describes does
not exist yet, and a test that passes without exercising anything is worse than
a missing one: the reauth-on-401 flow is AHA-9 and gets its tests with the step
it tests. Zeroconf discovery arrived with AHA-6 and is covered below.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from ipaddress import ip_address
from typing import Any
from unittest.mock import patch

import pytest
import pytest_socket
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.config_flow import (
    AlertRosterConfigFlow,
    _announced_addresses,
    _is_usable_host,
)
from custom_components.alertroster.const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DEFAULT_PORT,
    DOMAIN,
    ZEROCONF_TYPE,
)

from .conftest import FakeStation


async def _start(hass: HomeAssistant) -> ConfigFlowResult:
    """Open the flow the way Add Integration does."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _submit(hass: HomeAssistant, flow_id: str, **fields: Any) -> ConfigFlowResult:
    """Fill in the form on screen."""
    return await hass.config_entries.flow.async_configure(flow_id, fields)


def _announcement(
    host: str,
    port: int,
    name: str | None = "studio",
    version: str | None = "1",
    extra_addresses: list[str] | None = None,
) -> ZeroconfServiceInfo:
    """What the station puts on the wire, shaped the way §4.1 describes it.

    Read off a real `avahi-browse` of the `studio` and `om` stations: TXT is
    exactly `v=1` and `name=<display name>`, and the announcement carries one
    record per interface.
    """
    properties: dict[str, Any] = {}
    if version is not None:
        properties["v"] = version
    if name is not None:
        properties["name"] = name
    addresses = [ip_address(host), *(ip_address(a) for a in extra_addresses or [])]
    return ZeroconfServiceInfo(
        ip_address=addresses[0],
        ip_addresses=addresses,
        port=port,
        hostname=f"{name or 'station'}.local.",
        type=ZEROCONF_TYPE,
        name=f"{name or 'station'}.{ZEROCONF_TYPE}",
        properties=properties,
    )


async def _discover(hass: HomeAssistant, info: ZeroconfServiceInfo) -> ConfigFlowResult:
    """Hand the announcement to Home Assistant the way zeroconf does."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )


# -- the happy path -------------------------------------------------------


async def test_manual_host_and_code_creates_an_entry(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """REQUIREMENTS.md §3.2: host and port, then the 8-digit code."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    # The step's title is "Pair with {name}", so the placeholder must be there
    # or Home Assistant cannot render the step at all.
    assert result["description_placeholders"] == {"name": station.host}

    result = await _submit(hass, result["flow_id"], code=station.valid_code)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == station.host
    assert result["data"][CONF_PORT] == station.port
    assert result["data"][CONF_SOURCE_ID] == station.source_id
    assert result["data"][CONF_TOKEN].startswith("lat_")
    # Today's station cannot tell us its name; the entry is titled by address.
    assert result["data"][CONF_STATION_NAME] is None
    assert result["title"] == station.host


async def test_the_port_defaults_to_4747(hass: HomeAssistant) -> None:
    """The default is offered, because most stations are on it -- but not all."""
    result = await _start(hass)

    schema = result["data_schema"]
    assert schema is not None
    assert schema({"host": "studio.local"})["port"] == DEFAULT_PORT


async def test_station_name_titles_the_entry_once_discover_ships(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The probe's answer is what names the station, the day §5 item 1 lands."""
    station.discover_status = 200

    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)
    assert result["description_placeholders"] == {"name": "studio"}

    result = await _submit(hass, result["flow_id"], code=station.valid_code)
    assert result["title"] == "studio"
    assert result["data"][CONF_STATION_NAME] == "studio"


# -- the user step --------------------------------------------------------


async def test_unreachable_station_says_cannot_connect(
    hass: HomeAssistant, socket_enabled: None, unused_tcp_port: int
) -> None:
    """A station with LAN listening off looks exactly like an absent one."""
    result = await _start(hass)

    result = await _submit(hass, result["flow_id"], host="127.0.0.1", port=unused_tcp_port)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_cannot_connect_is_recoverable(
    hass: HomeAssistant, station: FakeStation, unused_tcp_port: int
) -> None:
    """The user fixes the address and carries on in the same flow."""
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host="127.0.0.1", port=unused_tcp_port)
    assert result["errors"] == {"base": "cannot_connect"}

    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)

    assert result["step_id"] == "pair"


@pytest.mark.parametrize("host", ["", "   ", "http://10.0.0.4", "10.0.0.4:4747", "not a host"])
async def test_a_pasted_url_is_not_cannot_connect(
    hass: HomeAssistant, station: FakeStation, host: str
) -> None:
    """Its own error: "check Accept sources from the LAN" would misdirect."""
    result = await _start(hass)

    result = await _submit(hass, result["flow_id"], host=host, port=station.port)

    assert result["errors"] == {"base": "invalid_host"}
    # Nothing was asked of the station, because nothing could be.
    assert station.requests == []


@pytest.mark.parametrize(
    ("host", "usable"),
    [
        ("10.0.0.4", True),
        ("studio.local", True),
        # yarl brackets an IPv6 literal; the hand-rolled "a host may not
        # contain a colon" rule this gate is not would reject it.
        ("fe80::1", True),
        ("", False),
        ("http://10.0.0.4", False),
        ("10.0.0.4:4747", False),
        ("not a host", False),
    ],
)
def test_the_host_gate_accepts_anything_that_can_address_a_station(host: str, usable: bool) -> None:
    """Asserted on the gate itself rather than by dialling.

    Reaching `studio.local` would mean a DNS lookup, which is exactly the
    network REQUIREMENTS.md §6 forbids a test from touching -- and what the
    gate decides is a question about the string, not about who answers.
    """
    assert _is_usable_host(host, DEFAULT_PORT) is usable


async def test_the_same_address_twice_is_refused(hass: HomeAssistant, station: FakeStation) -> None:
    """A second entry for one station helps nobody."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="src_something_else",
        data={CONF_HOST: station.host, CONF_PORT: station.port},
    ).add_to_hass(hass)

    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_station_already_paired_at_another_address_is_refused(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The `source_id` catches the station that moved, which host:port cannot."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=station.source_id,
        data={CONF_HOST: "10.0.0.9", CONF_PORT: DEFAULT_PORT},
    ).add_to_hass(hass)

    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)
    assert result["step_id"] == "pair"

    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# -- the pair step --------------------------------------------------------


async def test_a_wrong_code_can_be_retried(hass: HomeAssistant, station: FakeStation) -> None:
    """§3.2: the user may retry, and the retry lands in the same flow."""
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)

    result = await _submit(hass, result["flow_id"], code="00000000")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert result["errors"] == {"base": "invalid_code"}

    result = await _submit(hass, result["flow_id"], code=station.valid_code)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_third_wrong_code_says_the_window_has_closed(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Three wrong codes close the window, so "try again" stops being true."""
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)

    for _ in range(2):
        result = await _submit(hass, result["flow_id"], code="00000000")
        assert result["errors"] == {"base": "invalid_code"}

    result = await _submit(hass, result["flow_id"], code="00000000")

    assert result["errors"] == {"base": "window_exhausted"}


async def test_a_closed_window_is_currently_indistinguishable(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """AHA-10 asks for `window_closed`, and it is not reachable yet.

    The station answers a closed window with the same bodiless `403` as a wrong
    code (`api.py`'s `InvalidCode`), so the flow cannot tell them apart and
    honestly says `invalid_code`. This test exists to fail the day the station
    starts distinguishing them -- that is when `window_closed` becomes
    reachable and REQUIREMENTS.md §5 item 1 can be ticked off.
    """
    station.pairing_window_open = False

    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)
    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["errors"] == {"base": "invalid_code"}


async def test_a_station_that_goes_away_mid_flow_says_cannot_connect(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The address was good at the probe and is not any more."""
    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)

    # Take the station off the air rather than reaching into the flow's own
    # attributes: a rename inside Home Assistant should not break this test,
    # and a restarting station is the thing actually being described.
    await station.stop()

    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["errors"] == {"base": "cannot_connect"}


# -- the credential -------------------------------------------------------


async def test_the_token_reaches_no_log_record(
    hass: HomeAssistant, station: FakeStation, caplog: pytest.LogCaptureFixture
) -> None:
    """REQUIREMENTS.md §6, and the reason the rig logs this at debug."""
    with caplog.at_level(logging.DEBUG):
        result = await _start(hass)
        result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)
        result = await _submit(hass, result["flow_id"], code=station.valid_code)

    token = result["data"][CONF_TOKEN]
    assert token.startswith("lat_")
    assert token not in caplog.text
    # The one debug line the flow does emit names the source, and says so.
    assert station.source_id in caplog.text


# -- what the station's Pairing list will say ------------------------------


async def test_the_pair_request_sends_this_instance_s_name(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """AHA-32: two Home Assistants on one station must be tellable apart.

    The station's Pairing list shows this name, so sending the literal "Home
    Assistant" would leave every instance looking identical there.
    """
    hass.config.location_name = "Beach House"

    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)
    await _submit(hass, result["flow_id"], code=station.valid_code)

    assert station.paired_names == ["Beach House"]


async def test_an_unnamed_instance_still_pairs_as_something(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """An empty instance name must not become an empty row on the station."""
    hass.config.location_name = "   "

    result = await _start(hass)
    result = await _submit(hass, result["flow_id"], host=station.host, port=station.port)
    await _submit(hass, result["flow_id"], code=station.valid_code)

    assert station.paired_names == ["Home Assistant"]


# -- reauth (AHA-9) -------------------------------------------------------


def _paired(hass: HomeAssistant, station: FakeStation) -> MockConfigEntry:
    """An entry as the flow leaves it, holding a token the station has forgotten."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="studio",
        unique_id=station.source_id,
        data={
            CONF_HOST: station.host,
            CONF_PORT: station.port,
            CONF_TOKEN: "lat_the_one_that_was_revoked",
            CONF_SOURCE_ID: station.source_id,
            CONF_STATION_NAME: None,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> ConfigFlowResult:
    """Start reauth the way a `401` does (§3.6)."""
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    return await hass.config_entries.flow.async_configure(flows[0]["flow_id"], None)


async def test_reauth_replaces_the_token_on_the_existing_entry(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """AHA-9: pair again, same entry, no second one."""
    entry = _paired(hass, station)

    result = await _start_reauth(hass, entry)
    assert result["type"] is FlowResultType.FORM
    # Its own step, because "this station no longer recognises Home Assistant"
    # is not something the ordinary pair step has any reason to say.
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"] == {"name": station.host}

    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_TOKEN] != "lat_the_one_that_was_revoked"
    assert entry.data[CONF_TOKEN] in station.tokens


async def test_reauth_never_asks_for_the_address_again(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """There is no host field, so reauth cannot repoint an entry at another station."""
    entry = _paired(hass, station)

    result = await _start_reauth(hass, entry)

    schema = result["data_schema"]
    assert schema is not None
    assert set(schema.schema) == {"code"}
    # The address it will POST to is the entry's, untouched.
    assert entry.data[CONF_HOST] == station.host
    assert entry.data[CONF_PORT] == station.port


async def test_reauth_follows_the_station_to_its_new_source_id(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§6.2 ties a token to a source row, and pairing again mints a new one.

    Keeping the old `source_id` as the unique id would leave the entry
    identified by a row the station has deleted -- so the same station could be
    added a second time under the id it now answers to.
    """
    entry = _paired(hass, station)
    station.source_id = "src_a_brand_new_row"

    result = await _start_reauth(hass, entry)
    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == "src_a_brand_new_row"
    assert entry.data[CONF_SOURCE_ID] == "src_a_brand_new_row"


async def test_a_wrong_code_during_reauth_stays_on_the_reauth_step(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The retry lands back where it was, not on the add-a-station form."""
    entry = _paired(hass, station)

    result = await _start_reauth(hass, entry)
    result = await _submit(hass, result["flow_id"], code="00000000")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_code"}

    result = await _submit(hass, result["flow_id"], code=station.valid_code)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_against_a_station_that_is_away_says_cannot_connect(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Recoverable: the station comes back and the same flow finishes."""
    entry = _paired(hass, station)
    result = await _start_reauth(hass, entry)
    await station.stop()

    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_replacement_token_reaches_no_log_record(
    hass: HomeAssistant, station: FakeStation, caplog: pytest.LogCaptureFixture
) -> None:
    """The rule that holds for pairing holds for re-pairing (§4.1)."""
    entry = _paired(hass, station)

    with caplog.at_level(logging.DEBUG):
        result = await _start_reauth(hass, entry)
        await _submit(hass, result["flow_id"], code=station.valid_code)

    token = entry.data[CONF_TOKEN]
    assert token.startswith("lat_")
    assert token not in caplog.text


# -- discovery (AHA-6) ----------------------------------------------------


def _probe(answers: bool) -> Any:
    """Say what the reachability probe would have found, instead of finding out.

    These tests are about which entry an announcement is matched to and what is
    written back, not about whether an address answers -- that has its own
    tests against the real fake station. It has to be stubbed because
    `pytest-socket` allows only loopback, so an entry stored at `10.0.0.4` or
    `studio.local` cannot be probed for real, and what the block raises is not
    the `CannotConnect` a dead address gives.
    """
    return patch.object(
        AlertRosterConfigFlow,
        "_async_reachable_address",
        autospec=True,
        side_effect=lambda _self, addresses, _port: addresses[0] if answers else None,
    )


async def test_a_discovered_station_pairs_and_is_named_by_its_txt_record(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.1: the TXT `name` titles the flow, and pairing still keys on `source_id`."""
    result = await _discover(hass, _announcement(station.host, station.port))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    # The card and the step are both titled from the announcement, not from the
    # address, which is the whole point of reading the TXT record.
    assert result["description_placeholders"] == {"name": "studio"}

    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "studio"
    assert result["data"][CONF_HOST] == station.host
    assert result["data"][CONF_PORT] == station.port
    assert result["data"][CONF_STATION_NAME] == "studio"
    # §3.1: the entry's identity is the pairing, not the announcement.
    assert result["data"][CONF_SOURCE_ID] == station.source_id
    assert hass.config_entries.async_entries(DOMAIN)[0].unique_id == station.source_id


async def test_a_repeat_announcement_does_not_open_a_second_card(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.1: one station on screen once, however often it announces.

    The second announcement deliberately carries a *different* address, which
    is what a station re-announcing on another interface looks like and what a
    host-and-port key would have failed to recognise.
    """
    first = await _discover(hass, _announcement(station.host, station.port))
    assert first["step_id"] == "pair"

    again = await _discover(
        hass, _announcement("10.51.1.215", station.port, extra_addresses=[station.host])
    )

    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_in_progress"
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_rediscovering_a_paired_station_is_ignored(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.1: a station already paired does not come back as something to add."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=station.source_id,
        data={
            CONF_HOST: station.host,
            CONF_PORT: station.port,
            CONF_STATION_NAME: "studio",
        },
    ).add_to_hass(hass)

    result = await _discover(hass, _announcement(station.host, station.port))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_paired_station_that_moved_has_its_address_updated(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.1: the entry follows the station, rather than pointing where it was."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=station.source_id,
        data={
            CONF_HOST: "10.0.0.9",
            CONF_PORT: DEFAULT_PORT,
            CONF_STATION_NAME: "studio",
        },
    )
    entry.add_to_hass(hass)

    result = await _discover(hass, _announcement(station.host, station.port))
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Matched by name -- neither the host nor the port it was stored with is
    # the one that just announced.
    assert entry.data[CONF_HOST] == station.host
    assert entry.data[CONF_PORT] == station.port


async def test_a_station_added_by_hand_is_matched_by_address_and_gains_its_name(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """An entry from the manual step has no name stored, so the first
    announcement has only the address to recognise it by -- and backfills the
    name, so the next move can be matched the better way."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=station.source_id,
        data={CONF_HOST: "10.0.0.4", CONF_PORT: station.port, CONF_STATION_NAME: None},
    )
    entry.add_to_hass(hass)

    with _probe(answers=True):
        result = await _discover(
            hass, _announcement(station.host, station.port, extra_addresses=["10.0.0.4"])
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_STATION_NAME] == "studio"


@pytest.mark.parametrize(
    ("version", "name"),
    [
        ("2", "studio"),
        (None, "studio"),
        ("1", None),
        ("1", "   "),
    ],
    ids=["a newer announcement", "no version", "no name", "a blank name"],
)
async def test_an_announcement_this_version_cannot_read_is_refused(
    hass: HomeAssistant, station: FakeStation, version: str | None, name: str | None
) -> None:
    """§4.1 promises `v=1` and a name. Anything else is left to the manual step."""
    result = await _discover(
        hass, _announcement(station.host, station.port, name=name, version=version)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_version"


async def test_a_station_that_announces_but_does_not_answer_is_refused(
    hass: HomeAssistant, station: FakeStation, unused_tcp_port: int
) -> None:
    """Announcing is not answering: the port is real, nothing is behind it."""
    result = await _discover(hass, _announcement(station.host, unused_tcp_port))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


@pytest.fixture
def dead_address() -> Iterator[str]:
    """A loopback address with nothing bound to it, for the walk-past test.

    `pytest-socket` allows only `127.0.0.1`, and what it raises for anything
    else is a `SocketConnectBlockedError` rather than the refusal a real dead
    address produces -- which would escape `CannotConnect` and prove nothing.
    Widening the allowance by one more loopback address keeps the no-network
    rule intact: `127.0.0.2` is still this machine.
    """
    pytest_socket.socket_allow_hosts(["127.0.0.1", "127.0.0.2"])
    try:
        yield "127.0.0.2"
    finally:
        pytest_socket.socket_allow_hosts(["127.0.0.1"])


async def test_the_first_announced_address_that_answers_is_the_one_used(
    hass: HomeAssistant, station: FakeStation, dead_address: str
) -> None:
    """A station announces on every interface; most of them are not reachable.

    The dead address stands in for the docker and libvirt bridge addresses a
    real station announces -- reachable from the station, not from here.
    """
    result = await _discover(
        hass,
        _announcement(dead_address, station.port, extra_addresses=[station.host]),
    )
    assert result["step_id"] == "pair"

    result = await _submit(hass, result["flow_id"], code=station.valid_code)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == station.host


@pytest.mark.parametrize(
    ("leads", "announced", "expected"),
    [
        ("10.0.0.4", ["10.0.0.4"], ["10.0.0.4"]),
        # Loopback is kept but demoted: on another machine it is Home
        # Assistant itself, not the station.
        ("127.0.0.1", ["127.0.0.1", "10.0.0.4"], ["10.0.0.4", "127.0.0.1"]),
        # `fe80::` needs a scope id the announcement does not carry.
        ("fe80::1", ["fe80::1", "10.0.0.4"], ["10.0.0.4"]),
        ("10.0.0.4", ["10.0.0.4", "10.0.0.4"], ["10.0.0.4"]),
        ("fe80::1", ["fe80::1"], []),
        # The address Home Assistant already settled on goes first, wherever
        # it happens to sit in the announcement.
        ("10.0.0.5", ["10.0.0.4", "10.0.0.5"], ["10.0.0.5", "10.0.0.4"]),
    ],
    ids=[
        "one address",
        "loopback last",
        "link-local dropped",
        "deduplicated",
        "nothing usable",
        "the chosen address leads",
    ],
)
def test_which_announced_addresses_are_worth_trying(
    leads: str, announced: list[str], expected: list[str]
) -> None:
    """The ordering `_async_reachable_address` then walks."""
    info = _announcement("10.0.0.4", DEFAULT_PORT)
    info.ip_address = ip_address(leads)
    info.ip_addresses = [ip_address(a) for a in announced]

    assert _announced_addresses(info) == expected


async def test_an_ignored_station_stays_ignored(hass: HomeAssistant, station: FakeStation) -> None:
    """Ignore is keyed on the name, which is the one entry with no data at all.

    Without the unique-id check the discovery card would come back every time
    the station announced, which is the opposite of what Ignore means.
    """
    result = await _discover(hass, _announcement(station.host, station.port))
    assert result["step_id"] == "pair"

    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_IGNORE},
        data={"unique_id": "studio", "title": "studio"},
    )
    await hass.async_block_till_done()

    again = await _discover(hass, _announcement(station.host, station.port))

    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"


async def test_a_station_paired_by_hostname_is_not_offered_twice(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The manual step invites `studio.local`, so an announcement has to know it.

    A missed match here is not a redundant card: pairing through it mints a
    second `source_id`, and a second `source_id` is a second entry and a second
    events socket for one station, which no unique id would catch.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=station.source_id,
        data={CONF_HOST: "studio.local", CONF_PORT: station.port, CONF_STATION_NAME: None},
    )
    entry.add_to_hass(hass)

    with _probe(answers=True):
        result = await _discover(hass, _announcement(station.host, station.port))
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_an_unrelated_station_announcing_loopback_is_not_mistaken_for_this_one(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """`127.0.0.1` says nothing about *which* station announced it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="src_some_other_station",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: station.port, CONF_STATION_NAME: None},
    )
    entry.add_to_hass(hass)

    result = await _discover(
        hass, _announcement(station.host, station.port, name="a different station")
    )

    # Offered as its own station rather than swallowed into the entry above.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert entry.data[CONF_STATION_NAME] is None


async def test_a_paired_station_that_still_answers_keeps_the_address_it_has(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A station re-announces on a timer, and `ip_addresses` reorders between
    announcements. Repointing the entry at whichever address answered first
    would hand it a different host every few minutes, and every change is a
    reload -- the events socket dropped and rebuilt for nothing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=station.source_id,
        data={
            CONF_HOST: "studio.local",
            CONF_PORT: station.port,
            CONF_STATION_NAME: "studio",
        },
    )
    entry.add_to_hass(hass)

    with _probe(answers=True):
        result = await _discover(hass, _announcement(station.host, station.port))
    await hass.async_block_till_done()

    assert result["reason"] == "already_configured"
    # Left exactly as the user typed it, not rewritten to today's IP address.
    assert entry.data[CONF_HOST] == "studio.local"


async def test_a_paired_station_that_announces_but_does_not_answer_keeps_its_address(
    hass: HomeAssistant, station: FakeStation, unused_tcp_port: int
) -> None:
    """Nothing answered, so there is no better address to write down."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=station.source_id,
        data={
            CONF_HOST: "10.0.0.9",
            CONF_PORT: unused_tcp_port,
            CONF_STATION_NAME: "studio",
        },
    )
    entry.add_to_hass(hass)

    with _probe(answers=False):
        result = await _discover(hass, _announcement(station.host, unused_tcp_port))
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "10.0.0.9"
    assert entry.data[CONF_PORT] == unused_tcp_port
