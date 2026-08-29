"""Config flow for AlertRoster: find a station, then trade a code for a token.

Two steps, in the order REQUIREMENTS.md §3.2 sets out. `user` takes a host and
port for a station that mDNS did not find and proves something is listening
there before asking for anything else; `pair` exchanges the 8-digit code shown
on the station for the source token the entry is built around.

Probing first is not politeness. §4.1 has the service bind the LAN only once
the user has ticked *Accept sources from the LAN*, which makes "the feature is
off" and "there is no station here" the same silence -- so the probe exists to
turn that silence into `cannot_connect`, whose text names the tickbox.

The token this flow obtains is a credential (§4.1: no TLS on this link). It
goes into the entry's data and nowhere else -- not into a log line, not into a
form, not into an error. Anything added here must keep that true.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AlertRosterClient,
    AlertRosterError,
    CannotConnect,
    InvalidCode,
    PairingWindowClosed,
    StationInfo,
)
from .const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DEFAULT_PORT,
    DOMAIN,
    PAIR_KIND,
    PAIR_NAME,
    PAIRING_ATTEMPTS,
)

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
    }
)

STEP_PAIR_SCHEMA = vol.Schema({vol.Required(CONF_CODE): cv.string})


class AlertRosterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AlertRoster."""

    VERSION = 1

    # Carried from the `user` step (or, once AHA-6 lands, from discovery) into
    # the `pair` step, which needs an address to POST the code to.
    _host: str = ""
    _port: int = DEFAULT_PORT
    _station: StationInfo = StationInfo()
    _wrong_codes: int = 0

    @property
    def _station_label(self) -> str:
        """Name the station in the UI, falling back to its address.

        The station only tells us its name once it ships `GET /v1/discover`
        (REQUIREMENTS.md §5 item 1), so until then the pair step's "Pair with
        {name}" title reads as the host the user just typed -- which is still
        the thing they are looking at.
        """
        return self._station.name or self._host

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take a host and port and check a station is listening there."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            # The real guard against a second entry for one station is the
            # `source_id` unique id set in `async_step_pair`; this only spares
            # the user a pairing code they were never going to be able to use.
            self._async_abort_entries_match({CONF_HOST: host, CONF_PORT: port})

            client = AlertRosterClient(async_get_clientsession(self.hass), host, port)
            try:
                station = await client.probe()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self._host = host
                self._port = port
                self._station = station
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Exchange the station's 8-digit pairing code for a source token (§6.1)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = AlertRosterClient(async_get_clientsession(self.hass), self._host, self._port)
            try:
                result = await client.pair(user_input[CONF_CODE].strip(), PAIR_NAME, PAIR_KIND)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except PairingWindowClosed:
                # Unreachable until the station can say so -- see `InvalidCode`
                # in api.py. Handled now so the day it can, this step already
                # tells the user the right thing.
                errors["base"] = "window_closed"
            except InvalidCode:
                self._wrong_codes += 1
                # The station closes its window on the third wrong code, so at
                # that point "try again" stops being useful advice and the user
                # has to go back to the station for a fresh one.
                errors["base"] = (
                    "window_exhausted" if self._wrong_codes >= PAIRING_ATTEMPTS else "invalid_code"
                )
            except AlertRosterError:
                # Safe to trace: api.py keeps the token out of every message it
                # raises, and never chains the aiohttp error whose text would
                # carry the URL.
                _LOGGER.exception("Pairing with the AlertRoster station failed")
                errors["base"] = "unknown"
            else:
                # §6.2 scopes a token to one source, so `source_id` is the
                # identity of *this* pairing and the right unique id -- it
                # survives the station changing address, which host:port
                # does not.
                await self.async_set_unique_id(result.source_id)
                self._abort_if_unique_id_configured()
                _LOGGER.debug(
                    "Paired with the AlertRoster station at %s:%s as source %s",
                    self._host,
                    self._port,
                    result.source_id,
                )
                return self.async_create_entry(
                    title=self._station_label,
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_TOKEN: result.token,
                        CONF_SOURCE_ID: result.source_id,
                        CONF_STATION_NAME: self._station.name,
                    },
                )

        return self.async_show_form(
            step_id="pair",
            data_schema=STEP_PAIR_SCHEMA,
            errors=errors,
            description_placeholders={"name": self._station_label},
        )
