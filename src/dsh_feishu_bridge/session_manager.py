"""Sticky-session bookkeeping and turn orchestration on top of a DshBackend.

This plays the role Owlery's ``SessionManager`` plays for its bridges, but
scoped to what one dsh backend actually offers (see dsh_adapter.py module
docstring): no multi-agent registry, no mid-turn incremental streaming, no
tool-approval flow. One session == one dsh conversation, addressed by the
same id on both sides.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from .dsh_adapter import DshAdapterError, DshBackend

logger = logging.getLogger(__name__)

BroadcastHandler = Callable[[dict[str, Any]], Awaitable[None]]


class SessionStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class BridgeSession:
    id: str
    name: str
    owner: str | None = None  # "platform:chat_id" of the chat that created it
    created_at: float = field(default_factory=time.time)
    status: SessionStatus = SessionStatus.IDLE
    message_count: int = 0
    working_dir: str = "."


class SessionManager:
    """Owns sessions and fans out their turn events to registered listeners
    (the ``BridgeManager`` in practice — see its ``_on_broadcast``).

    A turn runs as a detached asyncio task: ``start_message`` returns as soon
    as the task is scheduled, matching the fire-and-forget contract
    ``FeishuBridge._on_message`` relies on (it must not block the chat's own
    inbound handler on a full turn).
    """

    def __init__(self, backend: DshBackend) -> None:
        self._backend = backend
        self._sessions: dict[str, BridgeSession] = {}
        self._listeners: dict[str, BroadcastHandler] = {}
        self._tasks: set[asyncio.Task] = set()
        # One lock per dsh session id, so two fast successive messages to the
        # same session serialize instead of both calling backend.run_turn()
        # concurrently — the SDK's Session.run() only filters notifications
        # by session id, so two overlapping calls on the same id would race
        # to collect each other's events (see docs/architecture.md).
        self._session_locks: dict[str, asyncio.Lock] = {}

    # --- session CRUD ---

    async def create_session(
        self, name: str | None = None, *, owner: str | None = None
    ) -> BridgeSession:
        session_id = f"feishu-{uuid.uuid4().hex}"
        session = BridgeSession(id=session_id, name=name or session_id, owner=owner)
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str | None) -> BridgeSession | None:
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[BridgeSession]:
        return list(self._sessions.values())

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    # --- broadcast ---

    def on_broadcast(self, key: str, handler: BroadcastHandler) -> None:
        self._listeners[key] = handler

    def remove_broadcast(self, key: str) -> None:
        self._listeners.pop(key, None)

    async def _broadcast(self, session_id: str, event: dict[str, Any]) -> None:
        event = {"session_id": session_id, **event}
        for key, handler in list(self._listeners.items()):
            try:
                await handler(event)
            except Exception:
                logger.exception("Broadcast listener %s failed", key)

    # --- turns ---

    async def start_message(self, session_id: str, text: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Unknown session '{session_id}'")
        task = asyncio.create_task(self._run_turn(session, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_turn(self, session: BridgeSession, text: str) -> None:
        # Serialize turns for THIS session; a different session's lock is
        # independent, so concurrent chats still run concurrently.
        async with self._lock_for(session.id):
            session.status = SessionStatus.RUNNING
            session.message_count += 1
            await self._broadcast(session.id, {"type": "status", "status": "running"})
            try:
                result = await self._backend.run_turn(session.id, text)
            except Exception as exc:
                # Broad on purpose (not just DshAdapterError): the SDK's
                # low-level client can also raise a bare TimeoutError, and any
                # other unexpected exception must still resolve the chat back
                # to idle rather than leaving it stuck at "running" forever.
                # CancelledError (shutdown()) is a BaseException, not an
                # Exception, so it still propagates through this handler.
                logger.exception("dsh turn failed for session %s", session.id)
                session.status = SessionStatus.ERROR
                message = str(exc) if isinstance(exc, DshAdapterError) else f"Internal error: {exc}"
                await self._broadcast(session.id, {"type": "error", "message": message})
                await self._broadcast(
                    session.id, {"type": "result", "cost": None, "is_error": True}
                )
                await self._broadcast(session.id, {"type": "status", "status": "idle"})
                return

            is_error = result.finish_reason not in (None, "completed")
            if result.text:
                await self._broadcast(
                    session.id, {"type": "assistant_text", "content": result.text}
                )
            if is_error and not result.text:
                await self._broadcast(
                    session.id,
                    {
                        "type": "error",
                        "message": f"Turn ended without a reply (reason: {result.finish_reason}).",
                    },
                )
            session.status = SessionStatus.ERROR if is_error else SessionStatus.IDLE
            await self._broadcast(
                session.id, {"type": "result", "cost": None, "is_error": is_error}
            )
            await self._broadcast(session.id, {"type": "status", "status": "idle"})

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # This cancels promptly for a turn still queued behind another
            # turn's per-session lock, but NOT for one already inside
            # backend.run_turn() — cancelling the awaiting asyncio Task
            # doesn't stop the underlying blocking thread. That residual race
            # (this close() call racing a still-running harness.run() thread)
            # is closed one layer down, in DshAdapter.close(), which tracks
            # in-flight calls and waits for them before actually closing the
            # harness.
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._backend.close()
