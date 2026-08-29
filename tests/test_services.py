"""Service action tests (AHA-15), against the fake station over a real socket."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DOMAIN,
)

from .conftest import FakeStation


async def _setup(
    hass: HomeAssistant, station: FakeStation, title: str = "studio"
) -> MockConfigEntry:
    """A paired station, set up the way the config flow leaves it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=station.source_id + title,
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


async def _call(hass: HomeAssistant, action: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Call an action and take its response.

    The `assert` is load-bearing rather than a type-checker appeasement: both
    actions promise to hand the alert back (§3.3), so a `None` here is a
    failure of the thing under test, not a shape to tolerate.
    """
    response = await hass.services.async_call(
        DOMAIN, action, fields, blocking=True, return_response=True
    )
    assert response is not None
    return dict(response)


async def _raise(hass: HomeAssistant, **fields: Any) -> dict[str, Any]:
    """Call `alertroster.raise`."""
    return await _call(hass, "raise", fields)


async def _resolve(hass: HomeAssistant, **fields: Any) -> dict[str, Any]:
    """Call `alertroster.resolve`."""
    return await _call(hass, "resolve", fields)


# -- registration ---------------------------------------------------------


async def test_both_actions_are_registered_and_acknowledge_is_not(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§4.4 and REQUIREMENTS.md §2: a source may never acknowledge."""
    await _setup(hass, station)

    assert hass.services.has_service(DOMAIN, "raise")
    assert hass.services.has_service(DOMAIN, "resolve")
    assert not hass.services.has_service(DOMAIN, "acknowledge")


# -- raise ----------------------------------------------------------------


async def test_raise_returns_the_alert_so_resolve_can_use_its_id(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.3: `raise` hands back the alert as response data."""
    await _setup(hass, station)

    alert = await _raise(hass, title="Garage door open after midnight")

    assert alert["status"] == "triggered"
    assert alert["title"] == "Garage door open after midnight"
    assert alert["id"] in station.alerts
    assert ("POST", "/v1/alerts") in station.requests


async def test_raise_passes_ack_timeout_through_and_defaults_urgency(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Urgency defaults to high (§3.3); expiry stays the station's business (§2)."""
    await _setup(hass, station)

    passed = await _raise(hass, title="A", ack_timeout_seconds=45)
    defaulted = await _raise(hass, title="B")

    assert passed["ack_timeout_seconds"] == 45
    assert passed["urgency"] == "high"
    # Not sent, so the station applied its own default rather than ours.
    assert defaulted["ack_timeout_seconds"] == 120


async def test_raise_accepts_low_urgency(hass: HomeAssistant, station: FakeStation) -> None:
    """The other half of the selector actually reaches the station."""
    await _setup(hass, station)

    alert = await _raise(hass, title="Bin day", urgency="low")

    assert alert["urgency"] == "low"


async def test_raise_rejects_an_urgency_the_station_does_not_have(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Caught by the schema, so a typo in an automation fails loudly."""
    await _setup(hass, station)

    with pytest.raises(vol.Invalid, match="must be one of"):
        await _raise(hass, title="A", urgency="screaming")

    # Rejected before anything was sent, not by the station.
    assert ("POST", "/v1/alerts") not in station.requests


async def test_a_repeat_with_a_live_dedup_key_is_a_success(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§4.2: retrying automations are safe, and it is not an update."""
    await _setup(hass, station)

    first = await _raise(hass, title="Garage door open", dedup_key="garage")
    second = await _raise(hass, title="Something else entirely", dedup_key="garage")

    assert second["id"] == first["id"]
    assert second["title"] == "Garage door open"


# -- resolve --------------------------------------------------------------


async def test_resolve_by_alert_id(hass: HomeAssistant, station: FakeStation) -> None:
    """The id `raise` handed back closes the alert it opened."""
    await _setup(hass, station)
    alert = await _raise(hass, title="Garage door open")

    resolved = await _resolve(hass, alert_id=alert["id"])

    assert resolved["status"] == "resolved"
    assert station.alerts[alert["id"]]["status"] == "resolved"


async def test_resolve_by_dedup_key(hass: HomeAssistant, station: FakeStation) -> None:
    """For an automation that raised with a key and kept no id."""
    await _setup(hass, station)
    alert = await _raise(hass, title="Garage door open", dedup_key="garage")

    resolved = await _resolve(hass, dedup_key="garage")

    assert resolved["id"] == alert["id"]
    assert resolved["status"] == "resolved"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"alert_id": "la_1", "dedup_key": "garage"},
    ],
    ids=["neither", "both"],
)
async def test_resolve_needs_exactly_one_of_id_or_key(
    hass: HomeAssistant, station: FakeStation, fields: dict[str, str]
) -> None:
    """Ambiguity here would resolve an alert nobody named."""
    await _setup(hass, station)

    with pytest.raises(ServiceValidationError):
        await _resolve(hass, **fields)


async def test_resolve_by_an_unknown_dedup_key_names_the_station(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The automation trace has to say which station had no such alert."""
    await _setup(hass, station, title="studio")

    with pytest.raises(HomeAssistantError, match="studio"):
        await _resolve(hass, dedup_key="never-raised")


async def test_resolving_an_unknown_alert_surfaces_the_station_s_refusal(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A `404` from the station reaches the trace, naming the station."""
    await _setup(hass, station, title="studio")

    with pytest.raises(HomeAssistantError, match="studio"):
        await _resolve(hass, alert_id="la_nope")


# -- failures worth reading -----------------------------------------------


async def test_an_unreachable_station_is_named_in_the_error(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """§3.6: a failed request must name the station in the trace."""
    await _setup(hass, station, title="studio")
    await station.stop()

    with pytest.raises(HomeAssistantError, match="studio"):
        await _raise(hass, title="Garage door open")


async def test_a_revoked_token_says_to_pair_again(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """`401` is not transient -- the message has to say what to do about it."""
    await _setup(hass, station, title="studio")
    station.revoked = True

    with pytest.raises(HomeAssistantError, match="pair it again"):
        await _raise(hass, title="Garage door open")


# -- which station ---------------------------------------------------------


async def test_the_only_station_is_implied(hass: HomeAssistant, station: FakeStation) -> None:
    """Naming the only station there is would be pointless ceremony."""
    await _setup(hass, station)

    alert = await _raise(hass, title="Garage door open")

    assert alert["status"] == "triggered"


async def test_with_two_stations_the_target_is_required_and_the_error_names_them(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """Guessing which station should page someone is not ours to do."""
    await _setup(hass, station, title="studio")
    await _setup(hass, station, title="workshop")

    with pytest.raises(ServiceValidationError, match="studio, workshop"):
        await _raise(hass, title="Garage door open")


async def test_naming_the_station_picks_it(hass: HomeAssistant, station: FakeStation) -> None:
    """With several paired, `config_entry` chooses."""
    await _setup(hass, station, title="studio")
    workshop = await _setup(hass, station, title="workshop")

    alert = await _raise(hass, title="Garage door open", config_entry=workshop.entry_id)

    assert alert["status"] == "triggered"


async def test_a_config_entry_that_is_not_a_station_is_refused(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A stale entry id in an automation must not reach the wire."""
    await _setup(hass, station)
    other = MockConfigEntry(domain="sun", title="Sun")
    other.add_to_hass(hass)

    with pytest.raises(ServiceValidationError):
        await _raise(hass, title="A", config_entry=other.entry_id)
