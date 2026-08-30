"""Diagnostics tests (AHA-26).

Driven through `get_diagnostics_for_config_entry`, which fetches the real
`/api/diagnostics/config_entry/<id>` endpoint and decodes the real JSON. That
matters more here than in most tests: the assertion that has to hold is about
the bytes a user downloads and pastes into a public issue, and Home Assistant's
serializer renders anything it cannot encode with `repr()` rather than raising
-- so a leak introduced that way would never show up in a test that inspected
the returned dict directly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.alertroster.const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    CONF_STATION_VERSION,
    DOMAIN,
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
    hass: HomeAssistant,
    station: FakeStation,
    *,
    connect: bool = True,
    version: str | None = "1.4.2",
) -> MockConfigEntry:
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
            CONF_STATION_NAME: "studio",
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


def _alert(detail: str = "Nobody at the desk") -> dict[str, Any]:
    """A complete alert, shaped as §4.3 describes one."""
    return {
        "id": "alt_1",
        "status": "triggered",
        "title": "Front door",
        "detail": detail,
        "urgency": "high",
        "dedup_key": None,
        "ack_timeout_seconds": 120,
        "cloud": {"synced": True, "id": "cl_9"},
    }


# -- the credential --------------------------------------------------------


async def test_the_token_is_absent_from_the_download(
    hass: HomeAssistant, station: FakeStation, hass_client: ClientSessionGenerator
) -> None:
    """REQUIREMENTS.md §6, on the file most likely to be posted in public."""
    entry = await _setup(hass, station)
    token = entry.data[CONF_TOKEN]

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert token not in json.dumps(diagnostics)
    # The negative above passes just as well against an empty payload, so this
    # says the thing that was withheld was withheld from a real dump.
    assert diagnostics["entry"]["source_id"] == station.source_id


async def test_a_token_pasted_into_an_alert_is_scrubbed_too(
    hass: HomeAssistant, station: FakeStation, hass_client: ClientSessionGenerator
) -> None:
    """The case key-based redaction structurally cannot see.

    An alert's `detail` is whatever an automation wrote, so a token can reach
    the payload as a *value* under a perfectly innocent key. Nothing in this
    integration puts it there; the requirement is "never", which is a claim
    about what arrives, not only about what this code sends.
    """
    entry = await _setup(hass, station)
    token = entry.data[CONF_TOKEN]
    station.alerts = {"alt_1": _alert(detail=f"pairing failed with {token}, retrying")}
    await station.push("alert.triggered", station.alerts["alt_1"])
    connection = entry.runtime_data.connection
    await _until(lambda: connection.open_alert_count == 1, "the alert to be applied")

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    dumped = json.dumps(diagnostics)
    assert token not in dumped
    # The alert itself still came through -- scrubbing the credential must not
    # cost the report the thing it was reporting.
    assert "pairing failed with" in dumped
    assert "**REDACTED**" in dumped


# -- what it actually says -------------------------------------------------


async def test_diagnostics_describe_the_entry_and_its_connection(
    hass: HomeAssistant, station: FakeStation, hass_client: ClientSessionGenerator
) -> None:
    """The three sections a bug report needs, on a healthy entry."""
    entry = await _setup(hass, station)
    station.alerts = {"alt_1": _alert()}
    await station.push("alert.triggered", station.alerts["alt_1"])
    connection = entry.runtime_data.connection
    await _until(lambda: connection.open_alert_count == 1, "the alert to be applied")

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"] == {
        "title": "studio",
        "entry_id": entry.entry_id,
        "state": "loaded",
        "host": station.host,
        "port": station.port,
        "source_id": station.source_id,
        "station_name": "studio",
        "station_version": "1.4.2",
    }
    assert diagnostics["connection"]["connected"] is True
    assert diagnostics["connection"]["open_alert_count"] == 1
    assert diagnostics["connection"]["task_running"] is True
    assert [a["id"] for a in diagnostics["open_alerts"]] == ["alt_1"]
    # Passed through whole, nesting included: §2 leaves `cloud` untouched and a
    # report that flattened it would be answering a different question.
    assert diagnostics["open_alerts"][0]["cloud"] == {"synced": True, "id": "cl_9"}


async def test_diagnostics_report_a_station_that_went_away(
    hass: HomeAssistant, station: FakeStation, hass_client: ClientSessionGenerator
) -> None:
    """The report this exists for: entities unavailable, and why.

    `state` says the entry loaded, so the fault is not setup; `connected` and
    the failure count say the socket is down and being retried. Those two
    together are what tells a broken entry from an unreachable station.
    """
    entry = await _setup(hass, station)
    connection = entry.runtime_data.connection
    await station.stop()
    await _until(lambda: not connection.connected, "the socket to notice the outage")
    await _until(lambda: connection.diagnostics()["consecutive_failures"] > 0, "a retry")

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diagnostics["entry"]["state"] == "loaded"
    assert diagnostics["connection"]["connected"] is False
    assert diagnostics["connection"]["consecutive_failures"] > 0
    assert diagnostics["open_alerts"] == []


async def test_an_entry_that_never_loaded_still_answers(
    hass: HomeAssistant, station: FakeStation, hass_client: ClientSessionGenerator
) -> None:
    """Diagnostics for a broken entry is exactly when somebody needs help.

    An entry that failed to set up has no `runtime_data`, and reaching for it
    would turn the one download that could explain the failure into a 500.

    Home Assistant registers the diagnostics platform per *domain* rather than
    per entry, so this is reachable exactly when a second station is working --
    which is also the case where a user can be asked for the file at all.
    """
    await _setup(hass, station)
    broken = MockConfigEntry(
        domain=DOMAIN,
        title="garage",
        unique_id="src_never-loaded",
        data={
            CONF_HOST: station.host,
            CONF_PORT: station.port,
            CONF_TOKEN: station.issue_token(),
            CONF_SOURCE_ID: "src_never-loaded",
            CONF_STATION_NAME: "garage",
            CONF_STATION_VERSION: None,
        },
    )
    broken.add_to_hass(hass)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, broken)

    assert diagnostics["entry"]["state"] == "not_loaded"
    assert diagnostics["entry"]["title"] == "garage"
    assert diagnostics["connection"] is None
    assert diagnostics["open_alerts"] == []
    assert "not loaded" in diagnostics["note"]
    assert broken.data[CONF_TOKEN] not in json.dumps(diagnostics)
