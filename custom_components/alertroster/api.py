"""Client for the AlertRoster receiver station's local API.

The single place in this integration that speaks the wire protocol. The config
flow, the services and the entities all go through here and none of them build a
request of their own.

The contract is `alertroster/docs/LOCAL_ACK_PROTOCOL.md` -- §4 for source to
service, §6 for pairing and token scope, §8 for errors. Where the shipped
service in `alertroster-desktop/core/localapi.cpp` differs from that document,
this client follows the service and says so at the point it matters; those spots
are marked "divergence" below.

Two rules worth stating once:

* This client does not validate alert fields. The station validates and answers
  `422` with a `details` object, which is surfaced verbatim. Duplicating those
  rules here would guarantee drift -- the doc already says `dedup_key` is capped
  at 200 while the service accepts 255.
* The `lat_` token is a credential (§4.1: there is no TLS on this link). It is
  never logged, never placed in an exception message, and never rendered by
  `repr`. Anything added here must keep that true.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Self

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

# The station answers well under a second on a LAN; anything slower is a
# problem worth reporting rather than waiting on inside a service call.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# No receive timeout on the events socket -- it is meant to stay open for days
# and a quiet station is the normal case, not a fault. Liveness comes from the
# heartbeat instead: an unanswered ping arrives as a WSMsgType.ERROR frame,
# which is the only thing that distinguishes a dead link from a quiet one.
EVENTS_TIMEOUT = aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10.0)
EVENTS_HEARTBEAT = 30.0


class AlertRosterError(Exception):
    """Base class for every failure this client raises."""


class CannotConnect(AlertRosterError):
    """The station could not be reached at all.

    Either nothing is listening, or LAN listening is off on the station (§4.1:
    the service binds loopback always and the LAN only when the user enables
    it, so a station in its default state looks exactly like an absent one).
    """


class InvalidAuth(AlertRosterError):
    """`401`: the token is absent, unknown, or was revoked on the station.

    §8 makes those indistinguishable on purpose. This is not a transient error
    and must not be retried -- it means re-pairing, which is why the integration
    turns it into a reauth flow rather than a reconnect loop.
    """


class InvalidCode(AlertRosterError):
    """`403` from `POST /v1/pair`: the pairing attempt was refused.

    Divergence, and an important one for the config flow: a wrong code, a
    pairing window that is not open, and a station with no pairing authority at
    all are the *same* `403` with no body detail, deliberately, so that a
    guesser learns nothing (`localapi.cpp`, `LocalApi::pair`). This client
    therefore cannot tell the user which it was from the pair response alone.
    Distinguishing them needs the unauthenticated `GET /v1/discover` carrying
    `pairing_window_open` that REQUIREMENTS.md §5 item 1 asks the station for;
    until that ships, `PairingWindowClosed` is unreachable from `pair()`.
    """


class PairingWindowClosed(AlertRosterError):
    """The station's pairing window is not open.

    Only raisable once the station exposes it -- see `InvalidCode`. Defined now
    so the config flow can branch on it the day it becomes distinguishable,
    rather than having its error handling reshaped later.
    """


class InvalidAlertId(AlertRosterError):
    """An alert id that is not usable as a single path segment.

    Alert ids come from an automation, so they are user input. `yarl` resolves
    dot segments when it builds a URL, which means an id of `../../admin` turns
    `/v1/alerts/<id>` into `/admin` -- an authenticated request aimed somewhere
    the caller never named. Percent-encoding does not help: `URL.build` encodes
    again, and `joinpath` normalises the same way. So ids are checked instead.
    """


class ProtocolError(AlertRosterError):
    """The station answered successfully with something unusable.

    Distinct from `StationError`, which carries a real HTTP failure status; a
    caller branching on `status >= 400` must not meet this one.
    """


class StationError(AlertRosterError):
    """Any other error response from the station (§8).

    Carries the HTTP status, the station's `error` code and whatever else it
    sent (`details` on `422`, `available_actions` on `409`) so a caller can
    render something better than "request failed".
    """

    def __init__(
        self,
        status: int,
        error: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record the station's status and error code."""
        super().__init__(f"station returned {status} {error}")
        self.status = status
        self.error = error
        self.extra: dict[str, Any] = extra or {}


@dataclass(frozen=True)
class PairResult:
    """What `POST /v1/pair` hands back (§6.1)."""

    token: str = field(repr=False)  # never rendered: this is the credential
    source_id: str
    kind: str


@dataclass(frozen=True)
class StationInfo:
    """What an unauthenticated probe could learn about a station.

    Every field is optional because today it learns nothing: `GET /v1/discover`
    is REQUIREMENTS.md §5 item 1, a change requested of the station and not yet
    shipped, so a reachable station answers the probe with a `404`. The shape is
    here now so the config flow can read `pairing_window_open` the day it
    appears rather than being reshaped around it -- `None` means "the station
    did not say", which is not the same as `False`.
    """

    name: str | None = None
    version: str | None = None
    pairing_window_open: bool | None = None


@dataclass(frozen=True)
class StationEvent:
    """One frame from the events socket (§4.6).

    `event` is the station's event name. Transitions carry a single `alert`;
    the `snapshot` frame the service sends on join carries `alerts` instead.
    Unknown event names reach the caller unchanged rather than being dropped,
    because §9 makes new events an additive change that must not break a client.
    """

    event: str
    alert: dict[str, Any] | None = None
    alerts: list[dict[str, Any]] | None = None


class AlertRosterClient:
    """Talks to one AlertRoster receiver station.

    Holds no state beyond the address and the token: reconnect logic, backoff
    and entity state live in the caller, because what a dropped socket *means*
    is a Home Assistant question, not a protocol one.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str | None = None,
    ) -> None:
        """Create a client for the station at `host:port`.

        `token` is optional so the config flow can pair before it has one. The
        session is Home Assistant's shared one; this client never closes it.
        """
        self._session = session
        self._host = host
        self._port = port
        self._token = token

    def __repr__(self) -> str:
        """Describe the client without exposing the token."""
        return (
            f"AlertRosterClient(host={self._host!r}, port={self._port}, "
            f"token={'set' if self._token else 'unset'})"
        )

    @property
    def host(self) -> str:
        """The station's host."""
        return self._host

    @property
    def port(self) -> int:
        """The station's port."""
        return self._port

    def with_token(self, token: str) -> Self:
        """Return a client for the same station carrying `token`."""
        return type(self)(self._session, self._host, self._port, token)

    @staticmethod
    def _alert_segment(alert_id: str) -> str:
        """Return `alert_id` if it is a single safe path segment.

        The station's ids are opaque (`la_` + a ULID), so anything carrying a
        separator or resolving to a parent is not an id this client was given
        by a station -- see `InvalidAlertId`.
        """
        if not alert_id or "/" in alert_id or "\\" in alert_id or alert_id in {".", ".."}:
            raise InvalidAlertId(f"{alert_id!r} is not a usable alert id")
        return alert_id

    def _url(self, path: str) -> URL:
        """Build an absolute URL for a `/v1` path."""
        return URL.build(scheme="http", host=self._host, port=self._port, path=path)

    def _auth_headers(self) -> dict[str, str]:
        """Bearer header for an authenticated request (§6.1 step 4)."""
        if self._token is None:
            raise InvalidAuth("no pairing token for this station")
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        """Make one request and return `(status, body)`.

        Raises `CannotConnect` when the station is unreachable and `InvalidAuth`
        on `401`; every other non-2xx becomes a `StationError`. The caller sees
        a decoded JSON object -- the station answers JSON on every path,
        including its errors (§8).
        """
        headers = self._auth_headers() if authenticated else {}
        try:
            async with self._session.request(
                method,
                self._url(path),
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                # Errors are JSON too, so read the body before branching on
                # status: it carries the `details` a 422 needs to be useful.
                body = await self._decode(response)
                status = response.status
        except aiohttp.ClientError:
            # Deliberately not `from err`: aiohttp puts the full URL in its
            # message, and for the events socket that URL could carry a token.
            raise CannotConnect(
                f"could not reach the AlertRoster station at {self._host}:{self._port}"
            ) from None
        except TimeoutError:
            raise CannotConnect(
                f"the AlertRoster station at {self._host}:{self._port} did not answer"
            ) from None

        if status == 401:
            raise InvalidAuth("the station did not accept this token")
        if status >= 400:
            error = str(body.get("error", "unknown_error"))
            extra = {k: v for k, v in body.items() if k != "error"}
            raise StationError(status, error, extra)
        return status, body

    @staticmethod
    async def _decode(response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Decode a JSON object body, tolerating a station that sends neither.

        A body that is missing or not an object is not worth failing on by
        itself -- the status still carries the outcome, and an empty mapping
        keeps every caller's `.get` working.
        """
        try:
            decoded = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    async def probe(self) -> StationInfo:
        """Check the station answers, without a token (REQUIREMENTS.md §3.2 step 1).

        Raises `CannotConnect` and nothing else: **any** HTTP response means
        something is listening, so a `404` (no `/v1/discover` yet) and a `401`
        (every other path, unauthenticated) both count as reachable. That is
        deliberate rather than lax -- §4.1 has the service bind the LAN only
        once the user ticks *Accept sources from the LAN*, so a station in its
        default state is indistinguishable from an absent one at the socket
        level, and the connection failure is the only honest signal of it.

        Aiming at `/v1/discover` rather than at a path known to answer today
        means this upgrades by itself: the moment the station ships the
        endpoint, the same single request starts returning the station's name
        and whether its pairing window is open.
        """
        try:
            _status, body = await self._request("GET", "/v1/discover", authenticated=False)
        except (InvalidAuth, StationError):
            # Reachable, just not answering this path yet.
            return StationInfo()

        name = body.get("name")
        version = body.get("version")
        window_open = body.get("pairing_window_open")
        return StationInfo(
            name=name if isinstance(name, str) else None,
            version=version if isinstance(version, str) else None,
            pairing_window_open=window_open if isinstance(window_open, bool) else None,
        )

    async def pair(self, code: str, name: str, kind: str = "homeassistant") -> PairResult:
        """Exchange an 8-digit pairing code for a source token (§6.1).

        Divergence: the protocol document says the reply is
        `{token, source_id}`, but the shipped service returns `{token, id,
        kind}`. Both spellings of the id are accepted so this keeps working
        whichever way the station settles.
        """
        try:
            _status, body = await self._request(
                "POST",
                "/v1/pair",
                payload={"code": code, "name": name, "kind": kind},
                authenticated=False,
            )
        except StationError as err:
            if err.status == 403:
                raise InvalidCode("the station refused the pairing code") from None
            raise

        token = body.get("token")
        source_id = body.get("source_id") or body.get("id")
        if not isinstance(token, str) or not isinstance(source_id, str):
            # Never include the body here: on the success path it holds the token.
            raise ProtocolError("the station's pairing reply had no token or id")
        kind_returned = body.get("kind")
        return PairResult(
            token=token,
            source_id=source_id,
            kind=kind_returned if isinstance(kind_returned, str) else kind,
        )

    async def list_alerts(self) -> list[dict[str, Any]]:
        """Open alerts this source raised, newest first (§4.3).

        §6.2 scopes a source token to its own alerts, so this is what Home
        Assistant raised -- not everything the station is paging about.
        """
        _status, body = await self._request("GET", "/v1/alerts")
        alerts = body.get("alerts")
        return [a for a in alerts if isinstance(a, dict)] if isinstance(alerts, list) else []

    async def get_alert(self, alert_id: str) -> dict[str, Any]:
        """One alert of any status, for 24 hours after it closes (§4.3)."""
        _status, body = await self._request("GET", f"/v1/alerts/{self._alert_segment(alert_id)}")
        return self._alert_from(body)

    async def create_alert(
        self,
        *,
        title: str,
        detail: str | None = None,
        urgency: str | None = None,
        dedup_key: str | None = None,
        ack_timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Raise an alert on the station (§4.2).

        Omitted fields are left out of the request rather than sent as null, so
        the station applies its own defaults -- `ack_timeout_seconds` in
        particular, which this integration must not have an opinion about.

        A repeat with a live `dedup_key` is a success: the station answers `200`
        with the existing alert instead of `201`, and both come back here as the
        alert, so a retrying automation is safe. The repeat is *not* an update --
        verified against a station, the fields sent on the second call are
        ignored and the original alert comes back unchanged. A caller that wants
        to change a live alert has to resolve it and raise a new one.
        """
        payload: dict[str, Any] = {"title": title}
        if detail is not None:
            payload["detail"] = detail
        if urgency is not None:
            payload["urgency"] = urgency
        if dedup_key is not None:
            payload["dedup_key"] = dedup_key
        if ack_timeout_seconds is not None:
            payload["ack_timeout_seconds"] = ack_timeout_seconds

        _status, body = await self._request("POST", "/v1/alerts", payload=payload)
        return self._alert_from(body)

    async def resolve_alert(self, alert_id: str) -> dict[str, Any]:
        """Resolve an alert -- the source saying the condition cleared (§4.4).

        There is no `acknowledge` counterpart and there must never be one: §6.2
        refuses it for a source token because acknowledging is a person
        answering, and the station returns `403` to anything that tries.
        """
        _status, body = await self._request(
            "POST", f"/v1/alerts/{self._alert_segment(alert_id)}/resolve"
        )
        return self._alert_from(body)

    @staticmethod
    def _alert_from(body: dict[str, Any]) -> dict[str, Any]:
        """Unwrap the `{"alert": {...}}` envelope the station replies with."""
        alert = body.get("alert")
        return alert if isinstance(alert, dict) else {}

    async def events(self) -> AsyncIterator[StationEvent]:
        """Yield transitions from the station's events socket (§4.6).

        The socket is authenticated with the same Bearer header as every other
        request. The station also accepts `?token=`, but only as a fallback for
        clients that cannot set headers (`localapi.cpp`,
        `principalForUpgrade`) -- aiohttp can, and putting a credential in a URL
        gets it logged by everything that touches the URL, so this never does.

        Divergence worth knowing for the reconnect logic: the document says a
        source receives transitions only, but the shipped service sends a
        `snapshot` frame carrying the source's open alerts immediately on join.
        It is yielded like any other event; a caller that ignores it and
        re-seeds from `list_alerts()` is still correct.

        How the socket ends, which the reconnect logic has to reason about:
        a clean close ends the iterator, and so does an abruptly reset
        connection -- aiohttp turns that into a CLOSED message rather than
        raising. A link that goes silent without being closed is the case the
        heartbeat exists for, and that one raises `CannotConnect`, as does a
        protocol error. So the caller must treat a plain end-of-iteration as
        "reconnect" too; it is not proof the station meant to say goodbye.

        This is an async generator holding an open socket, so a caller that may
        abandon it part-way (`break` out of the `async for`) should wrap it in
        `contextlib.aclosing()`. Without that the socket closes whenever the
        event loop gets round to finalising the generator, which is not a
        moment worth depending on.

        Reconnecting, and how long to wait before doing it, is the caller's
        business.
        """
        try:
            async with self._session.ws_connect(
                self._url("/v1/events"),
                headers=self._auth_headers(),
                timeout=EVENTS_TIMEOUT,
                heartbeat=EVENTS_HEARTBEAT,
            ) as socket:
                async for message in socket:
                    # aiohttp ends the iteration itself on CLOSE/CLOSING/CLOSED,
                    # so what arrives here is TEXT, BINARY or ERROR.
                    if message.type is aiohttp.WSMsgType.ERROR:
                        # Where a dead connection surfaces: the heartbeat's
                        # timeout is delivered as an ERROR frame, not raised. A
                        # `break` here would end the iterator exactly as a clean
                        # close does and the caller could not tell them apart.
                        raise CannotConnect(
                            f"lost the events socket to the AlertRoster station "
                            f"at {self._host}:{self._port}"
                        ) from None
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    event = self._event_from(message.data)
                    if event is not None:
                        yield event
        except aiohttp.WSServerHandshakeError as err:
            # The upgrade is where a revoked token shows up, and it arrives as a
            # handshake failure rather than a normal response.
            if err.status == 401:
                raise InvalidAuth("the station did not accept this token") from None
            raise CannotConnect(
                f"the AlertRoster station at {self._host}:{self._port} refused the events socket"
            ) from None
        except aiohttp.ClientError:
            # Only the upgrade reaches here. Once the socket is open a
            # connection error is delivered as a CLOSED message and quietly ends
            # the iterator; only protocol errors and the heartbeat's pong
            # timeout arrive as ERROR frames.
            raise CannotConnect(
                f"could not open the events socket to the AlertRoster station "
                f"at {self._host}:{self._port}"
            ) from None
        except TimeoutError:
            # ws_connect takes no connect timeout of its own -- the handshake
            # runs under the shared session's default -- so a station that
            # accepts the TCP connection and then says nothing times out here.
            raise CannotConnect(
                f"the AlertRoster station at {self._host}:{self._port} "
                f"did not complete the events socket handshake"
            ) from None

    @staticmethod
    def _event_from(data: str) -> StationEvent | None:
        """Parse one socket frame, or `None` if it is not usable.

        A frame this client cannot parse is dropped with a debug line rather
        than killing the socket: §9 makes new events additive, and one
        unrecognised frame is never a reason to stop hearing about expiries.
        """
        try:
            frame = json.loads(data)
        except ValueError:
            _LOGGER.debug("ignoring an events frame that was not JSON")
            return None
        if not isinstance(frame, dict):
            return None
        event = frame.get("event")
        if not isinstance(event, str):
            return None
        alert = frame.get("alert")
        alerts = frame.get("alerts")
        return StationEvent(
            event=event,
            alert=alert if isinstance(alert, dict) else None,
            alerts=[a for a in alerts if isinstance(a, dict)] if isinstance(alerts, list) else None,
        )
