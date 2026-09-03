"""The `raise` and `resolve` service actions (REQUIREMENTS.md §3.3).

What an automation touches. Both go through `AlertRosterClient` and neither
builds a request of its own.

There is no `acknowledge` here and there must never be one: protocol §4.4
refuses it for a source token, because acknowledging is a person answering at a
surface, not a program deciding the page has been dealt with.

Every failure becomes a `HomeAssistantError` naming the station, because where
these are read is an automation trace, and "request failed" tells the person
reading it nothing about which of their stations is unreachable.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .api import (
    AlertRosterError,
    CannotConnect,
    InvalidAlertId,
    InvalidAuth,
    StationError,
)
from .const import DOMAIN

if TYPE_CHECKING:
    from . import AlertRosterConfigEntry

SERVICE_RAISE = "raise"
SERVICE_RESOLVE = "resolve"

ATTR_CONFIG_ENTRY = "config_entry"
ATTR_TITLE = "title"
ATTR_DETAIL = "detail"
ATTR_URGENCY = "urgency"
ATTR_DEDUP_KEY = "dedup_key"
ATTR_ACK_TIMEOUT = "ack_timeout_seconds"
ATTR_ALERT_ID = "alert_id"

URGENCIES = ["high", "low"]

_ENTRY_SELECTOR = selector.ConfigEntrySelector({"integration": DOMAIN})


def _whole_seconds(value: Any) -> Any:
    """Refuse a fractional timeout rather than truncating it.

    `cv.positive_int` coerces, and coercion truncates. That was harmless while
    the bound started at 1: `0.5` became `0`, fell outside the range, and the
    automation was told its number was wrong. `0` is now the one value that
    means "never expires", so the same truncation would quietly grant an alert
    nothing can ever expire to a template that divided its way to half a second.

    Only a real `float` can get here fractional -- `vol.Coerce(int)` already
    refuses `"0.5"`, because `int()` of that string raises.
    """
    if isinstance(value, float) and not value.is_integer():
        raise vol.Invalid("ack_timeout_seconds must be a whole number of seconds")
    return value


RAISE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): _ENTRY_SELECTOR,
        vol.Required(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_DETAIL): cv.string,
        # Defaulted here, unlike `ack_timeout_seconds`: §3.3 fixes the default
        # urgency at `high`, while expiry is the station's business entirely.
        vol.Optional(ATTR_URGENCY, default="high"): vol.In(URGENCIES),
        vol.Optional(ATTR_DEDUP_KEY): cv.string,
        # Bounded to match services.yaml, and `0` is inside the bound on
        # purpose (AHA-41). §2.1 gives it a meaning -- no timeout, the alert
        # stays up until a person answers -- and it is the only value that lets
        # somebody far away answer: §7.1 forwards this as `local_grace_seconds`,
        # so the core holds its first escalation for exactly this long and any
        # positive value pages the phone at the moment the panel gives up.
        vol.Optional(ATTR_ACK_TIMEOUT): vol.All(
            _whole_seconds, cv.positive_int, vol.Range(min=0, max=86400)
        ),
    }
)

RESOLVE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): _ENTRY_SELECTOR,
        vol.Optional(ATTR_ALERT_ID): cv.string,
        vol.Optional(ATTR_DEDUP_KEY): cv.string,
    }
)


def _target_entry(hass: HomeAssistant, call: ServiceCall) -> AlertRosterConfigEntry:
    """Find the station this call is aimed at.

    With one station paired the target may be left out, because making every
    automation name the only station there is would be pointless ceremony. With
    several it must be given, and the error names them -- guessing which
    station should page someone is not a choice this integration gets to make.
    """
    entries: list[AlertRosterConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)

    if (entry_id := call.data.get(ATTR_CONFIG_ENTRY)) is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(f"{entry_id} is not an AlertRoster station")
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(f"the AlertRoster station {entry.title} is not loaded")
        return cast("AlertRosterConfigEntry", entry)

    if not entries:
        raise ServiceValidationError("no AlertRoster station is set up")
    if len(entries) > 1:
        names = ", ".join(sorted(e.title for e in entries))
        raise ServiceValidationError(
            f"several AlertRoster stations are paired ({names}); "
            f"name the one to use in the action's station field"
        )
    return entries[0]


@contextlib.contextmanager
def _station_errors(hass: HomeAssistant, entry: AlertRosterConfigEntry) -> Iterator[None]:
    """Turn every way a station call can fail into a readable error.

    A context manager rather than a wrapper taking the coroutine: the calls it
    guards return different shapes -- one alert, or a list of them -- and a
    wrapper would have had to claim one type for both and lie about the other.

    It takes `hass` for one reason: a `401` has to start a reauth flow, and that
    is the only thing here that does something to Home Assistant rather than
    just describing what went wrong.
    """
    try:
        yield
    except InvalidAlertId as err:
        # Never left the process: the id came from the automation and `api.py`
        # refused it before building a URL. Calling that a station failure
        # would point the reader at the wrong machine.
        raise ServiceValidationError(f"{err}") from err
    except CannotConnect as err:
        raise HomeAssistantError(f"could not reach the AlertRoster station {entry.title}") from err
    except InvalidAuth as err:
        # The same answer `connection.py` gives the events socket, because it is
        # the same fact: the token was revoked, and no amount of retrying fixes
        # that. The socket gets here on its own the next time it reconnects --
        # but a call that fails while the socket is still up would otherwise
        # leave the user told to pair again with nothing in the UI offering to,
        # and deleting the entry to re-add it is what reauth exists to avoid.
        #
        # `async_start_reauth` returns without doing anything when a reauth or
        # reconfigure flow is already in progress for the entry, so the two
        # paths -- and a burst of failing calls -- cannot queue several.
        entry.async_start_reauth(hass)
        # The message stays, and still names the station: this is read in an
        # automation trace, where the repair notification is not.
        raise HomeAssistantError(
            f"the AlertRoster station {entry.title} no longer recognises "
            f"this Home Assistant -- pair it again"
        ) from err
    except StationError as err:
        detail = err.extra.get("details") or err.error
        raise HomeAssistantError(
            f"the AlertRoster station {entry.title} refused the request: {detail}"
        ) from err
    except AlertRosterError as err:
        raise HomeAssistantError(
            f"the AlertRoster station {entry.title} failed the request: {err}"
        ) from err


async def _async_raise(call: ServiceCall) -> ServiceResponse:
    """Raise an alert on the station (§4.2).

    A repeat with a live `dedup_key` is a success that returns the existing
    alert, so a retrying automation is safe. It is *not* an update -- verified
    against a station, the second call's fields are ignored -- so changing a
    live alert means resolving it and raising a new one.
    """
    entry = _target_entry(call.hass, call)
    with _station_errors(call.hass, entry):
        alert = await entry.runtime_data.client.create_alert(
            title=call.data[ATTR_TITLE],
            detail=call.data.get(ATTR_DETAIL),
            urgency=call.data.get(ATTR_URGENCY),
            dedup_key=call.data.get(ATTR_DEDUP_KEY),
            # Omitted rather than defaulted: the station decides when an alert
            # has expired, and this integration holds no timers (§2).
            ack_timeout_seconds=call.data.get(ATTR_ACK_TIMEOUT),
        )
    return dict(alert)


async def _async_resolve(call: ServiceCall) -> ServiceResponse:
    """Resolve an alert -- the source saying the condition cleared (§4.4)."""
    alert_id = call.data.get(ATTR_ALERT_ID)
    dedup_key = call.data.get(ATTR_DEDUP_KEY)
    if (alert_id is None) == (dedup_key is None):
        raise ServiceValidationError("give either alert_id or dedup_key, not both and not neither")

    entry = _target_entry(call.hass, call)

    if alert_id is None:
        # §6.2 scopes the token to this source, so this searches what Home
        # Assistant raised, never everything the station is paging about.
        with _station_errors(call.hass, entry):
            open_alerts = await entry.runtime_data.client.list_alerts()
        matches = [a for a in open_alerts if a.get("dedup_key") == dedup_key]
        if not matches:
            # The automation named a key nothing is open under -- bad input, not
            # a broken station, so no stack trace in the log.
            raise ServiceValidationError(
                f"the AlertRoster station {entry.title} has no open alert "
                f"with dedup_key {dedup_key!r}"
            )
        found = matches[0].get("id")
        if not isinstance(found, str):
            raise HomeAssistantError(
                f"the AlertRoster station {entry.title} returned an alert with no id"
            )
        alert_id = found

    with _station_errors(call.hass, entry):
        alert = await entry.runtime_data.client.resolve_alert(alert_id)
    return dict(alert)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register both actions on the `alertroster` domain."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_RAISE,
        _async_raise,
        schema=RAISE_SCHEMA,
        # OPTIONAL, not ONLY: most automations just page someone and never look
        # at the reply, but one that means to resolve the alert later needs the
        # id, and §3.3 says it gets it from here.
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESOLVE,
        _async_resolve,
        schema=RESOLVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
