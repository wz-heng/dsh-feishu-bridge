"""Real-SDK smoke test — the only test in this repo that spends actual
DEEPSEEK_API_KEY quota and spawns the real dsh runtime subprocess. Everything
else in tests/ uses FakeDshBackend / FakeFeishuServer specifically so this is
the exception, not the norm.

Auto-skips when DEEPSEEK_API_KEY isn't set, mirroring the convention Owlery's
own test_*_real.py suite uses for its real-CLI smoke tests.
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
            base_url=os.environ.get("DEEPSEEK_BASE_URL"),
        )
    )
    try:
        result = await adapter.run_turn(
            "smoke-test", "Reply with exactly one word: pong"
        )
        assert result.text.strip()
        assert result.finish_reason == "completed"
    finally:
        await adapter.close()
