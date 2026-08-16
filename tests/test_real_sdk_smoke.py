"""Real-SDK smoke test — the only test in this repo that spends actual
DEEPSEEK_API_KEY quota and spawns the real dsh runtime subprocess. Everything
else in tests/ uses FakeDshBackend / FakeFeishuServer specifically so this is
the exception, not the norm.

Auto-skips when DEEPSEEK_API_KEY isn't set, mirroring the real-CLI smoke-test
convention of the upstream bridge project this was ported from.
"""

from __future__ import annotations

import os

import pytest

from dsh_feishu_bridge.dsh_adapter import DshAdapter, DshAdapterConfig

pytestmark = [
    pytest.mark.real_sdk,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="needs a real DEEPSEEK_API_KEY — set it to run this smoke test",
    ),
]


async def test_real_turn_runs_and_replies(tmp_path):
    adapter = DshAdapter(
        DshAdapterConfig(
            cwd=str(tmp_path),
            session_root=str(tmp_path / "sessions"),
            api_key=os.environ["DEEPSEEK_API_KEY"],
            # `or None`, not a bare `.get()`: a CI env referencing an
            # unconfigured secret (`${{ secrets.DEEPSEEK_BASE_URL }}` with no
            # such secret set) sets this var to "" rather than omitting it,
            # and an empty base_url breaks the runtime's request construction
            # outright (reproduced locally against CI run 31948093525) — so
            # treat "set but empty" the same as "unset".
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
        )
    )
    try:
        result = await adapter.run_turn(
            "smoke-test", "Reply with exactly one word: pong"
        )
        assert result.finish_reason == "completed", (
            f"finish_reason={result.finish_reason!r} error={result.error!r}"
        )
        assert result.text.strip(), f"empty reply; error={result.error!r}"
    finally:
        await adapter.close()
