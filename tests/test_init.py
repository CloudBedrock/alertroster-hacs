"""Entry lifecycle tests (AHA-25): what removing a config entry does.

REQUIREMENTS.md §3.7 -- deleting the entry unpairs Home Assistant from the
station, and never at the cost of the entry itself being deletable.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DOMAIN,
)

from .conftest import FakeStation


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


async def _remove(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Delete the entry, the way the user does in Devices & services."""
    assert await hass.config_entries.async_remove(entry.entry_id) == {"require_restart": False}
    await hass.async_block_till_done()


async def test_removing_the_entry_revokes_the_token(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.7: the row goes off the station, rather than being left behind."""
    entry = await _setup(hass, station)

    await _remove(hass, entry)

    assert ("DELETE", "/v1/sources/self") in station.requests
    assert not station.paired
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_the_entry_goes_even_when_the_station_is_unreachable(
    hass: HomeAssistant, station: FakeStation, caplog: pytest.LogCaptureFixture
) -> None:
    """A station that is off must not leave an entry that cannot be deleted.

    The warning is part of the contract, not noise: it is the only place the
    user is told the station still has a row that Home Assistant could not
    remove, and §3.7 sends them to the Pairing screen for it.
    """
    entry = await _setup(hass, station)
    await station.stop()

    with caplog.at_level(logging.WARNING):
        await _remove(hass, entry)

    assert hass.config_entries.async_entries(DOMAIN) == []
    assert "Pairing screen" in caplog.text


async def test_a_station_that_revoked_first_is_not_a_warning(
    hass: HomeAssistant, station: FakeStation, caplog: pytest.LogCaptureFixture
) -> None:
    """A `401` here means the row is already gone -- the wanted outcome.

    Someone who deletes the row on the station and then the entry in Home
    Assistant has done nothing wrong, and should not be told to go and clear
    up a row that is not there.
    """
    entry = await _setup(hass, station)
    station.revoked = True

    with caplog.at_level(logging.DEBUG):
        await _remove(hass, entry)

    # Attempted, not skipped: "no warning" on its own would still be true of a
    # removal that never called the station at all.
    assert ("DELETE", "/v1/sources/self") in station.requests
    assert "had already revoked this pairing" in caplog.text
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert "Pairing screen" not in caplog.text


async def test_removal_never_logs_the_token(
    hass: HomeAssistant, station: FakeStation, caplog: pytest.LogCaptureFixture
) -> None:
    """REQUIREMENTS.md §6, on the one path that holds the token to use it.

    Driven through the failing case, because that is the one that logs: the
    warning names the station and the error, and the error is built from an
    address and a status, never from the credential.
    """
    entry = await _setup(hass, station)
    token = entry.data[CONF_TOKEN]
    await station.stop()

    with caplog.at_level(logging.DEBUG):
        await _remove(hass, entry)

    assert "Pairing screen" in caplog.text
    assert token not in caplog.text


async def test_an_entry_that_never_loaded_is_still_removable(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Removal reaches for its own client, so there is none to be missing.

    An entry in a failed state has no `runtime_data`; taking the client from
    there instead would turn deleting it into an `AttributeError`.
    """
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

    await _remove(hass, entry)

    assert ("DELETE", "/v1/sources/self") in station.requests
    assert hass.config_entries.async_entries(DOMAIN) == []
