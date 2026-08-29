"""Config flow tests (AHA-10), against the fake station over a real socket.

Two boxes on AHA-10 are not ticked here because the code they describe does not
exist yet, and a test that passes without exercising anything is worse than a
missing one: zeroconf discovery is AHA-6 and the reauth-on-401 flow is AHA-9.
Both get their tests with the step they test.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.config_flow import _is_usable_host
from custom_components.alertroster.const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DEFAULT_PORT,
    DOMAIN,
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
