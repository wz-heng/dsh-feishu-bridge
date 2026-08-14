from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import queue
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from lark_channel import FeishuChannel, SecurityConfig, TransportConfig
import lark_channel.ws.client as _lark_ws_client

from .base import Bridge
from .manager import BridgeManager

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lark_channel.channel.types import CardActionEvent, InboundMessage

logger = logging.getLogger(__name__)

# Char budget per outbound card. Feishu's real limit is on the SERIALIZED card
# (~30KB), not a character count: multibyte text and JSON escaping both eat
# into it — a single CJK char can cost 6 bytes once \uXXXX-escaped. 3500 chars
# stays comfortably under 30KB even in that worst case, so we never build a
# card the API will reject.
MAX_CARD_CHARS = 3500

# Tool input / result previews are truncated before they ever reach a card.
MAX_PREVIEW_CHARS = 3000

# Cap on live card nonces held in memory. Each approval / session-list button
# mints one; consuming or evicting bounds the map. FIFO eviction of the oldest
# only ever drops a nonce for a card old enough that a late click is already
# stale — the one-time-use guarantee for *recent* cards is untouched.
MAX_LIVE_NONCES = 2000

# Feishu webhook signature headers (event subscription v2, encrypt-key mode):
# X-Lark-Signature = sha256(timestamp + nonce + encrypt_key + raw_body).
_WEBHOOK_TIMESTAMP_HEADER = "x-lark-request-timestamp"
_WEBHOOK_NONCE_HEADER = "x-lark-request-nonce"
_WEBHOOK_SIGNATURE_HEADER = "x-lark-signature"

# How far a request's timestamp may drift from wall-clock "now" and still be
# accepted. Matches the source bridge's own webhook convention.
WEBHOOK_TIMESTAMP_WINDOW_SECONDS = 300

# Cap on tracked (timestamp, nonce) replay keys, mirroring MAX_LIVE_NONCES'
# FIFO-eviction posture: normal traffic never gets near this (entries older
# than the window are pruned on every request), it only bounds a burst of
# otherwise-validly-signed requests within one window.
MAX_WEBHOOK_NONCES = 10_000

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(domain: str) -> bool:
    """True when `domain` points at loopback — the test fake-server case."""
    host = (urlsplit(domain).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


# The single event loop this process owns on behalf of the lark-channel ws
# client. Minted lazily on the first ws start and then REUSED for the whole
# process lifetime — never rebound once a client is live on it (see
# _isolate_lark_ws_loop for why "once live, never replace" is a hard rule).
_isolated_ws_loop: asyncio.AbstractEventLoop | None = None


def _isolate_lark_ws_loop() -> asyncio.AbstractEventLoop:
    """Point the lark-channel ws client at a loop we own and that uvicorn
    never runs — and, once a client is live on that loop, NEVER take it away.

    Why this exists
    ---------------
    ``lark_channel/ws/client.py`` binds a MODULE-LEVEL ``loop =
    asyncio.get_event_loop()`` at import time and uses THAT global everywhere,
    ignoring the per-instance ``self._loop``: ``WSClient.start()`` drives the
    socket with ``loop.run_until_complete(...)`` and the running client keeps
    reading it to schedule inbound work (``loop.create_task``);
    ``FeishuChannel._stop_private_ws_client`` reads the same global to
    disconnect. If this bridge is constructed while an app event loop is
    already running (the normal FastAPI-lifespan case), that import-time
    ``get_event_loop()`` captures the running server loop. In ws mode the SDK
    then runs the blocking ``WSClient.start()`` on a worker thread, which
    calls ``run_until_complete()`` on the server's own loop from that thread
    and STOPS it — ``RuntimeError: Event loop stopped before Future
    completed``, taking the whole process down at boot.

    The invariant: once live, never replace
    ---------------------------------------
    Because that global is read for the ENTIRE life of a ws client — not just
    at start — a live client and its loop are inseparable. Rebinding the
    global out from under a running client sends its freshly-received inbound
    to a loop nobody drives and its stop/disconnect to the wrong loop,
    stranding the old ``WSClient.start()`` thread forever in ``_select()``: a
    zombie. So a RUNNING loop must NEVER be swapped out.

    We therefore mint ONE loop the first time ws starts, remember it in
    ``_isolated_ws_loop``, install it as the SDK global, and from then on
    always hand back that same loop. It is replaced only when we own none yet
    or ours is CLOSED — never merely because it ``is_running()`` (the old bug:
    "running" was read as "replaceable", but running = a live client is using
    it = the one thing you must not steal). A normal stop→start reuses it (the
    SDK stops but never closes it); a second ``start()`` while the client is
    still live also reuses it, so the lone live client keeps its loop. This
    mirrors the SDK's one-global-loop model exactly: it supports a single ws
    client per process, and this bridge process registers exactly one Feishu
    bridge, so one owned loop is all there is to own.
    """
    global _isolated_ws_loop
    loop = _isolated_ws_loop
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _isolated_ws_loop = loop
    # Re-assert ourselves as the SDK global. Idempotent when it already points
    # here (the running-client case: same object, no-op); corrective on a first
    # start, where the global is still the app loop captured at import.
    _lark_ws_client.loop = loop
    return loop


class FeishuConfigError(ValueError):
    """Raised when Feishu bridge config is incomplete/inconsistent — surfaces
    as a hard boot failure rather than a silent disable."""


def build_feishu_bridge(
    manager: BridgeManager, settings: Any
) -> "FeishuBridge | None":
    """Construct a FeishuBridge from settings, or return None when the bridge
    is simply not configured (neither credential set).

    Fail-loud, not fail-silent:
    - exactly one of app_id / app_secret set → FeishuConfigError (boot fails);
    - webhook transport without a verification token → FeishuConfigError, so a
      webhook route is never registered bare (fail-closed);
    - webhook transport without an encrypt key → FeishuConfigError. A
      verification token alone cannot authenticate the request's origin (it
      is a static value carried in the body, not a per-request signature);
      the encrypt key is what `FeishuBridge.handle_webhook` uses to verify
      `X-Lark-Signature` itself, at this bridge's own boundary, before a
      request is trusted (fail-closed).
    """
    app_id = settings.feishu_app_id
    app_secret = settings.feishu_app_secret
    if not app_id and not app_secret:
        return None
    if bool(app_id) != bool(app_secret):
        raise FeishuConfigError(
            "Feishu bridge needs BOTH feishu_app_id and feishu_app_secret; "
            "exactly one is set. Configure both, or neither to disable."
        )

    transport = settings.feishu_transport
    if transport not in ("ws", "webhook"):
        raise FeishuConfigError(
            f"feishu_transport must be 'ws' or 'webhook', got {transport!r}."
        )
    if transport == "webhook" and not settings.feishu_verification_token:
        raise FeishuConfigError(
            "Feishu webhook transport requires feishu_verification_token — "
            "without it the webhook route is not registered (fail-closed)."
        )
    if transport == "webhook" and not settings.feishu_encrypt_key:
        raise FeishuConfigError(
            "Feishu webhook transport requires feishu_encrypt_key — without "
            "it inbound requests cannot be signature-verified (fail-closed)."
        )

    return FeishuBridge(
        manager,
        app_id=app_id,
        app_secret=app_secret,
        transport=transport,
        verification_token=settings.feishu_verification_token,
        encrypt_key=settings.feishu_encrypt_key,
        domain=settings.feishu_domain,
        allowed_open_ids=settings.feishu_allowed_open_ids or None,
        allowed_chat_ids=settings.feishu_allowed_chat_ids or None,
    )


class FeishuBridge(Bridge):
    """Feishu (Lark) bot integration over the official ``lark-channel-sdk``.

    Transport is the platform's own event-delivery choice, mirrored as
    config: ``ws`` (long connection — no public URL needed) or ``webhook``.
    Both feed one inbound path; the webhook HTTP route is mounted by
    ``app.py`` only in webhook mode (and only with a verification token).
    Outbound resilience — retry, jittered backoff, Retry-After, business-code
    classification, token refresh — is owned by the SDK by design; this
    bridge configures those knobs and surfaces failures, it does not
    reimplement them.
    """

    name = "feishu"

    def __init__(
        self,
        manager: BridgeManager,
        *,
        app_id: str,
        app_secret: str,
        transport: str = "ws",
        verification_token: str | None = None,
        encrypt_key: str | None = None,
        domain: str = "https://open.feishu.cn",
        allowed_open_ids: list[str] | None = None,
        allowed_chat_ids: list[str] | None = None,
    ) -> None:
        super().__init__(manager)
        self.transport = transport
        self._app_id = app_id
        self._app_secret = app_secret
        # Our OWN copy, used by _verify_webhook_request — never delegated to
        # the SDK (see handle_webhook / build_feishu_bridge).
        self._encrypt_key = encrypt_key
        self._domain = domain.rstrip("/")
        # Fail-closed allowlists: an empty/None open_id set means the
        # `_authorized` gate rejects EVERY sender and operator — there is no
        # implicit allow-all. A None chat set means "no group restriction".
        self.allowed_open_ids: frozenset[str] = frozenset(allowed_open_ids or ())
        self.allowed_chat_ids: frozenset[str] | None = (
            frozenset(allowed_chat_ids) if allowed_chat_ids else None
        )

        # Loopback (test fake server) must not have its outbound POSTs
        # hijacked by an ambient http_proxy (e.g. a system-wide proxy on a
        # dev machine). Real domains keep None (honor env proxy).
        trust_env_proxy = False if _is_loopback(domain) else None

        self._channel = FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            domain=domain,
            verification_token=verification_token,
            encrypt_key=encrypt_key,
            transport=TransportConfig(kind=transport, trust_env_proxy=trust_env_proxy),
            # SDK default is "compat"; strict turns on the full signature /
            # timestamp / anti-replay posture a production bot needs.
            security=SecurityConfig(mode="strict"),
        )
        self._channel.on("message", self._on_message)
        self._channel.on("cardAction", self._on_card_action)

        # nonce -> {"kind", ...identity fields} for one-time card consumption.
        # Insertion-ordered dict; FIFO-evicted at cap.
        self._nonces: dict[str, dict[str, str]] = {}

        # "timestamp:nonce" -> monotonic expiry (when the SIGNED timestamp
        # itself ages out of the validity window — see
        # _consume_webhook_nonce), for webhook replay rejection. Pruned of
        # expired entries on every request, FIFO-evicted at cap as a
        # defensive floor.
        self._webhook_nonces: dict[str, float] = {}

        # The app's main event loop, captured in start(). The SDK delivers
        # inbound events on its OWN loop in ws mode (a background thread), but
        # SessionManager's turn scheduling (asyncio.create_task) needs a loop
        # that's actually running — this app's main loop. We marshal manager
        # calls back onto it (see _run_on_main). Webhook mode has no such
        # split: our own route handler calls into the SDK on the main loop
        # already, so _run_on_main's fast path (await inline) applies there.
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._tick_stop = False  # set True in stop() to end _keepalive_tick
        self._tick_future: Any = None  # concurrent.futures.Future once scheduled

        # Outbound goes through OUR OWN REST calls, NOT the SDK's channel.send,
        # and runs on a DEDICATED worker thread: the event loop only
        # `queue.put`s a request (instant) and the worker sends serially with
        # a BLOCKING `requests` call. This keeps the loop free even if a send
        # stalls, and matches the SDK's own retry/backoff posture at our layer
        # instead of depending on channel.send internals we don't control.
        self._token: str | None = None
        self._token_exp: float = 0.0  # monotonic seconds
        self._token_lock = threading.Lock()  # guards token; held by the worker
        self._loopback = _is_loopback(domain)
        # Per-request timeouts (connect, read). Tight so a stuck send can't
        # wedge the outbound queue for long; the loop is unaffected either way.
        self._timeout: tuple[float, float] = (5.0, 15.0)
        self._out_q: "queue.Queue[tuple | None]" = queue.Queue(maxsize=1000)
        self._worker: threading.Thread | None = None
        # Set in stop() to end the worker even when the queue is full (a plain
        # blocking `put(None)` sentinel could otherwise wedge shutdown).
        self._worker_stop = threading.Event()
        # How long stop()/start() wait off-loop for the worker thread to exit
        # before giving up (stop) or failing loud (start). An attribute so
        # lifecycle tests can shrink it and drive a real join-timeout quickly.
        self._worker_join_timeout: float = 5.0

    # --- lifecycle -----------------------------------------------------------

    @property
    def healthy(self) -> bool:
        # Webhook mode has no live socket to be "unready"; it's healthy once
        # constructed. WS mode reports the SDK's readiness flag.
        if self.transport == "webhook":
            return True
        return bool(self._channel.is_ready)

    async def start(self) -> None:
        # Capture the main loop while we're on it — SDK event handlers run on a
        # different (background) loop in ws mode and must hop back here.
        self._main_loop = asyncio.get_running_loop()
        # WS transport only: keep the SDK's module-level ws loop off THIS loop.
        # It may have captured our (running) loop at import time; letting the
        # SDK drive run_until_complete on it from a worker thread stops the
        # app loop and crashes boot. Install our owned, isolated loop first —
        # reused across restarts and never stolen from a live client.
        if self.transport == "ws":
            _isolate_lark_ws_loop()
        # Bring the outbound worker up FIRST, then the SDK channel + keepalive
        # tick. The only thing that can fail here is a stuck lingering worker
        # (_ensure_worker raises); doing it before channel/tick means such a
        # failure happens with NOTHING half-started to roll back.
        await self._ensure_worker()
        # From here on the worker is already live, so any failure bringing up
        # the channel/tick must roll it back — else a failed start leaves a
        # healthy outbound worker spinning against a dead channel. Tear down
        # via stop() (best-effort; the ORIGINAL error still propagates).
        try:
            # Webhook mode returns ready without dialing; WS returns once
            # connected.
            await self._channel.start_background()
            # Keep the SDK's background loop iterating (see _keepalive_tick)
            # so cross-thread inbound scheduling stays prompt. Save the
            # future so stop() can cancel/await it, and never stack a second
            # tick on a re-start.
            self._tick_stop = False
            if self._tick_future is None or self._tick_future.done():
                self._tick_future = self._channel.schedule(self._keepalive_tick())
        except Exception:
            try:
                await self.stop()
            except Exception:
                logger.exception(
                    "Feishu bridge rollback after failed start also failed"
                )
            raise
        logger.info("Feishu bridge started (transport=%s)", self.transport)

    async def _ensure_worker(self) -> None:
        """Bring the dedicated outbound worker (see __init__) to a known-live
        state, cleanly and idempotently.

        - A healthy running worker (alive, not being stopped) → no-op, so a
          re-start doesn't stack a second worker or drop in-flight sends.
        - A lingering worker from a stop() whose join timed out (alive AND the
          stop-flag set) is still draining a slow in-flight send and WILL exit
          once it returns. Wait it out off-loop; if it truly refuses to die,
          raise rather than run two workers on one queue.
        - Otherwise (no worker, or the old one has exited): discard any stale
          queue state so the fresh worker doesn't immediately read a stale
          sentinel and die; then clear the flag and spawn.
        """
        worker = self._worker
        if worker is not None and worker.is_alive() and not self._worker_stop.is_set():
            return  # already running and healthy
        if worker is not None and worker.is_alive() and self._worker_stop.is_set():
            await asyncio.to_thread(worker.join, self._worker_join_timeout)
            if worker.is_alive():
                raise RuntimeError(
                    "Feishu outbound worker did not exit after stop(); refusing "
                    "to start a second worker on the same queue"
                )
        self._drain_queue()
        self._worker_stop.clear()
        self._worker = threading.Thread(
            target=self._outbound_worker, name="feishu-outbound", daemon=True
        )
        self._worker.start()

    def _drain_queue(self) -> None:
        """Discard every pending outbound item, INCLUDING any stale sentinel.

        Fire-and-forget outbound is intentionally NOT replayed across a
        stop/start lifecycle: a shutdown-era card update for a since-gone
        session must not resurrect on the next boot, and a leftover ``None``
        sentinel must not kill a freshly-spawned worker."""
        while True:
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                return

    async def _keepalive_tick(self) -> None:
        """Keep the SDK's background event loop iterating on a short timer.

        The SDK delivers INBOUND events on a loop in a background thread
        (ws mode), fed cross-thread. A tiny periodic sleep forces the loop to
        iterate ~every 50ms off its own timer, draining queued cross-thread
        work promptly instead of sitting on a stale select() timeout. Cheap;
        ends on ``stop()``. (Outbound doesn't rely on this — it runs on its
        own worker thread.)
        """
        while not self._tick_stop:
            await asyncio.sleep(0.05)

    async def _run_on_main(self, coro, *, wait: bool = True):
        """Run `coro` on the main app loop, from whatever loop we're on.

        SDK inbound handlers run on the SDK's background loop in ws mode;
        SessionManager's turn scheduling needs a loop that's actually
        running, so we hop there via run_coroutine_threadsafe.

        ``wait=True`` awaits the result (card actions need it). ``wait=False``
        is FIRE-AND-FORGET: it returns immediately after scheduling. That is
        essential for an agent turn — the SDK serializes a chat's outbound
        behind the *inbound* handler that produced it, so if ``_on_message``
        blocked on the whole turn, the turn's own reply to that chat would
        deadlock. Errors from a detached coro are logged, not lost.

        Unit tests (and the already-on-main case, e.g. webhook mode) have no
        cross-thread hop to make, so we just await inline.
        """
        loop = self._main_loop
        if loop is None or loop is asyncio.get_running_loop():
            return await coro
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        if wait:
            return await asyncio.wrap_future(fut)
        fut.add_done_callback(self._log_detached_error)
        return None

    @staticmethod
    def _log_detached_error(fut) -> None:
        try:
            exc = fut.exception()
        except Exception:  # cancelled
            return
        if exc is not None:
            logger.error("Feishu: detached inbound handler failed: %r", exc)

    # --- outbound REST (dedicated worker thread; loop only enqueues) ---------

    # Business codes meaning the tenant_access_token is invalid/expired — clear
    # the cache and retry once.
    _TOKEN_INVALID_CODES = frozenset({99991663, 99991661, 99991664, 99991665})
    # Codes worth backing off and retrying (rate limit / transient).
    _RETRY_CODES = frozenset({99991400, 230020, 230098, 11232})
    _MAX_ATTEMPTS = 3

    def _enqueue(self, method: str, path: str, body: dict[str, Any]) -> None:
        """Hand one REST call to the outbound worker. Non-blocking; never waits
        on the network from the event loop."""
        try:
            self._out_q.put_nowait((method, path, body))
        except queue.Full:
            logger.error("Feishu: outbound queue full, dropping %s %s", method, path)

    def _outbound_worker(self) -> None:
        """Serial outbound sender, on its own thread. Polls the queue with a
        short timeout so a `stop()` flag ends the thread promptly even when
        the queue is full (a plain blocking `get` could never see the
        sentinel then and would wedge shutdown)."""
        while not self._worker_stop.is_set():
            try:
                item = self._out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:  # sentinel from stop()
                return
            method, path, body = item
            try:
                self._api_sync(method, path, body)
            except Exception:
                logger.exception("Feishu: outbound worker failed on %s %s", method, path)

    def _get_token_sync(self, *, force: bool = False) -> str | None:
        """Cached tenant_access_token; fetch on miss/expiry. Runs on the worker
        thread (blocking). Feishu signals token errors as HTTP 200 + a business
        ``code``."""
        with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_exp:
                return self._token
            try:
                data = self._sync_request(
                    "POST",
                    f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
                    {"app_id": self._app_id, "app_secret": self._app_secret}, {},
                )
            except Exception:
                logger.exception("Feishu: token fetch failed")
                return None
            if data.get("code") != 0 or not data.get("tenant_access_token"):
                logger.error("Feishu: token fetch error: %s", data)
                return None
            self._token = data["tenant_access_token"]
            self._token_exp = time.monotonic() + max(60, int(data.get("expire", 7200)) - 300)
            return self._token

    def _sync_request(self, method: str, url: str, body: dict, headers: dict) -> dict:
        """One blocking Feishu REST call via ``requests`` (worker thread only).

        Loopback (test fake server) uses a ``trust_env=False`` session so NO
        ambient proxy — including ``ALL_PROXY`` (which requests would
        otherwise merge to an ``all`` entry that a ``{"http": None}``
        override doesn't cover) — can swallow the localhost POST. Real
        domains honor env.
        """
        import requests
        if self._loopback:
            with requests.Session() as s:
                s.trust_env = False
                r = s.request(method, url, json=body, headers=headers, timeout=self._timeout)
        else:
            r = requests.request(method, url, json=body, headers=headers, timeout=self._timeout)
        return r.json()

    def _api_sync(
        self, method: str, path: str, body: dict[str, Any]
    ) -> dict[str, Any] | None:
        """One Feishu REST call with token-refresh-on-invalid + code-based
        backoff, on the worker thread. Total wall-clock is bounded by the
        per-request (connect, read) timeout × a few attempts plus short
        sleeps — and, being off the event loop, it never freezes the server."""
        for attempt in range(self._MAX_ATTEMPTS):
            token = self._get_token_sync(force=attempt > 0 and self._token is None)
            if token is None:
                return None
            try:
                data = self._sync_request(
                    method, f"{self._domain}{path}", body,
                    {"Authorization": f"Bearer {token}"},
                )
            except Exception:
                logger.exception("Feishu: %s %s raised", method, path)
                if attempt < self._MAX_ATTEMPTS - 1:
                    time.sleep(0.4 * (attempt + 1) + secrets.randbelow(200) / 1000)
                    continue
                return None
            code = data.get("code", 0)
            if code == 0:
                return data
            if code in self._TOKEN_INVALID_CODES and attempt < self._MAX_ATTEMPTS - 1:
                self._token = None  # force refresh next iteration
                continue
            if code in self._RETRY_CODES and attempt < self._MAX_ATTEMPTS - 1:
                time.sleep(0.4 * (attempt + 1) + secrets.randbelow(200) / 1000)
                continue
            logger.error("Feishu: %s %s failed code=%s msg=%s", method, path, code, data.get("msg"))
            return None
        return None

    async def stop(self) -> None:
        # Tear the outbound worker down first, then the tick + SDK channel.
        worker = self._worker
        if worker is not None and worker.is_alive():
            # Flag first so the worker's timed `get` exits even on a full queue.
            self._worker_stop.set()
            # Drop every pending fire-and-forget send (not replayed across a
            # lifecycle), then drop a best-effort sentinel to wake an idle
            # worker at once. Never block the event loop: the put is
            # non-blocking and the join runs off-loop.
            self._drain_queue()
            try:
                self._out_q.put_nowait(None)  # sentinel: exit promptly if idle
            except queue.Full:
                pass
            await asyncio.to_thread(worker.join, self._worker_join_timeout)
        # Only drop the reference once the worker has actually exited — else a
        # later start() would spawn a SECOND worker racing this one. If the join
        # timed out, the reference is kept and start() reaps it (or fails loud).
        if worker is None or not worker.is_alive():
            self._worker = None
        self._tick_stop = True
        if self._tick_future is not None:
            self._tick_future.cancel()
            self._tick_future = None
        await self._channel.stop_background()

    async def handle_webhook(
        self, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes]:
        """Framework-agnostic webhook entry — the mounted FastAPI route hands
        raw headers+body here.

        Signature / timestamp / replay verification happens HERE, at this
        bridge's OWN boundary, before anything is handed to the SDK. We do
        not rely on ``lark_channel`` for this: its signature check silently
        no-ops when ``encrypt_key`` is unset (impossible here — see
        ``build_feishu_bridge``, which now requires it for webhook
        transport), and even when the key is set, the SDK never checks
        timestamp freshness or nonce reuse at all — a captured valid request
        replays cleanly forever. A request that fails any of these checks is
        rejected before it ever reaches ``_channel.handle_webhook_request``
        (and therefore before it can reach ``manager.handle_incoming``).

        Only a request that PASSES is handed to the SDK dispatcher, which
        then does its own decrypt / verification-token / challenge / routing
        to our registered handlers — that part is unaffected.
        """
        ok, reason = self._verify_webhook_request(headers, body)
        if not ok:
            logger.warning("Feishu: webhook request rejected (%s)", reason)
            return 401, b'{"code":401,"msg":"unauthorized"}'
        return await self._channel.handle_webhook_request(headers, body)

    def _verify_webhook_request(
        self, headers: dict[str, str], body: bytes
    ) -> tuple[bool, str]:
        """Fail-closed signature + timestamp + replay check.

        Returns ``(True, "")`` on success or ``(False, reason)`` on the first
        check that fails — headers missing, signature mismatch, timestamp
        outside the window, or a (timestamp, nonce) pair already seen.
        """
        lowered = {k.lower(): v for k, v in headers.items()}
        timestamp = lowered.get(_WEBHOOK_TIMESTAMP_HEADER)
        nonce = lowered.get(_WEBHOOK_NONCE_HEADER)
        signature = lowered.get(_WEBHOOK_SIGNATURE_HEADER)
        if not timestamp or not nonce or not signature:
            return False, "signature headers missing"

        expected = hashlib.sha256(
            (timestamp + nonce + self._encrypt_key).encode("utf-8") + body
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False, "signature invalid"

        try:
            ts = int(timestamp)
        except ValueError:
            return False, "timestamp malformed"
        now_wall = time.time()
        if abs(now_wall - ts) > WEBHOOK_TIMESTAMP_WINDOW_SECONDS:
            return False, "timestamp outside window"

        # Only a request that already passed signature + timestamp consumes a
        # replay-cache slot — an attacker without the encrypt key can't burn
        # through it with forged (timestamp, nonce) pairs.
        if not self._consume_webhook_nonce(f"{timestamp}:{nonce}", ts, now_wall):
            return False, "nonce replay"

        return True, ""

    def _consume_webhook_nonce(self, key: str, ts: int, now_wall: float) -> bool:
        """True (and records it) the first time `key` is seen while its
        SIGNED timestamp is still inside the validity window; False on a
        repeat — the replay-rejection gate.

        The cache entry's TTL tracks how much longer `ts` itself stays valid
        (``ts + WEBHOOK_TIMESTAMP_WINDOW_SECONDS - now``), NOT a flat window
        from receipt time. A flat receipt-time TTL and the timestamp check's
        own ±window overlap asymmetrically: a request signed up to
        ``WEBHOOK_TIMESTAMP_WINDOW_SECONDS`` in the future keeps passing the
        timestamp check for up to ``2 * WEBHOOK_TIMESTAMP_WINDOW_SECONDS``
        after receipt, so a receipt-time TTL of just one window would forget
        the nonce while the timestamp check would still accept a replay.
        Tying the TTL to the signed timestamp's own expiry closes that gap:
        the nonce is remembered for exactly as long as a replay could still
        pass the timestamp check, never more, never less.
        """
        now_mono = time.monotonic()
        expired = [k for k, expiry in self._webhook_nonces.items() if expiry <= now_mono]
        for k in expired:
            self._webhook_nonces.pop(k, None)

        if key in self._webhook_nonces:
            return False
        ttl = max(0.0, (ts + WEBHOOK_TIMESTAMP_WINDOW_SECONDS) - now_wall)
        if len(self._webhook_nonces) >= MAX_WEBHOOK_NONCES:
            oldest = next(iter(self._webhook_nonces))
            self._webhook_nonces.pop(oldest, None)
        self._webhook_nonces[key] = now_mono + ttl
        return True

    # --- authorization -------------------------------------------------------

    def _authorized(self, open_id: str | None) -> bool:
        """Fail-closed sender/operator gate: only explicitly-allowlisted
        open_ids pass; an empty allowlist rejects everyone."""
        return bool(open_id) and open_id in self.allowed_open_ids

    # --- inbound -------------------------------------------------------------

    async def _on_message(self, msg: "InboundMessage") -> None:
        chat_id = msg.chat_id
        sender = msg.sender_id

        if not self._authorized(sender):
            # Log the open_id so the operator can discover their own id (the
            # bot stays silent to an unauthorized tenant member — no presence
            # leak, no spam). This is the documented "how do I get on the
            # allowlist" path: send once, read it from the server log.
            logger.warning(
                "Feishu: rejecting message from unauthorized open_id=%s (chat=%s)",
                sender,
                chat_id,
            )
            return

        chat_type = msg.chat_type
        if chat_type == "group":
            # Groups: only act on messages that @-mention the bot, and honor an
            # optional chat allowlist alongside the operator check.
            if not msg.mentioned_bot:
                return
            if self.allowed_chat_ids is not None and chat_id not in self.allowed_chat_ids:
                logger.warning("Feishu: group chat %s not in allowlist", chat_id)
                return

        # Topic/thread messages are explicitly unsupported in v1: sharing one
        # sticky session across threads would silently cross wires.
        if msg.conversation.thread_id:
            await self._send_plain(
                chat_id,
                "Topic/thread replies aren't supported yet — message me in the "
                "main chat instead.",
            )
            return

        # Text (and rich-text 'post', which renders to markdown) only.
        if msg.raw_content_type not in ("", "text", "post"):
            await self._send_plain(
                chat_id,
                "I can only read text messages right now (no voice / image / "
                "file).",
            )
            return

        # body_text strips the bot's own @-mention (groups); fall back to the
        # full content for p2p where there's no mention to strip.
        text = (msg.body_text or msg.content_text or "").strip()
        if not text:
            await self._send_plain(chat_id, "Send me a message and I'll get to work.")
            return

        # Fire-and-forget on the main loop: this runs a dsh turn (a blocking
        # SDK call on a worker thread) and can take a while. Blocking
        # _on_message on it would wedge the chat's outbound behind its own
        # inbound (see _run_on_main).
        await self._run_on_main(
            self.manager.handle_incoming(self.name, chat_id, text, self),
            wait=False,
        )

    # --- card actions (approval / session switch) ----------------------------

    async def _on_card_action(self, event: "CardActionEvent") -> None:
        """Handle an approve / deny / switch button tap.

        Must return fast — Feishu expects the callback answered within ~3s and
        will not retry a failure. So we do only in-memory work inline
        (authorize, consume nonce, settle the approval future / repoint the
        sticky session) and push the cosmetic card update + any confirmation
        onto a background task.
        """
        operator = getattr(event.operator, "open_id", "") or ""
        chat_id = event.chat_id
        value = event.action.value if event.action else None
        if not isinstance(value, dict):
            return

        if not self._authorized(operator):
            logger.warning(
                "Feishu: rejecting card action from unauthorized operator=%s", operator
            )
            return

        # Validate the click against the nonce's stored record BEFORE consuming
        # (the card's `value` is attacker-controllable; only the nonce record
        # — minted when WE built the card — is trusted). We dispatch by the
        # RECORD's kind and require EVERY identity field of the click to match
        # what we bound to that nonce; execution then uses the RECORD's fields.
        # Any mismatch — a forged action, a tampered session/tool, a switch
        # nonce replayed as approve — is rejected WITHOUT consuming the nonce,
        # so a later legitimate click still works.
        nonce = value.get("nonce")
        record = self._nonces.get(nonce) if isinstance(nonce, str) else None
        if record is None:
            # Missing / already-consumed nonce → a stale or duplicate tap. This
            # is the one-time-use guarantee: a second click is inert.
            logger.info("Feishu: ignoring stale/duplicate card action (chat=%s)", chat_id)
            return

        kind = record.get("kind")
        if kind == "approval":
            # Allow and Deny have SEPARATE nonces, each bound to its action; the
            # click's action, session, and tool must all match the record.
            # dsh v1 has no tool-approval flow (dsh_adapter.py module
            # docstring), so a session backend never actually emits an
            # approval-request event and this nonce kind is never minted in
            # practice — kept for interface parity and exercised directly by
            # tests. BridgeManager.handle_tool_decision always returns False.
            if (
                value.get("action") != record.get("action")
                or value.get("session_id") != record.get("session_id")
                or value.get("tool_use_id") != record.get("tool_use_id")
            ):
                logger.warning(
                    "Feishu: approval action/identity mismatch (chat=%s) — rejected", chat_id
                )
                return
            self._consume_nonce(nonce)
            approve = record.get("action") == "approve"
            applied = await self._run_on_main(
                self.manager.handle_tool_decision(
                    self.name, chat_id, record["session_id"], record["tool_use_id"], approve
                )
            )
            note = ("Approved." if approve else "Denied.") if applied else "No longer pending."
            self._settle_card(event.message_id, note)
        elif kind == "switch":
            # Both the action AND the session must match the record. Checking
            # only session_id would let a switch nonce be replayed with an
            # `approve`/`deny` value (session_id kept correct) — it would still
            # consume the nonce and fire a switch, a cross-action replay.
            if (
                value.get("action") != "switch"
                or value.get("session_id") != record.get("session_id")
            ):
                logger.warning(
                    "Feishu: switch action/session mismatch (chat=%s) — rejected", chat_id
                )
                return
            self._consume_nonce(nonce)
            result = await self._run_on_main(
                self.manager.switch_session(self.name, chat_id, record["session_id"])
            )
            self._settle_card(event.message_id, "Switched.")
            await self._send_plain(chat_id, result)
        else:
            # Unknown nonce kind — reject WITHOUT consuming.
            logger.warning(
                "Feishu: unknown nonce kind %r (chat=%s) — rejected", kind, chat_id
            )
            return

    def _settle_card(self, message_id: str, note: str) -> None:
        """Grey out a card after its button is acted on — a REST PATCH queued
        *after* the fast callback ack."""
        self._enqueue(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            {"content": json.dumps(self._note_card(note), ensure_ascii=False)},
        )

    # --- nonce lifecycle -----------------------------------------------------

    def _mint_nonce(self, kind: str, **fields: str) -> str:
        nonce = secrets.token_urlsafe(9)
        if len(self._nonces) >= MAX_LIVE_NONCES:
            # Drop the oldest — a card old enough that a click is already stale.
            oldest = next(iter(self._nonces))
            self._nonces.pop(oldest, None)
        self._nonces[nonce] = {"kind": kind, **fields}
        return nonce

    def _consume_nonce(self, nonce: str | None) -> dict[str, str] | None:
        if not nonce:
            return None
        return self._nonces.pop(nonce, None)

    # --- outbound send methods ----------------------------------------------

    async def _send(self, chat_id: str, message: dict[str, Any]) -> None:
        """Queue one message to a chat for the outbound worker.

        ``message`` is ``{"card": <card json>}`` or ``{"text": <str>}``. Cards go
        as ``msg_type="interactive"``, plain status lines as ``msg_type="text"``.
        ``content`` is the Feishu-required JSON *string*. Async only to match the
        base ``Bridge`` send interface — enqueue itself is instant/non-blocking.
        """
        if "card" in message:
            body = {"receive_id": chat_id, "msg_type": "interactive",
                    "content": json.dumps(message["card"], ensure_ascii=False)}
        else:
            body = {"receive_id": chat_id, "msg_type": "text",
                    "content": json.dumps({"text": message.get("text", "")}, ensure_ascii=False)}
        self._enqueue("POST", "/open-apis/im/v1/messages?receive_id_type=chat_id", body)

    async def _send_card_md(self, chat_id: str, markdown: str) -> None:
        for chunk in self._split_chars(markdown, MAX_CARD_CHARS):
            await self._send(chat_id, {"card": self._md_card(chunk)})

    async def _send_plain(self, chat_id: str, text: str) -> None:
        for chunk in self._split_chars(text, MAX_CARD_CHARS):
            await self._send(chat_id, {"text": chunk})

    async def send_text(self, chat_id: str, text: str) -> None:
        # Replies + command output render as interactive cards (lark_md),
        # which is the only way to get markdown in Feishu.
        await self._send_card_md(chat_id, text)

    async def send_tool_use(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        preview = ""
        if isinstance(tool_input, dict):
            if "command" in tool_input:
                preview = f": `{str(tool_input['command'])[:80]}`"
            elif "file_path" in tool_input:
                preview = f": `{tool_input['file_path']}`"
        await self._send_card_md(chat_id, f"**{tool_name}**{preview}")

    async def send_tool_result(self, chat_id: str, output: str, is_error: bool) -> None:
        prefix = "Error" if is_error else "Result"
        truncated = output[:MAX_PREVIEW_CHARS]
        if len(output) > MAX_PREVIEW_CHARS:
            truncated += "\n…"
        await self._send_card_md(chat_id, f"**{prefix}:**\n```\n{truncated}\n```")

    async def send_tool_approval_request(
        self,
        chat_id: str,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        input_str = json.dumps(tool_input, indent=2, ensure_ascii=False)
        if len(input_str) > MAX_PREVIEW_CHARS:
            input_str = input_str[:MAX_PREVIEW_CHARS] + "\n…"
        # Allow and Deny each get their OWN nonce, bound to that exact action
        # PLUS full identity (session + tool). The nonce record — not the
        # attacker-controllable card `value` — is the source of truth, and a
        # nonce fires only the action it was minted for: a `deny` value can't
        # replay the `approve` nonce, and neither can be re-pointed at another
        # session/tool.
        allow_nonce = self._mint_nonce(
            "approval", action="approve", session_id=session_id, tool_use_id=tool_use_id
        )
        deny_nonce = self._mint_nonce(
            "approval", action="deny", session_id=session_id, tool_use_id=tool_use_id
        )
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{tool_name}** wants to run:\n```\n{input_str}\n```",
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        self._button("Allow", "primary", {
                            "action": "approve", "session_id": session_id,
                            "tool_use_id": tool_use_id, "nonce": allow_nonce,
                        }),
                        self._button("Deny", "danger", {
                            "action": "deny", "session_id": session_id,
                            "tool_use_id": tool_use_id, "nonce": deny_nonce,
                        }),
                    ],
                },
            ],
        }
        await self._send(chat_id, {"card": card})

    async def send_session_list(
        self,
        chat_id: str,
        sessions: list[dict[str, Any]],
        note: str | None = None,
    ) -> None:
        buttons = []
        for s in sessions:
            check = " ✓" if s.get("current") else ""
            label = f"{s['name']} [{s['status']}]{check}"
            if len(label) > 60:
                label = label[:57] + "…"
            nonce = self._mint_nonce("switch", session_id=s["id"])
            buttons.append(
                self._button(
                    label, "default", {"action": "switch", "session_id": s["id"], "nonce": nonce}
                )
            )
        content = "Tap a session to switch:"
        if note:
            content += f"\n{note}"
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "action", "actions": buttons},
            ],
        }
        await self._send(chat_id, {"card": card})

    async def send_status(self, chat_id: str, status: str) -> None:
        # "running"/"idle" are noise to a phone user; surface only meaningful
        # status lines as plain text.
        if status in ("running", "idle", "waiting_approval", ""):
            return
        await self._send_plain(chat_id, status)

    async def send_result(self, chat_id: str, cost: float | None, is_error: bool) -> None:
        cost_str = f" (${cost:.4f})" if cost is not None else ""
        label = "Error" if is_error else "Done"
        await self._send_plain(chat_id, f"{label}{cost_str}")

    async def send_error(self, chat_id: str, message: str) -> None:
        await self._send_plain(chat_id, f"Error: {message}")

    # --- card helpers --------------------------------------------------------

    @staticmethod
    def _md_card(content: str) -> dict[str, Any]:
        return {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        }

    @staticmethod
    def _note_card(note: str) -> dict[str, Any]:
        """A buttonless card used to replace an acted-on card (greys it out)."""
        return {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": note}}],
        }

    @staticmethod
    def _button(text: str, style: str, value: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": style,
            "value": value,
        }

    @staticmethod
    def _split_chars(text: str, max_len: int) -> list[str]:
        if len(text) <= max_len:
            return [text]
        chunks: list[str] = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            split_at = text.rfind("\n", 0, max_len)
            if split_at == -1 or split_at < max_len // 2:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks
