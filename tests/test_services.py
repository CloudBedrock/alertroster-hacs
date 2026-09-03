"""Service action tests (AHA-15), against the fake station over a real socket."""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alertroster.const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DOMAIN,
)

from .conftest import FakeStation, until


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
    # Asserted on what was *sent*: the fake applies "high" itself when the
    # field is absent, so reading it back off the alert would hold even with
    # the schema default deleted.
    assert station.created[0]["urgency"] == "high"
    assert "ack_timeout_seconds" not in station.created[1]
    # Omitted, so the station applied its own default rather than ours.
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


@pytest.mark.parametrize("bad", [-1, 86_401])
async def test_ack_timeout_is_bounded_the_way_the_form_bounds_it(
    hass: HomeAssistant, station: FakeStation, bad: int
) -> None:
    """The schema has to agree with services.yaml, not just the UI.

    The selector refuses anything outside 0..86400, but a YAML automation never
    meets the selector -- so without the same bound in the schema, an automation
    reaches the station through a door the form keeps shut. `0` is inside the
    bound and has its own tests below.
    """
    await _setup(hass, station)

    with pytest.raises(vol.Invalid):
        await _raise(hass, title="A", ack_timeout_seconds=bad)

    assert ("POST", "/v1/alerts") not in station.requests


async def test_ack_timeout_zero_is_accepted(hass: HomeAssistant, station: FakeStation) -> None:
    """AHA-41: §2.1 gives `0` a meaning, so the schema has to let it through.

    It is the only value that lets somebody far away answer: §7.1 forwards this
    as `local_grace_seconds`, so any positive value pages the phone at the exact
    moment the panel gives up, and `0` is what keeps the alert open until a
    person does.
    """
    await _setup(hass, station)

    alert = await _raise(hass, title="Pump room flooding", ack_timeout_seconds=0)

    assert alert["ack_timeout_seconds"] == 0


@pytest.mark.parametrize("fractional", [0.5, 0.9])
async def test_a_fractional_ack_timeout_is_refused_not_rounded_to_never(
    hass: HomeAssistant, station: FakeStation, fractional: float
) -> None:
    """Coercion truncates, and `0` is no longer a harmless place to land.

    While the bound started at 1, `0.5` truncated to `0` and failed the range,
    so a template that divided badly got told so. Now `0` means "never expires"
    -- the same truncation would hand that automation an alert nothing can
    expire, silently, which is the opposite of the sub-second timeout it asked
    for.
    """
    await _setup(hass, station)

    with pytest.raises(vol.Invalid):
        await _raise(hass, title="A", ack_timeout_seconds=fractional)

    assert ("POST", "/v1/alerts") not in station.requests


async def test_a_whole_float_is_still_accepted(hass: HomeAssistant, station: FakeStation) -> None:
    """The guard is about the fraction, not about the type.

    Templates hand Home Assistant floats routinely, and `120.0` is a perfectly
    good two minutes.
    """
    await _setup(hass, station)

    alert = await _raise(hass, title="A", ack_timeout_seconds=120.0)

    assert alert["ack_timeout_seconds"] == 120


async def test_ack_timeout_zero_is_sent_rather_than_swallowed(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """The trap in accepting `0`: it is falsy, and omission means the opposite.

    A field left out asks the station for *its* default (300s -- an alert that
    expires). A `0` sent asks for one that never does. Anything on the path that
    tests the value for truth rather than for `None` turns the second request
    into the first, and the automation that meant "keep paging until a person
    answers" silently gets a five-minute timer instead.
    """
    await _setup(hass, station)

    await _raise(hass, title="A", ack_timeout_seconds=0)

    assert "ack_timeout_seconds" in station.created[0]
    assert station.created[0]["ack_timeout_seconds"] == 0


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


async def test_resolve_by_dedup_key_picks_the_matching_alert(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """For an automation that raised with a key and kept no id.

    Two alerts are open, because with only one the lookup could return
    whatever it liked -- "resolve the newest" would pass just as well.
    """
    await _setup(hass, station)
    garage = await _raise(hass, title="Garage door open", dedup_key="garage")
    boiler = await _raise(hass, title="Boiler pressure low", dedup_key="boiler")

    resolved = await _resolve(hass, dedup_key="garage")

    assert resolved["id"] == garage["id"]
    assert resolved["status"] == "resolved"
    # The one nobody named is untouched.
    assert station.alerts[boiler["id"]]["status"] == "triggered"


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


async def _setup_connected(
    hass: HomeAssistant, station: FakeStation, title: str = "studio"
) -> MockConfigEntry:
    """A station whose events socket is actually up before the test starts.

    Load-bearing for the reauth tests below, not tidiness. `_setup` returns as
    soon as setup finishes, with the connection task still racing to seed and
    open its socket -- so a `401` armed straight afterwards is met by the *seed*
    first, and the events task starts the reauth flow. The test would then pass
    with the service code removed. Waiting for the socket puts the station in
    the state the fix is about: connected, so nothing else is calling it, and
    the service action is the only thing that can meet the `401`.
    """
    entry = await _setup(hass, station, title)
    connection = entry.runtime_data.connection
    await until(lambda: connection.connected, "the events socket to open")
    await hass.async_block_till_done()
    return entry


def _reauth_flows(hass: HomeAssistant, entry: MockConfigEntry) -> list[ConfigFlowResult]:
    """The reauth flows waiting on this entry, if any."""
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == SOURCE_REAUTH
        and flow["context"].get("entry_id") == entry.entry_id
    ]


async def test_a_revoked_token_starts_a_reauth_flow(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """CLAUDE.md: a `401` is a reauth, and that holds for a service call too.

    The reachable case is a station that revoked the source while the socket
    was up: the events task will not meet the `401` until it next reconnects,
    which may be never if the socket stays open. Without this the automation
    trace tells somebody to pair again and nothing offers them the form, so the
    way out is deleting the entry -- which is what reauth exists to avoid.
    """
    entry = await _setup_connected(hass, station)
    # The socket is up and nothing else has called the station, so there is
    # nothing yet that could have started a flow. This is what stops the test
    # passing on the events task's reauth instead of the one under test.
    assert _reauth_flows(hass, entry) == []
    station.revoked = True

    with pytest.raises(HomeAssistantError, match="pair it again"):
        await _raise(hass, title="Garage door open")
    await hass.async_block_till_done()

    flows = _reauth_flows(hass, entry)
    assert len(flows) == 1
    # The pairing form, not the address form: §3.6 takes the host off the entry,
    # because the only thing that went wrong is the token.
    assert flows[0]["step_id"] == "reauth_confirm"


async def test_a_burst_of_revoked_calls_queues_one_flow(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """An automation that retries must not fill the UI with pairing forms.

    `async_start_reauth` is what dedupes, so this pins the behaviour rather
    than the call -- the events task reaching the same conclusion from the
    other side is the second producer this has to survive.
    """
    entry = await _setup_connected(hass, station)
    station.revoked = True

    for _ in range(3):
        with pytest.raises(HomeAssistantError, match="pair it again"):
            await _raise(hass, title="Garage door open")
        await hass.async_block_till_done()

    assert len(_reauth_flows(hass, entry)) == 1


async def test_resolve_starts_a_reauth_flow_too(hass: HomeAssistant, station: FakeStation) -> None:
    """Both actions go through the same guard, and both need to.

    `resolve` by `dedup_key` fails at the *listing* call rather than the
    resolving one, which is a second `_station_errors` block -- so this is not
    the path `raise` takes to get there.
    """
    entry = await _setup_connected(hass, station)
    assert _reauth_flows(hass, entry) == []
    station.revoked = True

    with pytest.raises(HomeAssistantError, match="pair it again"):
        await _resolve(hass, dedup_key="garage-door-night")
    await hass.async_block_till_done()

    assert len(_reauth_flows(hass, entry)) == 1


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
    """A stale entry id in an automation must not reach the wire.

    `match` pins which guard fired. The foreign entry is deliberately left
    un-set-up: the domain check runs before the loaded check, so this still
    proves the domain guard, and actually setting up `sun` would drag its
    platform's timers into the test for nothing.
    """
    await _setup(hass, station)
    other = MockConfigEntry(domain="sun", title="Sun")
    other.add_to_hass(hass)

    with pytest.raises(ServiceValidationError, match="not an AlertRoster station"):
        await _raise(hass, title="A", config_entry=other.entry_id)


async def test_with_no_station_set_up_at_all(hass: HomeAssistant) -> None:
    """What an automation hits once the last entry is removed."""
    from custom_components.alertroster import async_setup as _register

    await _register(hass, {})

    with pytest.raises(ServiceValidationError, match="no AlertRoster station"):
        await _raise(hass, title="A")


async def test_targeting_a_station_that_is_not_loaded(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """A disabled entry is still a valid id, and still cannot be paged."""
    loaded = await _setup(hass, station, title="studio")
    await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match="not loaded"):
        await _raise(hass, title="A", config_entry=loaded.entry_id)


async def test_an_alert_id_that_would_redirect_the_request_is_user_error(
    hass: HomeAssistant, station: FakeStation
) -> None:
    """`InvalidAlertId` never left the process, so it must not blame a station."""
    await _setup(hass, station, title="studio")

    with pytest.raises(ServiceValidationError) as err:
        await _resolve(hass, alert_id="../../admin")

    assert "studio" not in str(err.value)
    assert not [r for r in station.requests if r[0] == "POST"]
