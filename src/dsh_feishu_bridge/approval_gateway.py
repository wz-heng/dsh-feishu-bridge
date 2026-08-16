"""Loopback HTTP relay between the dsh runtime's ``approval-relay`` cordis
plugin (Node, running inside the harness subprocess) and this bridge's
existing Feishu approval-card machinery (nonce + chat-ownership, already
unit-tested — see ``bridges/feishu.py``).

Why a *separate* HTTP server, not a route on the public FastAPI app: the
public app is the Feishu webhook target and, per the README quickstart,
typically sits behind a public tunnel. An endpoint that lets any caller
resolve a pending tool-approval decision must never be reachable from
outside — see ``docs/architecture.md`` "Remote tool approval". So this binds
``127.0.0.1`` only, on its own ephemeral port never advertised anywhere
except the harness subprocess's own env (``DSH_APPROVAL_CALLBACK_URL``,
wired in ``app.py``).

Fail-closed: every path that doesn't produce an explicit approve — an
unknown/malformed callback body, a card tap that never comes, this
gateway's own timeout — resolves to a deny outcome, never silently allows.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# NotifyFn publishes a pending approval request for display (e.g. as a Feishu
# card); it does not itself resolve the decision — resolve() does that.
NotifyFn = Callable[[str, str, str, "dict[str, Any]"], Awaitable[None]]
SessionExistsFn = Callable[[str], bool]

_OUTCOME_ALLOW = "allowed-once"
_OUTCOME_DENY = "rejected"

_ALLOWED_OUTCOMES = frozenset({_OUTCOME_ALLOW, _OUTCOME_DENY, "cancelled"})

# Single source of truth for the callback route, shared between the route
# registration in start() and callback_url below — a caller that builds its
# own callback URL by hand from `.url` (the bare origin) has no compiler or
# route-table check to catch drift if this path ever changes; `callback_url`
# exists so nothing outside this module needs to know the path at all.
_APPROVAL_PATH = "/approval"


class ApprovalGateway:
    """Owns pending approval decisions and the loopback callback server.

    One instance per bridge process. ``notify`` publishes a
    ``tool_approval_request`` broadcast (reusing ``SessionManager``'s
    existing fan-out, so ``Bridge.handle_event`` /
    ``FeishuBridge.send_tool_approval_request`` need no changes to go live);
    ``resolve`` is what ``BridgeManager.handle_tool_decision`` calls once a
    card button is tapped. ``session_exists`` lets an unknown session id
    (a bogus or stale callback) fail fast with a clear log line instead of
    silently waiting out the full timeout.
    """

    def __init__(
        self,
        *,
        notify: NotifyFn,
        session_exists: SessionExistsFn,
        timeout_seconds: float,
        host: str = "127.0.0.1",
    ) -> None:
        self._notify = notify
        self._session_exists = session_exists
        self._timeout_seconds = timeout_seconds
        self._host = host
        self._pending: dict[tuple[str, str], asyncio.Future[str]] = {}
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self.port: int | None = None

    @property
    def timeout_seconds(self) -> float:
        """This gateway's own deny-timeout — read-only, so a caller wiring
        up a relay's fallback timeout (which must run LARGER than this one;
        see ``app.py``'s ``_APPROVAL_RELAY_TIMEOUT_MARGIN_SECONDS``) has a
        single source of truth to derive it from instead of duplicating the
        literal passed to ``__init__``."""
        return self._timeout_seconds

    @property
    def url(self) -> str:
        """Bare origin the gateway listens on — ``http://host:port``, no
        path. For the URL a caller should actually POST tool-approval
        callbacks to, use :attr:`callback_url`."""
        if self.port is None:
            raise RuntimeError("ApprovalGateway.start() has not completed yet")
        return f"http://{self._host}:{self.port}"

    @property
    def callback_url(self) -> str:
        """The full URL ``approval-relay.mjs`` (or any other caller) must
        POST to — what ``DSH_APPROVAL_CALLBACK_URL`` needs to be set to.
        Deliberately distinct from :attr:`url`: a caller that appends the
        route path itself has no check tying it to the actual registered
        route, which is exactly how a prior version of this callback wiring
        silently 404'd every approval request (POSTing to the bare origin,
        never reaching ``_handle_request`` at all)."""
        return f"{self.url}{_APPROVAL_PATH}"

    async def start(self) -> None:
        if self._server is not None:
            return
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        app.post(_APPROVAL_PATH)(self._handle_request)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, 0))
        sock.listen(128)
        self._sock = sock
        self.port = sock.getsockname()[1]

        config = uvicorn.Config(app, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        self._server = server
        self._serve_task = asyncio.create_task(server.serve(sockets=[sock]))
        # serve() flips `started` only once the loop is actually accepting —
        # without waiting for it, a concurrent harness-subprocess spawn could
        # race a socket that's bound but not yet being served.
        while not server.started:
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            await self._serve_task
            self._serve_task = None
        self._server = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.port = None

    def resolve(self, session_id: str, tool_use_id: str, approved: bool) -> bool:
        """Settle a pending decision from a Feishu card tap.

        Returns False when nothing is pending for this key — a stale or
        duplicate tap, or a request this gateway already timed out. Callers
        (``BridgeManager.handle_tool_decision``) treat that as "no longer
        pending" and settle the card accordingly without raising.
        """
        future = self._pending.pop((session_id, tool_use_id), None)
        if future is None or future.done():
            return False
        future.set_result(_OUTCOME_ALLOW if approved else _OUTCOME_DENY)
        return True

    async def _handle_request(self, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"outcome": _OUTCOME_DENY}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"outcome": _OUTCOME_DENY}, status_code=400)

        session_id = body.get("sessionId")
        call_id = body.get("callId")
        tool_name = body.get("toolName")
        reason = body.get("reason")
        if not isinstance(session_id, str) or not session_id:
            return JSONResponse({"outcome": _OUTCOME_DENY}, status_code=400)
        if not isinstance(call_id, str) or not call_id:
            return JSONResponse({"outcome": _OUTCOME_DENY}, status_code=400)
        if not isinstance(tool_name, str) or not tool_name:
            return JSONResponse({"outcome": _OUTCOME_DENY}, status_code=400)

        if not self._session_exists(session_id):
            logger.warning(
                "Approval callback for unknown session %s (tool=%s) — denying",
                session_id,
                tool_name,
            )
            return JSONResponse({"outcome": _OUTCOME_DENY})

        key = (session_id, call_id)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[key] = future
        try:
            await self._notify(
                session_id,
                call_id,
                tool_name,
                {"summary": reason} if isinstance(reason, str) and reason else {},
            )
            outcome = await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.info(
                "Approval request timed out for session=%s tool=%s — denying",
                session_id,
                tool_name,
            )
            outcome = _OUTCOME_DENY
        finally:
            self._pending.pop(key, None)
        if outcome not in _ALLOWED_OUTCOMES:
            outcome = _OUTCOME_DENY
        return JSONResponse({"outcome": outcome})
