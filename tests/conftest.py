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
* The events socket sends a `snapshot` frame on join.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
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
        # Fixed rather than random so a test can assert the config entry's
        # unique id is the `source_id` and not something else that looks like one.
        self.source_id = "src_e49f9fe6-f09"

        # `404` is today's truth. A test that wants the station REQUIREMENTS.md
        # §5 item 1 asks for sets this to 200.
        self.discover_status = 404

        # Flip to make every authenticated path answer `401`, which is what a
        # token revoked on the station looks like from here.
        self.revoked = False

        self.alerts: dict[str, dict[str, Any]] = {}
        self.sockets: list[web.WebSocketResponse] = []

        # Every (method, path) served, so a test can assert on what was called
        # -- and, for the token tests, on what was not.
        self.requests: list[tuple[str, str]] = []

        self.host = "127.0.0.1"
        self.port = 0

    # -- wiring ---------------------------------------------------------

    @property
    def app(self) -> web.Application:
        """The application, with one route per path the client knows."""
        app = web.Application(middlewares=[self._record])
        app.router.add_get("/v1/discover", self._discover)
        app.router.add_post("/v1/pair", self._pair)
        app.router.add_get("/v1/alerts", self._list_alerts)
        app.router.add_post("/v1/alerts", self._create_alert)
        app.router.add_post("/v1/alerts/{alert_id}/resolve", self._resolve_alert)
        app.router.add_get("/v1/alerts/{alert_id}", self._get_alert)
        app.router.add_get("/v1/events", self._events)
        return app

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
        refused = (
            not self.has_pairing_authority
            or not self.pairing_window_open
            or body.get("code") != self.valid_code
        )
        if refused:
            return web.json_response({"error": "forbidden"}, status=403)

        token = f"lat_{secrets.token_hex(21)}"
        self.tokens.add(token)
        # `id`, not `source_id`; `kind` comes back as "source" whatever was sent.
        return web.json_response(
            {"token": token, "id": self.source_id, "kind": "source"},
            status=201,
        )

    async def _list_alerts(self, request: web.Request) -> web.Response:
        """§4.3. Open alerts only, newest first."""
        if not self._authorized(request):
            return self._unauthorized()
        open_alerts = [
            a for a in self.alerts.values() if a["status"] in ("triggered", "acknowledged")
        ]
        return web.json_response({"alerts": list(reversed(open_alerts))})

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

    async def _events(self, request: web.Request) -> web.StreamResponse:
        """§4.6. A `401` here is a failed upgrade, not a normal response."""
        if not self._authorized(request):
            return self._unauthorized()

        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.sockets.append(socket)
        await socket.send_json({"event": "snapshot", "alerts": list(self.alerts.values())})
        async for _message in socket:
            pass
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

    def issue_token(self) -> str:
        """Mint a token without going through pairing, for authed-path tests."""
        token = f"lat_{secrets.token_hex(21)}"
        self.tokens.add(token)
        return token


@pytest.fixture
async def station(socket_enabled: None) -> AsyncIterator[FakeStation]:
    """A fake station listening on a loopback port.

    `socket_enabled` is required: `pytest-socket`, which comes with
    `pytest-homeassistant-custom-component`, blocks every socket by default.
    That block is what enforces "no network in tests", and lifting it here --
    for a server on 127.0.0.1 that this fixture owns -- is the narrowest way to
    let the real client speak to it.
    """
    fake = FakeStation()
    server = TestServer(fake.app)
    await server.start_server()
    fake.host = server.host or "127.0.0.1"
    fake.port = server.port or 0
    try:
        yield fake
    finally:
        await server.close()
