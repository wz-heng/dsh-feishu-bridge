"""Adapter tests never spawn the real dsh runtime subprocess or start it —
``DeepSeekHarness`` itself is monkeypatched with an in-process stub that
mimics its synchronous ``start()`` / ``run()`` / ``close()`` contract
(python/sdk/README.md @ deepseek-ai/deepseek-harness, verified 2026-08-14).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from deepseek_harness.errors import TransportClosedError

from dsh_feishu_bridge import dsh_adapter as dsh_adapter_module
from dsh_feishu_bridge.dsh_adapter import DshAdapter, DshAdapterConfig, DshAdapterError


@dataclass
class _StubRunResult:
    session_id: str
    final_response: str
    finish_reason: str | None


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


async def test_run_turn_wraps_harness_error():
    adapter = DshAdapter(DshAdapterConfig())
    await adapter.run_turn("warmup", "x")  # start the stub
    _StubHarness.instances[0].next_error = TransportClosedError("runtime died")
    with pytest.raises(DshAdapterError, match="runtime died"):
        await adapter.run_turn("s1", "hello")


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
