"""A fake AlertRoster receiver station, as a real server.

REQUIREMENTS.md §6 says the tests must not touch the network, and the obvious
way to honour that is to mock the client out. This does the opposite: it stands
up an actual `aiohttp` application on a loopback port and lets the real client
make real requests to it. Mocking `AlertRosterClient` would test that the config
flow calls the methods we think it calls; this tests that the requests it
produces are ones a station answers -- which is the part that has actually been
getting things wrong.

The shapes here were read off a real station on 2026-08-29, not off the
protocol document, wherever the two disagree (`api.py` documents each
divergence at the point it matters):

* `POST /v1/pair` replies `{token, id, kind}`, not `{token, source_id}`, and
  the `kind` it returns is `"source"` -- not the `"homeassistant"` that was
  sent.
* Source ids look like `src_e49f9fe6-f09`.
* `GET /v1/discover` is a `404` with `{"error": "not_found"}`; the endpoint is
  REQUIREMENTS.md §5 item 1 and has not shipped. `discover_status` exists so a
  test can also describe the station we are asking for.
* A wrong code, a closed pairing window and a station with no pairing authority
  are all the same bodiless `403`, on purpose, so a guesser learns nothing.
* `DELETE /v1/sources/self` revokes the caller's own row and closes its
  sockets (§6.4, shipped in `alertroster-desktop` #50); any *other* id from a
  source token is a `403` decided before the row is looked up.
* The events socket sends a `snapshot` frame on join. `send_join_snapshot`
  turns that off, to be the station `LOCAL_ACK_PROTOCOL.md` §4.6 describes
  rather than the one that shipped.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

# `pytest-homeassistant-custom-component` ships this; without it Home Assistant
# refuses to load anything out of `custom_components`.
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Let Home Assistant see `custom_components/alertroster` in every test."""
    return None


class FakeStation:
    """One station, with every answer a test might need to ask it for."""

    def __init__(self) -> None:
        """Start out as a healthy station with its pairing window open."""
        self.name = "studio"
        self.version = "1.4.0"

        # Pairing.
        self.valid_code = "29109782"
        self.pairing_window_open = True
        self.has_pairing_authority = True
        self.tokens: set[str] = set()
        # What the last pair request called itself -- the station's Pairing
        # list shows this, so AHA-32 is about getting it right.
        self.paired_names: list[str] = []

        # The bodies of every `POST /v1/alerts`, so a test can prove what the
        # integration *sent* rather than what this fake defaulted for it.
        self.created: list[dict[str, Any]] = []
        # Fixed rather than random so a test can assert the config entry's
        # unique id is the `source_id` and not something else that looks like one.
        self.source_id = "src_e49f9fe6-f09"

        # `404` is today's truth. A test that wants the station REQUIREMENTS.md
        # §5 item 1 asks for sets this to 200.
        self.discover_status = 404

        # Flip to make every authenticated path answer `401`, which is what a
        # token revoked on the station looks like from here.
        self.revoked = False

        # Whether the station still has a row for this source. §6.4 answers
        # `404` once it does not, which is what a second revoke meets.
        self.paired = True

        # Answer a revoke with this status instead of carrying it out, so a
        # test can tell the one status the client swallows from the rest.
        self.revoke_status: int | None = None

        self.alerts: dict[str, dict[str, Any]] = {}
        self.sockets: list[web.WebSocketResponse] = []

        # The shipped service sends a `snapshot` on join; §4.6 says a source
        # gets transitions only. Turn it off to be the station the document
        # describes -- which is how a test proves the post-reconnect re-seed
        # is doing the work, rather than the snapshot quietly covering for it.
        self.send_join_snapshot = True

        # What the join snapshot carries, when it must differ from what
        # `GET /v1/alerts` returns. `None` means "the open alerts", which is
        # what the real station sends; a test sets this to tell the two apart,
        # because otherwise the seed and the snapshot deliver the same thing
        # and an assertion cannot say which one did the work.
        self.snapshot_alerts: list[dict[str, Any]] | None = None

        # Refuse the events upgrade with this status instead of accepting it,
        # so a test can hold the socket down while HTTP keeps working -- a
        # station whose LAN listener is up but whose socket is not.
        self.events_status: int | None = None

        # Every (method, path) served, so a test can assert on what was called
        # -- and, for the token tests, on what was not.
        self.requests: list[tuple[str, str]] = []

        # The reply spells the source id `id`; the protocol document says
        # `source_id`. A test flips this to prove the client takes either.
        self.id_field = "id"

        # Wrong codes, so the fake can close its own window on the third the
        # way the real station does -- otherwise a test of that behaviour only
        # asserts the config flow's counter against itself.
        self.wrong_codes = 0

        # Answer the next authenticated request with a non-JSON body, which is
        # the one thing `_decode` exists to tolerate.
        self.send_garbage = False

        self.host = "127.0.0.1"
        self.port = 0
        self._server: TestServer | None = None
        self.app = self._build_app()

    # -- wiring ---------------------------------------------------------

    def _build_app(self) -> web.Application:
        """The application, with one route per path the client knows."""
        app = web.Application(middlewares=[self._record])
        app.router.add_get("/v1/discover", self._discover)
        app.router.add_post("/v1/pair", self._pair)
        app.router.add_get("/v1/alerts", self._list_alerts)
        app.router.add_post("/v1/alerts", self._create_alert)
        app.router.add_post("/v1/alerts/{alert_id}/resolve", self._resolve_alert)
        app.router.add_get("/v1/alerts/{alert_id}", self._get_alert)
        app.router.add_get("/v1/events", self._events)
        app.router.add_delete("/v1/sources/{source_id}", self._revoke_source)
        # Without this the fixture's `server.close()` sits waiting out
        # aiohttp's shutdown timeout for every events socket still open --
        # which, now that the integration holds one for the life of the entry,
        # is every test that sets one up. Closing them here is aiohttp's own
        # answer to that, and it is the station going away, which is a thing a
        # real one does.
        app.on_shutdown.append(self._shutdown)
        return app

    async def _shutdown(self, app: web.Application) -> None:
        """Close the events sockets so the server can actually stop."""
        await self.drop_sockets()

    @web.middleware
    async def _record(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """Note every request so a test can assert on the traffic."""
        self.requests.append((request.method, request.path))
        return await handler(request)  # type: ignore[no-any-return]

    def _authorized(self, request: web.Request) -> bool:
        """Whether this request carries a token the station still honours."""
        if self.revoked:
            return False
        header = request.headers.get("Authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.tokens

    @staticmethod
    def _unauthorized() -> web.Response:
        """The station's `401`, spelled exactly as the real one spells it."""
        return web.json_response({"error": "invalid_credentials"}, status=401)

    # -- handlers -------------------------------------------------------

    async def _discover(self, request: web.Request) -> web.Response:
        """§5 item 1, which the shipped station answers with a `404`."""
        if self.discover_status != 200:
            return web.json_response({"error": "not_found"}, status=self.discover_status)
        return web.json_response(
            {
                "name": self.name,
                "version": self.version,
                "pairing_window_open": self.pairing_window_open,
            }
        )

    async def _pair(self, request: web.Request) -> web.Response:
        """§6.1. Every refusal is the same `403` with no body detail."""
        body = await request.json()
        wrong_code = body.get("code") != self.valid_code
        refused = not self.has_pairing_authority or not self.pairing_window_open or wrong_code
        if refused:
            if wrong_code:
                self.wrong_codes += 1
                if self.wrong_codes >= 3:
                    # Three wrong codes close the window, and it stays closed
                    # until someone opens it on the station again.
                    self.pairing_window_open = False
            # A JSON body, because §8 has the station answer JSON on every path
            # -- confirmed against a real one, which sends
            # `{"error": "invalid_credentials"}` on a 401. What the document
            # means by "no body detail" is that nothing in it distinguishes
            # these three refusals from each other.
            return web.json_response({"error": "forbidden"}, status=403)

        self.paired_names.append(str(body.get("name")))
        token = f"lat_{secrets.token_hex(21)}"
        self.tokens.add(token)
        # `id`, not `source_id`; `kind` comes back as "source" whatever was sent.
        return web.json_response(
            {"token": token, self.id_field: self.source_id, "kind": "source"},
            status=201,
        )

    async def _list_alerts(self, request: web.Request) -> web.Response:
        """§4.3. Open alerts only, newest first."""
        if not self._authorized(request):
            return self._unauthorized()
        if self.send_garbage:
            return web.Response(body=b"<html>not json</html>", content_type="text/html", status=500)
        return web.json_response({"alerts": list(reversed(self._open_alerts()))})

    def _open_alerts(self) -> list[dict[str, Any]]:
        """The alerts §4.3 calls open -- what both the list and the snapshot carry."""
        return [a for a in self.alerts.values() if a["status"] in ("triggered", "acknowledged")]

    async def _get_alert(self, request: web.Request) -> web.Response:
        """§4.3. One alert of any status."""
        if not self._authorized(request):
            return self._unauthorized()
        alert = self.alerts.get(request.match_info["alert_id"])
        if alert is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"alert": alert})

    async def _create_alert(self, request: web.Request) -> web.Response:
        """§4.2, including the idempotent repeat that is *not* an update."""
        if not self._authorized(request):
            return self._unauthorized()
        body = await request.json()
        self.created.append(dict(body))

        dedup_key = body.get("dedup_key")
        if dedup_key is not None:
            for alert in self.alerts.values():
                if alert.get("dedup_key") == dedup_key and alert["status"] in (
                    "triggered",
                    "acknowledged",
                ):
                    # Verified on a real station: the second call's fields are
                    # ignored and the original comes back unchanged.
                    return web.json_response({"alert": alert}, status=200)

        alert = {
            "id": f"la_{secrets.token_hex(8)}",
            "status": "triggered",
            "title": body["title"],
            "detail": body.get("detail"),
            "urgency": body.get("urgency", "high"),
            "dedup_key": dedup_key,
            "ack_timeout_seconds": body.get("ack_timeout_seconds", 120),
            "cloud": {"synced": False},
        }
        self.alerts[alert["id"]] = alert
        return web.json_response({"alert": alert}, status=201)

    async def _resolve_alert(self, request: web.Request) -> web.Response:
        """§4.4. The source saying the condition cleared."""
        if not self._authorized(request):
            return self._unauthorized()
        alert = self.alerts.get(request.match_info["alert_id"])
        if alert is None:
            return web.json_response({"error": "not_found"}, status=404)
        alert["status"] = "resolved"
        return web.json_response({"alert": alert})

    async def _revoke_source(self, request: web.Request) -> web.Response:
        """§6.4. A source may revoke itself, by `self` or by its own id.

        The `403` for any other id is decided before the row is looked up, so
        a refusal says nothing about whether that id exists -- which is the
        property that keeps this from being a way to enumerate the station's
        other sources.
        """
        if not self._authorized(request):
            return self._unauthorized()

        if self.revoke_status is not None:
            return web.json_response({"error": "forbidden"}, status=self.revoke_status)

        wanted = request.match_info["source_id"]
        if wanted not in {"self", self.source_id}:
            return web.json_response({"error": "forbidden"}, status=403)
        if not self.paired:
            return web.json_response({"error": "not_found"}, status=404)

        self.paired = False
        self.tokens.clear()
        await self.drop_sockets()
        return web.json_response({"revoked": self.source_id})

    async def _events(self, request: web.Request) -> web.StreamResponse:
        """§4.6. A `401` here is a failed upgrade, not a normal response."""
        if not self._authorized(request):
            return self._unauthorized()
        if self.events_status is not None:
            return web.json_response({"error": "unavailable"}, status=self.events_status)

        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.sockets.append(socket)
        if self.send_join_snapshot:
            # Open alerts only, like `GET /v1/alerts` and like the real
            # station -- verified on one, 2026-08-29. Sending everything here
            # would have made the fake the only place a closed alert could
            # arrive in a snapshot, and a test written against that would be
            # testing the fake.
            alerts = self._open_alerts() if self.snapshot_alerts is None else self.snapshot_alerts
            await socket.send_json({"event": "snapshot", "alerts": alerts})
        try:
            async for _message in socket:
                pass
        finally:
            # Otherwise a later `push()` writes to a socket the client has
            # already gone from, and the fake raises instead of the test failing
            # on whatever it was actually checking.
            if socket in self.sockets:
                self.sockets.remove(socket)
        return socket

    # -- what a test drives it with -------------------------------------

    async def push(self, event: str, alert: dict[str, Any]) -> None:
        """Send one transition to everything listening."""
        for socket in list(self.sockets):
            await socket.send_json({"event": event, "alert": alert})

    async def drop_sockets(self) -> None:
        """Close the events socket the way a restarting station would."""
        for socket in list(self.sockets):
            await socket.close()
        self.sockets.clear()

    async def stop(self) -> None:
        """Take the station off the air, as a station being restarted does."""
        if self._server is not None:
            await self._server.close()
            self._server = None

    def issue_token(self) -> str:
        """Mint a token without going through pairing, for authed-path tests."""
        token = f"lat_{secrets.token_hex(21)}"
        self.tokens.add(token)
        return token


@asynccontextmanager
async def _serving(source_id: str | None = None) -> AsyncIterator[FakeStation]:
    """Run one `FakeStation` on a loopback port for the body of a fixture."""
    fake = FakeStation()
    if source_id is not None:
        fake.source_id = source_id
    server = TestServer(fake.app)
    fake._server = server
    await server.start_server()
    fake.host = server.host or "127.0.0.1"
    fake.port = server.port or 0
    try:
        yield fake
    finally:
        await server.close()


@pytest.fixture
async def station(socket_enabled: None) -> AsyncIterator[FakeStation]:
    """A fake station listening on a loopback port.

    `socket_enabled` is required: `pytest-socket`, which comes with
    `pytest-homeassistant-custom-component`, blocks every socket by default.
    That block is what enforces "no network in tests", and lifting it here --
    for a server on 127.0.0.1 that this fixture owns -- is the narrowest way to
    let the real client speak to it.
    """
    async with _serving() as fake:
        yield fake


@pytest.fixture
async def second_station(socket_enabled: None) -> AsyncIterator[FakeStation]:
    """A second station, on its own port, for the tests about telling two apart.

    Its `source_id` differs from `station`'s because that is the config entry's
    unique id, and two entries paired to two stations cannot share one.
    """
    async with _serving("src_1cb0d3a2-7be") as fake:
        yield fake


async def until(check: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    """Wait for `check` to hold, or fail saying what never happened.

    Shared rather than copied per module: three test files were waiting on the
    same kinds of state, and a polling loop that drifts between copies is how
    one of them quietly starts passing on a timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")
