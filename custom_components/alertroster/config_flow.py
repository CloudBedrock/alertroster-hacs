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
from ipaddress import ip_address
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
    CONF_STATION_VERSION,
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
    # What the last successful `_async_reachable_address` probe learned. Kept
    # apart from `_station`, which is what the *flow* has settled on: the
    # zeroconf step takes the name off the announcement and only the version
    # off the probe.
    _probed: StationInfo = StationInfo()
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
        # one while the first is still on screen. The unique id is only the
        # station's name for the length of this flow -- `_async_finish`
        # replaces it with the `source_id` before the entry is created.
        await self.async_set_unique_id(name)
        # The one kind of entry that *is* keyed on the name: "Ignore" stores
        # the flow's unique id and no data at all, so neither pass in
        # `_async_paired_station` would see it and an ignored station would
        # come back every time it announced.
        self._abort_if_unique_id_configured()

        if (entry := self._async_paired_station(name, discovery_info)) is not None:
            return await self._async_follow_station(entry, name, addresses, port)

        if (host := await self._async_reachable_address(addresses, port)) is None:
            return self.async_abort(reason="cannot_connect")

        self._host = host
        self._port = port
        # The name off the announcement, which a station too old for §4.1's
        # probe still publishes; the version off the probe, which it does not.
        self._station = StationInfo(name=name, version=self._probed.version)
        # What the discovery card is titled before anyone opens it.
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_pair()

    async def _async_follow_station(
        self, entry: ConfigEntry, name: str, addresses: list[str], port: int
    ) -> ConfigFlowResult:
        """Leave a paired station alone, except for what has actually moved.

        §3.1 ignores the rediscovery of a paired station, but an entry pointing
        at where the station used to be is not much use, so an address that
        changed is written through.

        The address is only rewritten when the stored one has stopped
        answering. Checking that first is what keeps this quiet: a station
        re-announces on a timer, `ip_addresses` is ordered most-recently-
        updated-first so its order moves between announcements, and picking the
        first address that answers each time would hand the entry a different
        host every few minutes -- each one a change, each change a reload, each
        reload the events socket dropped and rebuilt for nothing. It also
        leaves a host somebody typed as `studio.local` alone rather than
        quietly replacing it with today's IP address.
        """
        updates: dict[str, Any] = {}
        if entry.data.get(CONF_STATION_NAME) != name:
            # Backfilled for entries added by hand, which have no name stored,
            # so the next move is matchable by name rather than by an address
            # that has already changed.
            updates[CONF_STATION_NAME] = name

        stored_host = entry.data.get(CONF_HOST)
        still_answering = (
            isinstance(stored_host, str)
            and entry.data.get(CONF_PORT) == port
            and await self._async_reachable_address([stored_host], port) is not None
        )
        if not still_answering and (host := await self._async_reachable_address(addresses, port)):
            updates[CONF_HOST] = host
            updates[CONF_PORT] = port

        # After the probing above rather than before it: `_probed` is what
        # those calls learned, and this is the same request that decided
        # whether the address moved. A station that upgraded therefore says so
        # on its next announcement, on its own timer, with nothing polling it.
        if (
            self._probed.version is not None
            and entry.data.get(CONF_STATION_VERSION) != self._probed.version
        ):
            updates[CONF_STATION_VERSION] = self._probed.version

        return self.async_update_reload_and_abort(
            entry,
            data_updates=updates,
            reason="already_configured",
            # Nothing changed on most announcements, and reloading an unchanged
            # entry would drop and rebuild the events socket every time one
            # arrived: a working integration restarting itself for no reason.
            reload_even_if_entry_is_unchanged=False,
        )

    async def _async_reachable_address(self, addresses: list[str], port: int) -> str | None:
        """The first of `addresses` that answers, or `None` if none do."""
        session = async_get_clientsession(self.hass)
        for address in addresses:
            try:
                info = await AlertRosterClient(session, address, port).probe()
            except CannotConnect:
                continue
            # Kept, not discarded: this is the same request that would learn
            # the station's version, and on a discovered station it is the only
            # one anything makes outside pairing.
            self._probed = info
            return address
        return None

    def _async_paired_station(
        self, name: str, discovery_info: ZeroconfServiceInfo
    ) -> ConfigEntry | None:
        """The entry already paired with this station, if there is one.

        Three passes, best evidence first, because a missed match is expensive:
        pairing through the discovery card it produces mints a second
        `source_id`, and a second `source_id` is a second entry and a second
        events socket for one station, which no unique id would catch.

        By name first, because that is the only thing that survives the station
        moving. Then by where the entry points, which is all an entry added
        through the manual step has until the first announcement backfills a
        name -- both the announced addresses and the announced hostname, since
        the manual step invites either ("10.0.0.4 or studio.local").

        Loopback is the last pass rather than part of the second, because
        `127.0.0.1` says nothing about *which* station announced it: an entry
        stored against it matches the first station on the LAN to announce
        loopback on the same port. Held back to a last resort, that misfire
        needs the announcement to match nothing else at all, while the
        same-host install `_announced_addresses` keeps loopback for still gets
        recognised. It cannot be resolved better from the announcement alone.
        """
        entries = self._async_current_entries(include_ignore=False)
        for entry in entries:
            if entry.data.get(CONF_STATION_NAME) == name:
                return entry

        hostname = discovery_info.hostname.rstrip(".").casefold()
        announced = _announced_addresses(discovery_info)
        routable = {address for address in announced if not ip_address(address).is_loopback}
        loopback = {address for address in announced if ip_address(address).is_loopback}

        for where in (routable, loopback):
            for entry in entries:
                if entry.data.get(CONF_PORT) != discovery_info.port:
                    continue
                host = entry.data.get(CONF_HOST)
                if not isinstance(host, str):
                    continue
                # Compared as an address where it is one, so that
                # `2001:DB8::1` and `2001:db8::1` are the same host rather than
                # two strings that differ.
                try:
                    if str(ip_address(host)) in where:
                        return entry
                except ValueError:
                    # Not an address, so it can only be the announced hostname
                    # -- and only if there is one to be.
                    if where is routable and hostname and host.rstrip(".").casefold() == hostname:
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
        version = entry_data.get(CONF_STATION_VERSION)
        self._station = StationInfo(
            name=name if isinstance(name, str) else None,
            version=version if isinstance(version, str) else None,
        )
        # Somebody is about to walk to the station anyway, so this is a free
        # moment to find out what it is running now -- a station upgraded since
        # pairing is exactly the kind that revoked the token.
        try:
            probed = await AlertRosterClient(
                async_get_clientsession(self.hass), self._host, self._port
            ).probe()
        except CannotConnect:
            pass
        else:
            if probed.version is not None:
                self._station = StationInfo(name=self._station.name, version=probed.version)
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
                    CONF_STATION_VERSION: self._station.version,
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
                CONF_STATION_VERSION: self._station.version,
            },
        )
