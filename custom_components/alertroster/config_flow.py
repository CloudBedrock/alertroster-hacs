"""Config flow for AlertRoster: find a station, then trade a code for a token.

Two ways in, one way through. `zeroconf` takes a station that announced itself
and `user` takes a host and port for one that did not; both prove something is
listening before asking for anything else, and both then hand off to `pair`,
which exchanges the 8-digit code shown on the station for the source token the
entry is built around.

`reauth_confirm` is the same pairing step under a different heading, reached
when the station stops recognising a token it once issued (§3.6). It runs
against the address already on the entry -- there is no host field -- so it can
only ever re-pair with the station the entry was built for.

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
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from yarl import URL

from .api import (
    AlertRosterClient,
    AlertRosterError,
    CannotConnect,
    InvalidCode,
    PairingWindowClosed,
    PairResult,
    StationInfo,
)
from .const import (
    CONF_SOURCE_ID,
    CONF_STATION_NAME,
    DEFAULT_PORT,
    DOMAIN,
    PAIR_KIND,
    PAIR_NAME_FALLBACK,
    PAIRING_ATTEMPTS,
    ZEROCONF_NAME_KEY,
    ZEROCONF_VERSION,
    ZEROCONF_VERSION_KEY,
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

# Both run `_async_pair`; they differ only in the words `strings.json` puts
# above the code box.
STEP_PAIR = "pair"
STEP_REAUTH_CONFIRM = "reauth_confirm"


def _is_usable_host(host: str, port: int) -> bool:
    """Whether `host` can address a station at all.

    The Host field collects what people can see, which means it collects
    `http://10.0.0.4` and `10.0.0.4:4747` as often as a bare host. Neither can
    build a URL, and without this the `ValueError` from deep inside the request
    escapes the step as an unknown error and kills the flow.

    Answering it with `URL.build` rather than a regex of our own is what keeps
    `fe80::1` working: yarl brackets an IPv6 literal itself, where any
    hand-rolled "a host may not contain a colon" rule would reject it.
    """
    if not host:
        return False
    try:
        URL.build(scheme="http", host=host, port=port)
    except ValueError:
        return False
    return True


def _announced_addresses(discovery_info: ZeroconfServiceInfo) -> list[str]:
    """The addresses from an announcement that are worth trying, best first.

    A station announces on every interface it has, and on a developer machine
    that is a lot of them: the `om` station publishes ten records, among them
    `172.17.0.1` (its docker bridge), `192.168.122.1` (its libvirt bridge) and
    `127.0.0.1`. Only some of those can be reached from wherever Home Assistant
    happens to be running, so the address is *chosen* by asking which one
    answers -- `dev/stations.sh` has taken the same first-one-that-answers
    approach since before this step existed.

    `ip_address` leads because Home Assistant already picked it as the most
    recently updated routable one, and loopback goes last rather than first:
    when the station is on another machine, `127.0.0.1` is whatever *Home
    Assistant* is running, so trying it before a real LAN address risks pairing
    with the wrong thing entirely. It is kept, rather than dropped, for the
    setup where the two really are the same host.

    Link-local is dropped for a duller reason -- `fe80::` needs a scope id that
    the announcement does not carry, so it could never be dialled.
    """
    routable: list[str] = []
    loopback: list[str] = []
    for ip in [discovery_info.ip_address, *discovery_info.ip_addresses]:
        if ip.is_link_local or ip.is_unspecified:
            continue
        bucket = loopback if ip.is_loopback else routable
        address = str(ip)
        if address not in routable and address not in loopback:
            bucket.append(address)
    return routable + loopback


class AlertRosterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AlertRoster."""

    VERSION = 1

    # Carried from the `zeroconf` or `user` step into the `pair` step, which
    # needs an address to POST the code to.
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

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Offer a station that announced itself on the LAN (§3.1, §4.1).

        The flow is keyed on the station's *name*, not on its host and port,
        which is a deliberate departure from what §3.1 asks for. A station
        announces on every interface it has and Home Assistant re-runs
        discovery whenever the address it picked out of that set changes, so
        an address-shaped key produces a second discovery card for a station
        already on screen -- the exact thing §3.1 wants prevented. The name is
        also the only thing an announcement carries that survives the address
        change §3.1 wants followed, which makes it the one key that can do both
        jobs. Two stations sharing a display name would collide here; the
        `source_id` unique id set at pairing is what actually stops one station
        being paired twice.
        """
        properties = discovery_info.properties
        if properties.get(ZEROCONF_VERSION_KEY) != ZEROCONF_VERSION:
            return self.async_abort(reason="unsupported_version")

        name = properties.get(ZEROCONF_NAME_KEY)
        if not isinstance(name, str) or not name.strip():
            # `name` is half of the TXT contract, and without it there is
            # nothing to key on and nothing to title the card with.
            return self.async_abort(reason="unsupported_version")
        name = name.strip()

        # Nothing to connect to, so the honest reason is the same one an
        # unreachable station gets rather than a shape complaint.
        port = discovery_info.port
        addresses = _announced_addresses(discovery_info)
        if not port or not addresses:
            return self.async_abort(reason="cannot_connect")

        # Dedupes repeat announcements: `raise_on_progress` aborts the second
        # one while the first is still on screen.
        await self.async_set_unique_id(name)

        host = await self._async_reachable_address(addresses, port)

        if (entry := self._async_paired_station(name, addresses, port)) is not None:
            # §3.1: rediscovery of a paired station is ignored, except that a
            # station which moved gets its new address written through --
            # otherwise the entry keeps pointing at where it used to be. The
            # name is backfilled for entries added by hand, which have none, so
            # the *next* move is matchable by name rather than by an address
            # that has already changed.
            updates: dict[str, Any] = {CONF_STATION_NAME: name}
            if host is not None:
                updates[CONF_HOST] = host
                updates[CONF_PORT] = port
            return self.async_update_reload_and_abort(
                entry,
                data_updates=updates,
                reason="already_configured",
                # A station re-announces on a timer. Reloading an entry that
                # did not change would drop and rebuild the events socket every
                # time it did, which is a working integration restarting itself
                # for no reason.
                reload_even_if_entry_is_unchanged=False,
            )

        if host is None:
            return self.async_abort(reason="cannot_connect")

        self._host = host
        self._port = port
        self._station = StationInfo(name=name)
        # What the discovery card is titled before anyone opens it.
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_pair()

    async def _async_reachable_address(self, addresses: list[str], port: int) -> str | None:
        """The first announced address that answers, or `None` if none do."""
        session = async_get_clientsession(self.hass)
        for address in addresses:
            try:
                await AlertRosterClient(session, address, port).probe()
            except CannotConnect:
                continue
            return address
        return None

    def _async_paired_station(
        self, name: str, addresses: list[str], port: int
    ) -> ConfigEntry | None:
        """The entry already paired with this station, if there is one.

        By name first, because that is what survives the station moving. The
        address pass is for entries added through the manual step, which have
        no name stored until this step backfills one.
        """
        entries = self._async_current_entries(include_ignore=False)
        for entry in entries:
            if entry.data.get(CONF_STATION_NAME) == name:
                return entry
        for entry in entries:
            if entry.data.get(CONF_HOST) in addresses and entry.data.get(CONF_PORT) == port:
                return entry
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take a host and port and check a station is listening there."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]

            if not _is_usable_host(host, port):
                # Its own error rather than `cannot_connect`: telling someone
                # who pasted a URL to go check "Accept sources from the LAN"
                # sends them after a problem they do not have.
                errors["base"] = "invalid_host"
            else:
                # The real guard against a second entry for one station is the
                # `source_id` unique id set in `async_step_pair`; this only
                # spares the user a pairing code they could never have used.
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
        return await self._async_pair(STEP_PAIR, user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start again after the station stopped recognising this token (§3.6).

        Reached from the events task and from any `401`. The address comes off
        the entry rather than from the user, because the only thing that has
        gone wrong is the token -- asking for a host again would invite pointing
        an existing entry at a different station.
        """
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        name = entry_data.get(CONF_STATION_NAME)
        self._station = StationInfo(name=name if isinstance(name, str) else None)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again with the station this entry is already for.

        A step of its own rather than a reuse of `pair` only because the two
        need different words on the form: somebody sent here did not ask to add
        a station and has to be told why they are looking at a code box.
        """
        return await self._async_pair(STEP_REAUTH_CONFIRM, user_input)

    async def _async_pair(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Run the pairing form for `step_id` and hand a token to `_async_finish`."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client = AlertRosterClient(async_get_clientsession(self.hass), self._host, self._port)
            try:
                # AHA-32: the station's Pairing list shows this, so it says
                # which Home Assistant, not merely that it is one.
                name = self.hass.config.location_name.strip() or PAIR_NAME_FALLBACK
                result = await client.pair(user_input[CONF_CODE].strip(), name, PAIR_KIND)
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
                return await self._async_finish(result)

        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP_PAIR_SCHEMA,
            errors=errors,
            description_placeholders={"name": self._station_label},
        )

    async def _async_finish(self, result: PairResult) -> ConfigFlowResult:
        """Create the entry, or replace the token on the one being reauthed."""
        _LOGGER.debug(
            "Paired with the AlertRoster station at %s:%s as source %s",
            self._host,
            self._port,
            result.source_id,
        )

        if self.source == SOURCE_REAUTH:
            # The unique id moves with the token. §6.2 ties a token to one
            # source row, and the station mints a new row for every pairing, so
            # after re-pairing the old `source_id` names something that no
            # longer exists -- keeping it would let the same station be added a
            # second time under its new id. There is no "is this the same
            # station?" check to make here because there is no host field to
            # get wrong: `async_step_reauth` took the address off the entry.
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                unique_id=result.source_id,
                data_updates={
                    CONF_TOKEN: result.token,
                    CONF_SOURCE_ID: result.source_id,
                },
            )

        # §6.2 scopes a token to one source, so `source_id` is the identity of
        # *this* pairing and the right unique id -- it survives the station
        # changing address, which host:port does not.
        await self.async_set_unique_id(result.source_id)
        self._abort_if_unique_id_configured()
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
