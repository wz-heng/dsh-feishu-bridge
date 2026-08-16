"""Real-SDK approval-mode smoke test — spends actual DEEPSEEK_API_KEY quota
and spawns the real dsh runtime subprocess against the bundled
``approval_runtime/cordis.yml`` composition. Everything else covering
approval mode (``test_approval_gateway.py``, ``test_bridge_manager.py``,
``tests-node/approval/*.test.mjs``) uses fakes; this is the one place that
verifies the Phase 1 recon's reverse-engineered assumptions about the
compiled runtime binary's actual wire behavior (tool name `"bash"`, the
`tools/pre-execute` → `approval/request` → HTTP relay chain) against the
real thing rather than trusting decompiled source alone — see
``docs/architecture.md`` "Remote tool approval".

Auto-skips when DEEPSEEK_API_KEY isn't set, mirroring
``test_real_sdk_smoke.py``.
"""

from __future__ import annotations

import os

import pytest

from dsh_feishu_bridge.approval_gateway import ApprovalGateway
from dsh_feishu_bridge.approval_runtime import bundled_cordis_path
from dsh_feishu_bridge.dsh_adapter import DshAdapter, DshAdapterConfig

pytestmark = [
    pytest.mark.real_sdk,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="needs a real DEEPSEEK_API_KEY — set it to run this smoke test",
    ),
]


async def _make_adapter(tmp_path, gateway: ApprovalGateway) -> DshAdapter:
    await gateway.start()
    return DshAdapter(
        DshAdapterConfig(
            cwd=str(tmp_path),
            session_root=str(tmp_path / "sessions"),
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
            cordis=str(bundled_cordis_path()),
            request_timeout_seconds=120.0,
            env={
                "DSH_APPROVAL_CALLBACK_URL": gateway.url,
                "DSH_APPROVAL_TIMEOUT_MS": "60000",
            },
        )
    )


async def test_approved_bash_call_actually_executes(tmp_path):
    """Auto-approve on notify() and prove the command RAN via a sentinel
    file — not by reading the model's own prose about what happened (Snape
    review: the original version of this test never resolved the pending
    approval at all, so it only ever exercised the timeout/deny path)."""
    sentinel = tmp_path / "allow-sentinel"

    async def notify(session_id, tool_use_id, tool_name, tool_input):
        gateway.resolve(session_id, tool_use_id, True)

    gateway = ApprovalGateway(
        notify=notify, session_exists=lambda _sid: True, timeout_seconds=60.0
    )
    adapter = await _make_adapter(tmp_path, gateway)
    try:
        result = await adapter.run_turn(
            "approval-smoke-allow",
            f"Use your bash tool to run exactly: touch {sentinel.name}. "
            "Then reply with exactly one word: done.",
        )
        assert result.finish_reason == "completed", (
            f"finish_reason={result.finish_reason!r} error={result.error!r}"
        )
        assert sentinel.exists(), "approved bash call should have created the sentinel file"
    finally:
        await adapter.close()
        await gateway.stop()


async def test_denied_bash_call_never_executes(tmp_path):
    """No auto-approve wired in — the gateway's own timeout denies the call,
    so the sentinel file the command would have created must NOT exist."""
    sentinel = tmp_path / "deny-sentinel"
    gateway = ApprovalGateway(
        notify=_noop_notify, session_exists=lambda _sid: True, timeout_seconds=2.0
    )
    adapter = await _make_adapter(tmp_path, gateway)
    try:
        result = await adapter.run_turn(
            "approval-smoke-deny",
            f"Use your bash tool to run exactly: touch {sentinel.name}. "
            "Then tell me in one sentence whether the command ran or was denied.",
        )
        assert result.finish_reason == "completed", (
            f"finish_reason={result.finish_reason!r} error={result.error!r}"
        )
        assert not sentinel.exists(), "denied bash call must never have executed"
    finally:
        await adapter.close()
        await gateway.stop()


async def _noop_notify(session_id, tool_use_id, tool_name, tool_input) -> None:
    pass
