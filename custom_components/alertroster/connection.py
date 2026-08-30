"""The events socket, its reconnect loop, and the state it seeds (§3.6).

One of these per config entry. It holds `GET /v1/events` open, keeps the set of
open alerts current, and tells its listeners whenever either the connection or
that set changes.

Deliberately **not** a `DataUpdateCoordinator` (REQUIREMENTS.md §6). A
coordinator polls on a timer and hands out the last poll; this integration is
`local_push`, so the station tells us the moment something happens and the only
periodic work is reconnecting after a failure. Borrowing the coordinator's
shape would mean either polling a station that has nothing new to say or
writing a coordinator that never updates on its own -- neither is worth the
familiarity.

Three things here are decisions rather than implementation detail:

* **The seed runs on every connect, not just the first.** §4.6 says the socket
  carries transitions, so anything that happened while it was down is not
  replayed and `GET /v1/alerts` is the only way to learn it. The shipped
  service does send a `snapshot` frame on join, which would cover it -- but
  that is a divergence from the protocol document (`api.py` says so at the
  point it matters), and the re-seed is what makes this correct against both.

* **Setup does not wait for the station.** The obvious Home Assistant idiom is
  to seed inside `async_setup_entry` and raise `ConfigEntryNotReady` when the
  station does not answer. This does not, because §3.6 requires a `raise` to
  still be attempted over HTTP while the connection is down, and an entry that
  failed to set up cannot service an action at all -- an unreachable station
  would silently swallow the automation that was trying to page someone about
  it. So the entry loads, `connected` is false, and the loop keeps trying.

* **`401` ends the loop.** It means the token was revoked on the station, which
  no amount of retrying fixes, so it starts a reauth flow and the task returns.
  Every other failure is treated as transient and backed off.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import random
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .api import AlertRosterClient, AlertRosterError, CannotConnect, InvalidAuth, StationEvent
from .const import DOMAIN

if TYPE_CHECKING:
    from . import AlertRosterConfigEntry

_LOGGER = logging.getLogger(__name__)

# §3.6: 1 s doubling to a 60 s cap. The exponent is capped alongside it so a
# station that stays down overnight cannot grow a float that overflows.
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
_BACKOFF_MAX_FAILURES = 6  # 2**6 = 64, already past the cap

# Jitter is applied as a fraction of the delay rather than added to it, so the
# cap stays a cap. A single Home Assistant reconnecting is no thundering herd;
# what this actually avoids is one station's restart lining every paired source
# up to retry in the same instant, over and over, for as long as it is down.
_BACKOFF_JITTER_MIN = 0.5
_BACKOFF_JITTER_MAX = 1.0

# How long `async_stop` will wait for the socket to close before giving up on
# it. Longer than `api.py`'s `ws_close` would let one unresponsive station hold
# up Home Assistant's shutdown; this is deliberately shorter.
_STOP_TIMEOUT = 5.0

# Which transitions leave an alert open. `acknowledged` does: somebody answered,
# but the condition has not cleared, and `GET /v1/alerts` keeps returning it.
_OPENS_ALERT = frozenset({"alert.triggered", "alert.acknowledged"})
_CLOSES_ALERT = frozenset({"alert.resolved", "alert.expired"})


class StationConnection:
    """Holds one station's events socket and the state it pushes.

    Listeners are told *that* something changed and read `connected` and
    `open_alerts` back; they are not handed a diff. With at most a handful of
    entities per station that is cheaper to get right than to optimise, and it
    means a listener registered halfway through cannot miss anything.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AlertRosterConfigEntry,
        client: AlertRosterClient,
    ) -> None:
        """Prepare a connection for `entry`, without starting it."""
        self._hass = hass
        self._entry = entry
        self._client = client
        self._listeners: set[CALLBACK_TYPE] = set()
        # Separate from `_listeners` because these are handed the frame. A
        # listener is told only *that* something changed and reads the state
        # back; a transition cannot be recovered that way -- by the time an
        # `alert.expired` has been folded in, the alert is simply gone from the
        # set, indistinguishable from one that was resolved.
        #
        # A dict rather than a set, used only for its keys: nothing depends on
        # the order these run in, but a *fixed* order is what makes the
        # "one raising does not cost the others" guarantee testable. Over a set
        # that test passes or fails on hash ordering, which is how a real
        # regression would have slipped through it.
        self._transitions: dict[Callable[[StationEvent], None], None] = {}
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._failures = 0
        # When the current socket came up, so the backoff can tell a connection
        # that held from one that was accepted and dropped on the spot.
        self._connected_at: float | None = None
        # Keyed by alert id so a transition can replace or remove one without
        # scanning. Order is the station's within a seed and not meaningful
        # after transitions have been applied on top; nothing depends on it.
        self._alerts: dict[str, dict[str, Any]] = {}

    def __repr__(self) -> str:
        """Describe the connection without touching the client's token."""
        return (
            f"StationConnection(entry={self._entry.title!r}, "
            f"connected={self._connected}, open_alerts={len(self._alerts)})"
        )

    @property
    def connected(self) -> bool:
        """Whether the events socket is open right now.

        False until the socket is up -- including while the first seed is in
        flight -- because an entity that is not hearing transitions must not
        claim to be showing the station's state (CLAUDE.md: never show stale
        state).
        """
        return self._connected

    @property
    def open_alerts(self) -> list[dict[str, Any]]:
        """The alerts this source has open on the station, as of the last frame.

        A deep copy, not a shallow one: an alert carries nested objects -- the
        `cloud` field §2 passes through untouched, for one -- and a caller that
        reached into those would be editing the state the next listener reads.
        §6.2 scopes the token to this source, so this is what Home Assistant
        raised, never everything the station is paging about.
        """
        return [copy.deepcopy(alert) for alert in self._alerts.values()]

    @property
    def open_alert_count(self) -> int:
        """How many alerts this source has open, without copying any of them.

        `open_alerts` deep-copies the whole set, which is the right thing for
        a caller that is going to hand them to a template and the wrong thing
        for one that only wants to know whether there are any: the binary
        sensor asks this on every frame the station sends.
        """
        return len(self._alerts)

    def diagnostics(self) -> dict[str, Any]:
        """Describe this connection for a diagnostics download (AHA-26).

        The connection describes itself rather than letting `diagnostics.py`
        reach in, because the two signals worth having in a bug report --
        the consecutive failure count and whether the task is still alive --
        are private, and reaching for them from another module would make
        them public by accident.

        `consecutive_failures` is the backoff's position, so it says how hard
        this has been retrying, not merely that it is down; a socket that
        held for `BACKOFF_MAX` resets it, which is why "connected, failures
        4" is a real and useful state -- it means recently flapping.

        Nothing here is the token, and nothing here is a live object: the
        diagnostics serializer renders anything it cannot encode with
        `repr()` and does not complain, so a stray object would be a silent
        leak rather than an error.
        """
        held_for = self._held_for()
        return {
            "connected": self._connected,
            "open_alert_count": len(self._alerts),
            "consecutive_failures": self._failures,
            "connected_for_seconds": None if held_for is None else round(held_for, 1),
            "task_running": self._task is not None and not self._task.done(),
        }

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Register `update`, and return the callable that unregisters it."""
        self._listeners.add(update)

        @callback
        def remove() -> None:
            self._listeners.discard(update)

        return remove

    @callback
    def async_add_transition_listener(
        self, transition: Callable[[StationEvent], None]
    ) -> CALLBACK_TYPE:
        """Register `transition` for every frame, and return its unregister.

        Every frame, not every *known* transition: this class does not decide
        which station events mean something. `events.py` owns the mapping to
        Home Assistant's bus and ignores what is not in it, so a station that
        grows a new event under §9 needs a change in one place rather than two.
        """
        self._transitions[transition] = None

        @callback
        def remove() -> None:
            self._transitions.pop(transition, None)

        return remove

    @callback
    def async_start(self) -> None:
        """Start the events task.

        A background task on the entry, so Home Assistant cancels it if the
        entry goes away by a route that does not reach `async_stop` -- and so
        a crash in it is reported against this integration rather than
        vanishing into a stray task.
        """
        if self._task is not None:
            return
        self._task = self._entry.async_create_background_task(
            self._hass,
            self._async_run(),
            f"{DOMAIN} events {self._entry.entry_id}",
        )

    async def async_stop(self) -> None:
        """Cancel the events task and wait for the socket to close.

        Awaited rather than fire-and-forget: on a reload the next entry setup
        opens its own socket, and a station that saw the new one before the old
        one closed would have two sources' worth of sockets from one Home
        Assistant.
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # `wait` rather than `await task`: awaiting a task you just
            # cancelled raises `CancelledError`, and swallowing that would also
            # swallow a cancellation aimed at *this* coroutine -- a Home
            # Assistant shutdown that cancelled the unload would look like it
            # had finished. `wait` reports on the task instead of re-raising
            # for it, while still propagating a cancellation of our own.
            #
            # Bounded, because the close handshake runs under `ws_close`
            # (api.py) and a station that holds the TCP connection open while
            # never answering the close frame would otherwise block unload,
            # reload and shutdown for the whole of that timeout. The task is
            # the entry's, so anything left running is cancelled again by Home
            # Assistant on teardown.
            done, _pending = await asyncio.wait({task}, timeout=_STOP_TIMEOUT)
            if not done:
                _LOGGER.debug(
                    "The AlertRoster station %s did not close its events socket in time",
                    self._entry.title,
                )
            elif not task.cancelled() and (failure := task.exception()) is not None:
                # The loop handles every failure the station can cause, so
                # reaching here means a bug in this file. Report it and let the
                # unload finish: an entry that cannot be removed because its
                # events task died is a worse failure than the one that killed
                # it, and it would leave the user no way out.
                _LOGGER.error(
                    "The AlertRoster events task for %s ended in an error: %s",
                    self._entry.title,
                    failure,
                    exc_info=failure,
                )
        self._async_set_connected(False)

    async def _async_run(self) -> None:
        """Seed, hold the socket, and reconnect until cancelled or revoked.

        Nothing short of cancellation or a revoked token ends this. A bug in
        here used to end the task with `connected` still true, which is the one
        state that must never happen: entities would have gone on reporting a
        station nobody was listening to (CLAUDE.md -- never show stale state).
        """
        try:
            while True:
                try:
                    # Before the socket rather than after: a listener that wakes on
                    # `connected` should find the alerts already there.
                    await self._async_seed()
                    async with contextlib.aclosing(
                        self._client.events(self._async_on_connect)
                    ) as events:
                        async for event in events:
                            # State first: an automation woken by the bus event
                            # reads `open_alerts` through a template, and it
                            # must see the set the transition just produced,
                            # not the one before it.
                            self._async_apply(event)
                            self._async_dispatch(event)
                except InvalidAuth:
                    self._async_set_connected(False)
                    # No token, no station name, no URL -- just which entry. §3.6
                    # turns this into a reauth flow, and it must never become a
                    # reconnect loop: retrying a revoked token is how you get an
                    # integration that hammers a station for days.
                    _LOGGER.warning(
                        "The AlertRoster station %s no longer recognises this Home Assistant; "
                        "asking for it to be paired again",
                        self._entry.title,
                    )
                    self._entry.async_start_reauth(self._hass)
                    return
                except CannotConnect as err:
                    # The ordinary case: the station is off, restarting, or the
                    # link dropped. Debug, not warning -- a station that is switched
                    # off overnight is not a fault to fill somebody's log with.
                    _LOGGER.debug("Lost the AlertRoster station %s: %s", self._entry.title, err)
                except AlertRosterError as err:
                    # The station answered, and with something unusable: a `500` on
                    # the seed, or a reply that is not the shape §4.3 describes.
                    # Worth a warning, still worth retrying.
                    _LOGGER.warning(
                        "The AlertRoster station %s failed the events connection: %s",
                        self._entry.title,
                        err,
                    )
                except Exception:
                    # Not a failure the station caused -- every one of those is
                    # handled above -- so this is a bug in this file. It still must
                    # not end the task: a station that pages people is worth
                    # retrying even when we are the broken end of it, and a dead
                    # task would leave the entry looking healthy until a reload.
                    _LOGGER.exception(
                        "The AlertRoster events connection to %s failed unexpectedly",
                        self._entry.title,
                    )
                else:
                    # The iterator ended without raising. §4.6 has no "goodbye", and
                    # `api.py` documents that an abruptly reset connection ends the
                    # iterator exactly as a clean close does -- so this is not proof
                    # the station meant to stop, and it reconnects like any failure.
                    _LOGGER.debug(
                        "The AlertRoster station %s closed the events socket", self._entry.title
                    )

                self._async_set_connected(False)
                await asyncio.sleep(self._next_backoff())
        finally:
            # However this ends -- cancelled on unload, or returning
            # because the token was revoked -- nothing is listening to the
            # station any more, and saying so is the difference between an
            # entity going unavailable and one showing yesterday's board.
            self._async_set_connected(False)

    async def _async_seed(self) -> None:
        """Replace the open-alert set from `GET /v1/alerts` (§4.3)."""
        self._alerts = self._by_id(await self._client.list_alerts())
        self._async_notify()

    @staticmethod
    def _by_id(alerts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Index alerts by id, dropping any the station sent without one.

        Dropped rather than kept under a placeholder: an alert with no id
        cannot be resolved, cannot be matched by a later transition, and
        cannot be told apart from the next one -- counting it would only make
        the open-alert count wrong in a way nothing could ever put right.
        """
        indexed: dict[str, dict[str, Any]] = {}
        for alert in alerts:
            alert_id = alert.get("id")
            if isinstance(alert_id, str):
                indexed[alert_id] = alert
        return indexed

    @callback
    def _async_on_connect(self) -> None:
        """The socket is open: note when, and tell everyone at once.

        The backoff is deliberately *not* cleared here. A station that accepts
        the upgrade and closes it immediately would otherwise reset it on every
        cycle and never back off at all; `_next_backoff` clears it only once
        the socket has held for `BACKOFF_MAX`.
        """
        self._connected_at = asyncio.get_running_loop().time()
        self._async_set_connected(True)

    @callback
    def _async_apply(self, event: StationEvent) -> None:
        """Fold one frame into the open-alert set.

        Only the set of open alerts, kept honest between one seed and the next.
        Turning transitions into Home Assistant bus events is `events.py`'s
        job, reached through `_async_dispatch`: this method is allowed to
        swallow a frame it cannot use for state -- an alert with no id, say --
        and a transition that fired no bus event because of that would be the
        expiry nobody heard.
        """
        if event.alerts is not None:
            # The shipped service's join `snapshot` -- a divergence from §4.6,
            # which promises transitions only. Applied when it comes because it
            # is more current than the seed that preceded it; the seed stays
            # because a station that follows the document sends no such frame.
            self._alerts = self._by_id(event.alerts)
        elif event.alert is not None and isinstance(alert_id := event.alert.get("id"), str):
            if event.event in _CLOSES_ALERT:
                self._alerts.pop(alert_id, None)
            elif event.event in _OPENS_ALERT:
                self._alerts[alert_id] = event.alert
            else:
                # §9 makes new events additive, so an unrecognised one is a
                # newer station talking to an older integration -- never a
                # reason to drop the socket and stop hearing about expiries.
                _LOGGER.debug("Ignoring an unknown station event %r", event.event)
                return
        else:
            return

        self._async_notify()

    @callback
    def _async_set_connected(self, connected: bool) -> None:
        """Record the connection state, telling listeners only on a change."""
        if self._connected == connected:
            return
        self._connected = connected
        self._async_notify()

    def _held_for(self) -> float | None:
        """How long the socket that just ended stayed up, if it came up at all."""
        if self._connected_at is None:
            return None
        return asyncio.get_running_loop().time() - self._connected_at

    @callback
    def _async_notify(self) -> None:
        """Tell every listener to re-read this connection.

        Each one is guarded: listeners are entities belonging to other
        platforms, and one of them raising must not cost the rest their update
        -- `_listeners` is a set, so which ones got skipped would not even be
        the same twice -- nor take the events connection down with it.
        """
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception(
                    "An AlertRoster listener for %s raised while being notified",
                    self._entry.title,
                )

    @callback
    def _async_dispatch(self, event: StationEvent) -> None:
        """Hand one frame to every transition listener.

        Guarded exactly as `_async_notify` is, and for a sharper reason: what
        these listeners do is fire the bus events §3.4 exists for, and one of
        them raising must not stop the others or end the socket task. An
        expiry nobody heard is the failure this integration was written to
        prevent.
        """
        for transition in list(self._transitions):
            try:
                transition(event)
            except Exception:
                _LOGGER.exception(
                    "An AlertRoster transition listener for %s raised on %r",
                    self._entry.title,
                    event.event,
                )

    def _next_backoff(self) -> float:
        """How long to wait before the next attempt, and count this failure.

        A socket that stayed up for `BACKOFF_MAX` is treated as a connection
        that worked, so the next outage starts again from 1 s rather than from
        wherever the last run of failures had climbed to.
        """
        # `BACKOFF_MAX` doubles as the "this connection worked" threshold, read
        # here rather than snapshotted into a constant of its own: two names for
        # one number drift the moment anything patches only one of them.
        held_for = self._held_for()
        self._connected_at = None
        if held_for is not None and held_for >= BACKOFF_MAX:
            self._failures = 0

        delay = min(BACKOFF_MAX, BACKOFF_START * float(2**self._failures))
        self._failures = min(self._failures + 1, _BACKOFF_MAX_FAILURES)
        return delay * random.uniform(_BACKOFF_JITTER_MIN, _BACKOFF_JITTER_MAX)
