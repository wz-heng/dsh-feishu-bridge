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
  schema isn't documented for v0.1. So this adapter does not attempt to
  reconstruct incremental assistant text — it posts one reply per turn, once
  the blocking call returns. See README "Limitations".
- Tool approval (``docs/architecture.md`` "Remote tool approval") needed NO
  change here: the runtime's own ``@deepseek-ai/dsh-sdk-jsonrpc-server``
  plugin never sends a server-initiated request over this SDK's stdio
  channel (confirmed by reading its compiled source — its ``handleRequest``
  only recognizes ``initialize``/``session/prompt``/``shutdown``), so a
  pending approval never surfaces through ``HarnessClient``, the high-level
  ``Session`` API, or this blocking ``run()`` call at all. It's relayed
  entirely by a Node cordis plugin composed alongside the SDK server
  (``approval_runtime/``) over an independent loopback HTTP side channel to
  ``ApprovalGateway`` — orthogonal to the turn this call is blocked on,
  which just runs a little longer while a human decides.
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

SDK-version compat shim (added for the ``deepseek-harness-sdk`` 0.1.2a3
canary break, verified 2026-09-02 by installing 0.1.2a3 +
``deepseek-harness-runtime-bin`` 0.1.2a3 into an isolated venv and actually
booting the runtime, not just reading source):

- 0.1.0rc6 through 0.1.1rc1 (the pinned range) all share the OLD
  ``DeepSeekHarnessConfig`` shape this module originally documented:
  ``session_root`` (a JSONL storage directory, implicitly ``./.sessions``
  when unset) and ``cordis`` (a path to a FULL replacement plugin
  composition file, wired through the ``DSH_CORDIS_CONFIG`` env var).
- 0.1.2a3 dropped both in favor of ``dsh_home`` + ``profile`` + ``patches``.
  ``dsh_home`` is a broader "harness home" directory (session JSONLs live
  under ``$dsh_home/sessions``, not directly in it) and is now MANDATORY —
  the SDK never falls back to ``~/.dsh`` implicitly, so omitting it raises
  at runtime start. ``profile`` selects a named, pre-materialized plugin
  composition (built-in default: ``"sdk"``, already a much richer agent
  than the old bundled default — subagents, web tools, skills, a sandboxed
  bash executor instead of the old unconfined one). ``patches`` is a tuple
  of overlay YAML files (id-targeted insert/config-override/disable
  operations — the SAME format this repo's own top-level
  ``cordis.patch.yml`` already uses) applied on top of the selected
  profile.
- ``_build_harness_config`` below probes the INSTALLED SDK's actual
  ``DeepSeekHarnessConfig`` field set via ``inspect.signature`` (cached —
  see ``_harness_config_field_names``) and builds whichever kwarg shape it
  supports, rather than branching on a parsed version number: the harness
  SDK is an explicit "developer preview" that documents breaking changes
  between pre-releases, so a future 0.1.3/0.2 could rename fields again,
  and capability probing keeps working without another code change (the
  SDK canary, ``.github/workflows/canary.yml``, installs the latest
  pre-release unpinned specifically to catch this early).
- For approval mode specifically: rather than replicate the OLD bundled
  default composition (``@deepseek-ai/dsh-agent-spine-demo`` +
  unconfined ``dsh-bash-local``) under the new profile/patch model — which
  a live boot test showed only resolves with undocumented risk (dropping
  the ``@deepseek-ai/dsh-base`` bundle it's normally layered under trips a
  missing ``loader`` service dependency warning) — the new-SDK path adopts
  the runtime's own unmodified default ``"sdk"`` profile and patches in
  ONLY the two first-party approval plugins (``approval_runtime/
  approval.patch.yml``, distinct from the old full-composition
  ``approval_runtime/cordis.yml``). A live ``harness.start()`` against a
  real 0.1.2a3 runtime confirmed this boots cleanly and that
  ``approval-relay.mjs``/``pre-execute-gate.mjs`` need no code changes:
  both already key off stable, version-independent extension points
  (``tools/pre-execute``, ``approval/request``) and a stable tool NAME
  (``'bash'``), not a specific executor plugin — see those files' own
  module docstrings, which already anticipated
  ``@deepseek-ai/dsh-tool-bash`` alongside the old ``dsh-bash-local``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
from deepseek_harness.errors import HarnessError

logger = logging.getLogger(__name__)

# Fallback directory name for the new SDK's mandatory `dsh_home` when the
# caller never configured `session_root` — same spirit as the old bundled
# composition's implicit `./.sessions` default, just under the broader
# "harness home" concept the new SDK introduced (see module docstring).
_DEFAULT_DSH_HOME_DIRNAME = ".dsh-home"


class DshAdapterError(Exception):
    """Raised when a dsh turn fails — subprocess crash, protocol error, or a
    JSON-RPC error from the runtime. Always carries a human-readable message
    safe to show the chat user."""


@dataclass(slots=True)
class DshTurnResult:
    session_id: str
    text: str
    finish_reason: str | None
    # Populated whenever finish_reason isn't ("completed", None): the runtime's
    # own code/message from the last turn/end event's data.reason.error, the
    # same event finish_reason() already reads .kind from. Without this a turn
    # that "soft-fails" (empty text, finish_reason="error") instead of raising
    # is undiagnosable from the outside — see CI run 31948093525, which showed
    # nothing but an empty string.
    error: str | None = None


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
    # Overlay patch files applied on top of the SDK's default profile — the
    # new-SDK equivalent of `cordis` for additive customization (see module
    # docstring's "SDK-version compat shim"). Ignored (and left unset) under
    # an SDK old enough that `cordis` alone is still the full-composition
    # mechanism.
    patches: tuple[str, ...] = ()
    request_timeout_seconds: float | None = 300.0
    env: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=1)
def _harness_config_field_names() -> frozenset[str]:
    """Cached probe of the fields the INSTALLED ``DeepSeekHarnessConfig``
    actually accepts — see module docstring's "SDK-version compat shim".
    Cached because it never changes within a process (the installed SDK
    doesn't change at runtime), and because this makes the probe itself
    trivially free after the first ``DshAdapter`` construction, e.g. under
    ``pytest -q`` where many tests each build their own adapter.
    """
    return frozenset(inspect.signature(DeepSeekHarnessConfig).parameters)


def _default_dsh_home(cwd: str | None) -> str:
    """Fallback ``dsh_home`` for the new SDK when ``session_root`` was never
    configured. Resolved to an absolute path ourselves, against ``cwd``
    (the runtime subprocess's own working directory) — not left as a bare
    relative string, since ``DeepSeekHarnessConfig.dsh_home`` gets resolved
    SDK-side against the Python *interpreter's* cwd
    (``Path(...).expanduser().resolve()`` in ``deepseek_harness.client``),
    which would make the effective location depend on wherever the bridge
    process happened to be launched from.
    """
    base = Path(cwd) if cwd else Path.cwd()
    return str(base.resolve() / _DEFAULT_DSH_HOME_DIRNAME)


def _build_harness_config(config: DshAdapterConfig) -> DeepSeekHarnessConfig:
    """Translate :class:`DshAdapterConfig` into the installed SDK's actual
    ``DeepSeekHarnessConfig`` shape (see module docstring's "SDK-version
    compat shim"). Capability-detected via ``_harness_config_field_names``,
    never branched on a parsed version number.
    """
    fields = _harness_config_field_names()
    kwargs: dict[str, object] = {
        "provider": config.provider,
        "model": config.model,
        "max_tokens": config.max_tokens,
        "api_key": config.api_key,
        "base_url": config.base_url,
        "cwd": config.cwd,
        "request_timeout_seconds": config.request_timeout_seconds,
        "env": dict(config.env),
    }

    if "session_root" in fields:
        kwargs["session_root"] = config.session_root
    if "dsh_home" in fields:
        # Mandatory on this SDK shape — never omit it, even when the caller
        # never configured session_root (see _default_dsh_home).
        kwargs["dsh_home"] = config.session_root or _default_dsh_home(config.cwd)

    if "cordis" in fields:
        kwargs["cordis"] = config.cordis
    elif config.cordis is not None and not config.patches:
        # A caller-supplied full-composition override has no equivalent on
        # this SDK shape — silently dropping it would silently downgrade
        # whatever behavior (e.g. approval mode's fail-closed gate) that
        # composition was providing. Refuse instead.
        #
        # When `patches` IS set alongside `cordis`, the caller (app.py's
        # approval-mode wiring) has already supplied the new-shape
        # equivalent composition — `cordis` here is just the OLD-shape
        # artifact carried along for whichever SDK turns out to be
        # installed, not a request we can't honor. Drop it silently in
        # that case; only `patches` below is applied.
        raise DshAdapterError(
            f"DSH_CORDIS is set to {config.cordis!r}, but the installed "
            "deepseek-harness-sdk no longer accepts a `cordis` kwarg (see "
            "dsh_adapter.py's SDK-version compat shim docstring) — a full "
            "composition override isn't supported against this SDK version. "
            "Drop DSH_CORDIS, or pin to an SDK release that still has it."
        )
    if "patches" in fields and config.patches:
        kwargs["patches"] = config.patches

    return DeepSeekHarnessConfig(**{name: value for name, value in kwargs.items() if name in fields})


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
        # Tracks calls currently blocked inside asyncio.to_thread(harness.run,
        # ...). close() waits for this to drain before closing the harness —
        # cancelling the asyncio Task that awaits a to_thread call does NOT
        # stop the underlying thread, so without this, close() could run
        # harness.close() concurrently with an in-flight harness.run() on the
        # same SDK client (Snape review round 2).
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def _ensure_started(self) -> DeepSeekHarness:
        if self._harness is not None:
            return self._harness
        async with self._start_lock:
            if self._harness is None:
                harness = DeepSeekHarness(_build_harness_config(self._config))
                await asyncio.to_thread(harness.start)
                self._harness = harness
        return self._harness

    async def run_turn(self, session_id: str, text: str) -> DshTurnResult:
        harness = await self._ensure_started()
        loop = asyncio.get_running_loop()
        self._inflight += 1
        self._idle.clear()

        def _blocking_call():
            # Bookkeeping happens HERE, inside the worker thread's own
            # finally, not in a try/finally around the `await` below.
            # SessionManager.shutdown() cancels the awaiting Task before
            # calling close(); cancelling a Task awaiting
            # asyncio.to_thread(...) raises CancelledError in this coroutine
            # immediately, but does NOT stop this thread — harness.run() just
            # keeps running in the background. A finally around the `await`
            # would therefore decrement _inflight (and set _idle) the moment
            # the Task is cancelled, not when the thread actually finishes,
            # letting close() run harness.close() while this call is still
            # live (Snape review round 3). Scheduling the decrement from
            # inside the thread via call_soon_threadsafe ties it to the
            # thread's real completion instead.
            try:
                return harness.run(text, session_id=session_id)
            finally:
                try:
                    loop.call_soon_threadsafe(self._turn_finished)
                except RuntimeError:
                    pass  # event loop already closed; nothing left to update

        try:
            result = await asyncio.to_thread(_blocking_call)
        except HarnessError as exc:
            logger.exception("dsh turn failed for session %s", session_id)
            raise DshAdapterError(str(exc)) from exc
        error_detail = None
        if result.finish_reason not in (None, "completed"):
            error_detail = _turn_end_error_detail(result.events)
        return DshTurnResult(
            session_id=result.session_id,
            text=result.final_response,
            finish_reason=result.finish_reason,
            error=error_detail,
        )

    def _turn_finished(self) -> None:
        """Runs on the event loop (via call_soon_threadsafe from the worker
        thread) once a blocking harness.run() call has actually returned."""
        self._inflight -= 1
        if self._inflight <= 0:
            self._inflight = 0
            self._idle.set()

    async def close(self, *, wait_timeout: float = 30.0) -> None:
        """Close the harness subprocess.

        Waits for any in-flight ``run_turn`` call to finish naturally before
        closing — cancelling the caller's asyncio Task (SessionManager.
        shutdown()) doesn't stop the underlying blocking thread, so closing
        immediately could run ``harness.close()`` concurrently with an
        in-flight ``harness.run()`` on the same SDK client. ``wait_timeout``
        is a bounded escape hatch, not a guarantee: past it we close anyway
        and log loudly, rather than hang shutdown forever on a truly stuck
        call.
        """
        if self._inflight:
            logger.warning(
                "Closing dsh adapter with %d turn(s) still in flight; "
                "waiting up to %ss for them to finish",
                self._inflight,
                wait_timeout,
            )
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=wait_timeout)
            except TimeoutError:
                logger.error(
                    "dsh adapter close() timed out waiting for %d in-flight "
                    "turn(s); closing the harness anyway",
                    self._inflight,
                )
        harness = self._harness
        self._harness = None
        if harness is not None:
            await asyncio.to_thread(harness.close)


def _turn_end_error_detail(events: list[dict]) -> str | None:
    """Pull ``data.reason.error`` out of the last ``turn/end`` event.

    Sibling key to the ``data.reason.kind`` the SDK's own ``finish_reason()``
    helper already commits to reading (deepseek_harness/api.py), so reading it
    here relies on nothing more than that same event shape.
    """
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        error = reason.get("error") if isinstance(reason, dict) else None
        if error is None:
            return None
        if isinstance(error, dict):
            message = error.get("message")
            code = error.get("code")
            if isinstance(message, str) and isinstance(code, str):
                return f"{code}: {message}"
            if isinstance(message, str):
                return message
            if isinstance(code, str):
                return code
        # Unknown shape (a bare string, an int code, ...) — better than
        # silently dropping it, which is the exact gap this function exists
        # to close (Snape review round 1).
        return str(error)
    return None


def new_session_id() -> str:
    return f"feishu-{uuid.uuid4().hex}"
