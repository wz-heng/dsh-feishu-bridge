"""The one module allowed to import ``deepseek_harness``.

deepseek-harness-sdk is a v0.1 developer preview that documents breaking
changes between releases (pyproject.toml pins an exact version). Every other
module in this package talks to :class:`DshBackend`, never to the SDK
directly, so a contract break only ever needs a fix here.

SDK shape that drives this design (python/sdk/README.md, docs/user/guide/
python-sdk.md @ deepseek-ai/deepseek-harness master, verified 2026-08-14):

- ``DeepSeekHarness`` owns ONE lazily-started runtime subprocess (JSON-RPC
  over stdio) and is meant to be reused across calls — "Reusable synchronous
  SDK for running DeepSeek Harness agent turns."
- ``harness.run(text, session_id=...)`` is a **synchronous, blocking** call:
  it returns only once that turn's session goes idle. There is no
  token-level streaming callback in the public contract — ``on_notification``
  receives raw protocol notifications while the call blocks, but their event
  schema isn't documented for v0.1 and the high-level ``Session`` wrapper
  doesn't surface server-initiated requests (e.g. a hypothetical approval
  prompt) at all, only the low-level ``HarnessClient`` does. So this adapter
  does not attempt to reconstruct incremental assistant text — it posts one
  reply per turn, once the blocking call returns. See README "Limitations".
- Multiple sessions can run concurrently against the SAME subprocess: the
  wire protocol filters notifications by session id. So the bridge process
  holds exactly one ``DeepSeekHarness`` and gives each chat its own session
  id, instead of spawning one runtime subprocess per chat.
- Session continuity ("sticky") is confirmed only *within* one
  ``DeepSeekHarness`` instance: reusing a session id against the same
  long-lived subprocess continues that conversation and its owned Bash
  process. Cross-restart resume via ``session_root``'s JSONL log is
  mentioned but not documented as a supported resume path, so this adapter
  does not rely on it — a bridge restart starts fresh sessions on purpose.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
from deepseek_harness.errors import HarnessError

logger = logging.getLogger(__name__)


class DshAdapterError(Exception):
    """Raised when a dsh turn fails — subprocess crash, protocol error, or a
    JSON-RPC error from the runtime. Always carries a human-readable message
    safe to show the chat user."""


@dataclass(slots=True)
class DshTurnResult:
    session_id: str
    text: str
    finish_reason: str | None


class DshBackend(Protocol):
    """What the session manager needs from a dsh backend. ``DshAdapter`` is
    the real implementation; tests substitute a canned fake so they never
    spawn the runtime subprocess or spend API quota."""

    async def run_turn(self, session_id: str, text: str) -> DshTurnResult: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class DshAdapterConfig:
    """Subprocess-wide dsh configuration — one bridge process, one model
    config. Per-chat state is only ever a session id (see SessionManager)."""

    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    max_tokens: int | None = None
    api_key: str | None = None
    base_url: str | None = None
    cwd: str | None = None
    session_root: str | None = None
    cordis: str | None = None
    request_timeout_seconds: float | None = 300.0
    env: dict[str, str] = field(default_factory=dict)


class DshAdapter:
    """Owns the single long-lived ``DeepSeekHarness`` subprocess for the
    bridge process's lifetime. ``run_turn`` is a blocking SDK call, so it
    always runs on a worker thread via ``asyncio.to_thread`` — the SDK's
    internal client is thread-safe (its own reader/writer locks), so
    concurrent chats calling ``run_turn`` at once is safe.
    """

    def __init__(self, config: DshAdapterConfig) -> None:
        self._config = config
        self._harness: DeepSeekHarness | None = None
        self._start_lock = asyncio.Lock()

    async def _ensure_started(self) -> DeepSeekHarness:
        if self._harness is not None:
            return self._harness
        async with self._start_lock:
            if self._harness is None:
                harness = DeepSeekHarness(
                    DeepSeekHarnessConfig(
                        provider=self._config.provider,
                        model=self._config.model,
                        max_tokens=self._config.max_tokens,
                        api_key=self._config.api_key,
                        base_url=self._config.base_url,
                        cwd=self._config.cwd,
                        session_root=self._config.session_root,
                        cordis=self._config.cordis,
                        request_timeout_seconds=self._config.request_timeout_seconds,
                        env=dict(self._config.env),
                    )
                )
                await asyncio.to_thread(harness.start)
                self._harness = harness
        return self._harness

    async def run_turn(self, session_id: str, text: str) -> DshTurnResult:
        harness = await self._ensure_started()
        try:
            result = await asyncio.to_thread(
                harness.run, text, session_id=session_id
            )
        except HarnessError as exc:
            logger.exception("dsh turn failed for session %s", session_id)
            raise DshAdapterError(str(exc)) from exc
        return DshTurnResult(
            session_id=result.session_id,
            text=result.final_response,
            finish_reason=result.finish_reason,
        )

    async def close(self) -> None:
        harness = self._harness
        self._harness = None
        if harness is not None:
            await asyncio.to_thread(harness.close)


def new_session_id() -> str:
    return f"feishu-{uuid.uuid4().hex}"
