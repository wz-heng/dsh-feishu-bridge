"""Adapter tests never spawn the real dsh runtime subprocess or start it —
``DeepSeekHarness`` itself is monkeypatched with an in-process stub that
mimics its synchronous ``start()`` / ``run()`` / ``close()`` contract
(python/sdk/README.md @ deepseek-ai/deepseek-harness, verified 2026-08-14).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

import pytest
from deepseek_harness.errors import TransportClosedError

from dsh_feishu_bridge import dsh_adapter as dsh_adapter_module
from dsh_feishu_bridge.dsh_adapter import DshAdapter, DshAdapterConfig, DshAdapterError


@dataclass
class _StubRunResult:
    session_id: str
    final_response: str
    finish_reason: str | None
    events: list[dict] = field(default_factory=list)


class _StubHarness:
    """Records start/run/close calls; one instance per DshAdapter (patched
    class is called once per adapter, matching the real lazy-start contract).
    """

    instances: list["_StubHarness"] = []

    def __init__(self, config) -> None:
        self.config = config
        self.started = False
        self.closed = False
        self.runs: list[tuple[str, str]] = []
        self.next_error: Exception | None = None
        self.next_finish_reason = "completed"
        self.next_events: list[dict] = []
        _StubHarness.instances.append(self)

    def start(self) -> None:
        self.started = True

    def run(self, text: str, *, session_id: str):
        self.runs.append((session_id, text))
        if self.next_error is not None:
            raise self.next_error
        return _StubRunResult(
            session_id=session_id,
            final_response=f"echo: {text}",
            finish_reason=self.next_finish_reason,
            events=self.next_events,
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def patched_harness(monkeypatch):
    _StubHarness.instances.clear()
    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarness", _StubHarness)
    yield _StubHarness


async def test_run_turn_starts_harness_lazily_once():
    adapter = DshAdapter(DshAdapterConfig())
    assert _StubHarness.instances == []
    await adapter.run_turn("s1", "hi")
    await adapter.run_turn("s1", "again")
    assert len(_StubHarness.instances) == 1
    assert _StubHarness.instances[0].started is True


async def test_run_turn_maps_result_fields():
    adapter = DshAdapter(DshAdapterConfig())
    result = await adapter.run_turn("s1", "hello")
    assert result.session_id == "s1"
    assert result.text == "echo: hello"
    assert result.finish_reason == "completed"
    assert result.error is None


async def test_run_turn_wraps_harness_error():
    adapter = DshAdapter(DshAdapterConfig())
    await adapter.run_turn("warmup", "x")  # start the stub
    _StubHarness.instances[0].next_error = TransportClosedError("runtime died")
    with pytest.raises(DshAdapterError, match="runtime died"):
        await adapter.run_turn("s1", "hello")


async def test_run_turn_extracts_error_detail_from_turn_end_event():
    # Matches the real runtime's own turn/end event shape (data.reason.error),
    # reproduced locally against deepseek-harness-sdk 0.1.0rc6 by forcing an
    # empty DEEPSEEK_BASE_URL: {"kind": "error", "error": {"code": "TRANSPORT",
    # "message": "DeepSeek API request to  failed"}}.
    adapter = DshAdapter(DshAdapterConfig())
    await adapter.run_turn("warmup", "x")  # start the stub
    stub = _StubHarness.instances[0]
    stub.next_finish_reason = "error"
    stub.next_events = [
        {
            "type": "turn/end",
            "data": {
                "reason": {
                    "kind": "error",
                    "error": {
                        "message": "DeepSeek API request to  failed",
                        "code": "TRANSPORT",
                    },
                },
            },
        },
    ]

    result = await adapter.run_turn("s1", "hello")

    assert result.finish_reason == "error"
    assert result.error == "TRANSPORT: DeepSeek API request to  failed"


async def test_run_turn_error_detail_falls_back_to_str_for_unknown_shapes():
    # Snape review round 1: a well-shaped {code, message} dict isn't the only
    # possible shape data.reason.error could take — nothing in the SDK's
    # contract guarantees it. Anything short of silently returning None would
    # have caught CI run 31948093525 faster than a bare finish_reason.
    adapter = DshAdapter(DshAdapterConfig())
    await adapter.run_turn("warmup", "x")  # start the stub
    stub = _StubHarness.instances[0]
    stub.next_finish_reason = "error"
    stub.next_events = [
        {"type": "turn/end", "data": {"reason": {"kind": "error", "error": "boom"}}}
    ]

    result = await adapter.run_turn("s1", "hello")

    assert result.error == "boom"


async def test_run_turn_error_detail_uses_code_when_message_is_not_a_string():
    adapter = DshAdapter(DshAdapterConfig())
    await adapter.run_turn("warmup", "x")  # start the stub
    stub = _StubHarness.instances[0]
    stub.next_finish_reason = "error"
    stub.next_events = [
        {
            "type": "turn/end",
            "data": {"reason": {"kind": "error", "error": {"code": "TRANSPORT"}}},
        }
    ]

    result = await adapter.run_turn("s1", "hello")

    assert result.error == "TRANSPORT"


async def test_run_turn_error_detail_absent_when_no_turn_end_error_key():
    adapter = DshAdapter(DshAdapterConfig())
    await adapter.run_turn("warmup", "x")  # start the stub
    stub = _StubHarness.instances[0]
    stub.next_finish_reason = "max-tokens"
    stub.next_events = [{"type": "turn/end", "data": {"reason": {"kind": "max-tokens"}}}]

    result = await adapter.run_turn("s1", "hello")

    assert result.finish_reason == "max-tokens"
    assert result.error is None


async def test_close_allows_restart():
    adapter = DshAdapter(DshAdapterConfig())
    await adapter.run_turn("s1", "hi")
    await adapter.close()
    assert _StubHarness.instances[0].closed is True
    await adapter.run_turn("s1", "hi again")
    assert len(_StubHarness.instances) == 2


async def test_config_forwarded_to_harness_config():
    config = DshAdapterConfig(provider="p", model="m", max_tokens=123, api_key="k")
    adapter = DshAdapter(config)
    await adapter.run_turn("s1", "hi")
    forwarded = _StubHarness.instances[0].config
    assert forwarded.provider == "p"
    assert forwarded.model == "m"
    assert forwarded.max_tokens == 123
    assert forwarded.api_key == "k"


# --------------------------------------------------------------------------
# close() vs. an in-flight run_turn() — Snape review round 2: cancelling the
# asyncio Task awaiting run_turn() doesn't stop the underlying OS thread, so
# close() must wait for it rather than close the harness out from under it.
# These use a REAL background thread (via asyncio.to_thread inside run_turn)
# gated by threading.Event, since the point is to prove close() actually
# blocks on genuine in-flight thread work, not just an awaited coroutine.
# --------------------------------------------------------------------------


async def test_close_waits_for_inflight_turn_before_closing_harness(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    class SlowHarness(_StubHarness):
        def run(self, text: str, *, session_id: str):
            started.set()
            release.wait(timeout=2.0)
            order.append("run_returned")
            return super().run(text, session_id=session_id)

        def close(self) -> None:
            order.append("closed")
            super().close()

    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarness", SlowHarness)
    adapter = DshAdapter(DshAdapterConfig())

    run_task = asyncio.create_task(adapter.run_turn("s1", "hello"))
    await asyncio.to_thread(started.wait, 2.0)
    assert started.is_set(), "run() never started on its worker thread"

    close_task = asyncio.create_task(adapter.close(wait_timeout=2.0))
    await asyncio.sleep(0.05)  # let close() reach its wait — it must not
    assert order == [], "close() closed the harness while a turn was still running"

    release.set()
    await run_task
    await close_task
    assert order == ["run_returned", "closed"]


async def test_close_waits_even_when_the_run_task_was_cancelled_first(monkeypatch):
    # This is the path SessionManager.shutdown() actually exercises: it
    # cancels the awaiting Task BEFORE calling close(). Round-2's fix tracked
    # in-flight state via a try/finally around the `await` in run_turn() —
    # but cancelling that await raises CancelledError in run_turn()
    # immediately, running its finally right away, even though the
    # underlying worker thread is still inside harness.run(). That let
    # close() proceed and call harness.close() while the thread was still
    # live. The fix (round 3) ties the in-flight bookkeeping to the thread's
    # OWN completion via call_soon_threadsafe, so it must survive the task
    # being cancelled out from under it.
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    class SlowHarness(_StubHarness):
        def run(self, text: str, *, session_id: str):
            started.set()
            release.wait(timeout=2.0)
            order.append("run_returned")
            return super().run(text, session_id=session_id)

        def close(self) -> None:
            order.append("closed")
            super().close()

    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarness", SlowHarness)
    adapter = DshAdapter(DshAdapterConfig())

    run_task = asyncio.create_task(adapter.run_turn("s1", "hello"))
    await asyncio.to_thread(started.wait, 2.0)
    assert started.is_set(), "run() never started on its worker thread"

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert order == [], "cancelling the task must not itself unblock close()"

    close_task = asyncio.create_task(adapter.close(wait_timeout=2.0))
    await asyncio.sleep(0.05)
    assert order == [], "close() closed the harness before the cancelled turn's thread actually finished"

    release.set()
    await close_task
    assert order == ["run_returned", "closed"]


async def test_close_force_closes_after_timeout_instead_of_hanging(monkeypatch):
    release = threading.Event()  # deliberately never set within the test

    class StuckHarness(_StubHarness):
        def run(self, text: str, *, session_id: str):
            release.wait(timeout=5.0)
            return super().run(text, session_id=session_id)

    monkeypatch.setattr(dsh_adapter_module, "DeepSeekHarness", StuckHarness)
    adapter = DshAdapter(DshAdapterConfig())
    run_task = asyncio.create_task(adapter.run_turn("s1", "hi"))
    await asyncio.sleep(0.05)  # let it enter the blocking call

    # Bounded timeout is an escape hatch, not a guarantee — close() must
    # still return promptly rather than hang shutdown forever.
    await asyncio.wait_for(adapter.close(wait_timeout=0.1), timeout=1.0)

    release.set()  # let the stuck thread finish so it doesn't outlive the test
    await asyncio.wait_for(run_task, timeout=2.0)
