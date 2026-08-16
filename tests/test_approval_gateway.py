"""ApprovalGateway: the loopback HTTP relay between the dsh runtime's
approval-relay cordis plugin and this bridge's Feishu approval-card flow.

Every test talks to a REAL bound loopback socket via httpx (no ASGI
in-process transport) — the whole point of this component is real socket
behavior (a real ephemeral port, a real concurrent request/response cycle
against `resolve()` called from elsewhere), so faking the transport would
test something else.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from dsh_feishu_bridge.approval_gateway import ApprovalGateway


class NotifyRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict]] = []

    async def __call__(self, session_id, tool_use_id, tool_name, tool_input) -> None:
        self.calls.append((session_id, tool_use_id, tool_name, tool_input))


@pytest.fixture
async def gateway():
    notify = NotifyRecorder()
    gw = ApprovalGateway(
        notify=notify,
        session_exists=lambda sid: sid == "known-session",
        timeout_seconds=5.0,
    )
    gw.notify_recorder = notify  # type: ignore[attr-defined]
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()


async def _post(gateway: ApprovalGateway, **overrides) -> httpx.Response:
    body = {
        "sessionId": "known-session",
        "callId": "call-1",
        "toolName": "bash",
        "reason": "ls -la",
        **overrides,
    }
    async with httpx.AsyncClient(trust_env=False) as client:
        return await client.post(f"{gateway.url}/approval", json=body, timeout=10.0)


class TestStartStop:
    async def test_start_binds_a_loopback_ephemeral_port(self, gateway: ApprovalGateway):
        assert gateway.port is not None and gateway.port > 0
        assert gateway.url == f"http://127.0.0.1:{gateway.port}"

    async def test_callback_url_includes_the_actual_registered_route(self, gateway: ApprovalGateway):
        """Regression test: a caller that used the bare `.url` origin as the
        callback URL (POSTing to `/`) would 404 on every single request —
        `_handle_request` never runs, so `resolve()` never has anything
        pending and no card is ever shown, no matter what the caller
        decides. `callback_url` must match the route `start()` actually
        registers, proven by driving a real request through it end to end
        (not just string-comparing the two)."""
        assert gateway.callback_url == f"{gateway.url}/approval"
        async with httpx.AsyncClient(trust_env=False) as client:
            request = asyncio.ensure_future(
                client.post(
                    gateway.callback_url,
                    json={"sessionId": "known-session", "callId": "call-1", "toolName": "bash"},
                    timeout=10.0,
                )
            )
            await _wait_for_pending(gateway, ("known-session", "call-1"))
            assert gateway.resolve("known-session", "call-1", True) is True
            response = await request
        assert response.status_code == 200
        assert response.json() == {"outcome": "allowed-once"}

    async def test_url_before_start_raises(self):
        gw = ApprovalGateway(notify=NotifyRecorder(), session_exists=lambda _sid: True, timeout_seconds=1.0)
        with pytest.raises(RuntimeError):
            _ = gw.url

    async def test_callback_url_before_start_raises(self):
        gw = ApprovalGateway(notify=NotifyRecorder(), session_exists=lambda _sid: True, timeout_seconds=1.0)
        with pytest.raises(RuntimeError):
            _ = gw.callback_url

    async def test_start_is_idempotent(self, gateway: ApprovalGateway):
        port_before = gateway.port
        await gateway.start()
        assert gateway.port == port_before


class TestApprovalFlow:
    async def test_approve_resolves_allowed_once(self, gateway: ApprovalGateway):
        request = asyncio.ensure_future(_post(gateway))
        await _wait_for_pending(gateway, ("known-session", "call-1"))
        assert gateway.resolve("known-session", "call-1", True) is True
        response = await request
        assert response.json() == {"outcome": "allowed-once"}

    async def test_deny_resolves_rejected(self, gateway: ApprovalGateway):
        request = asyncio.ensure_future(_post(gateway))
        await _wait_for_pending(gateway, ("known-session", "call-1"))
        assert gateway.resolve("known-session", "call-1", False) is True
        response = await request
        assert response.json() == {"outcome": "rejected"}

    async def test_notify_called_with_summary_before_decision(self, gateway: ApprovalGateway):
        request = asyncio.ensure_future(_post(gateway, reason="rm -rf /tmp/x"))
        await _wait_for_pending(gateway, ("known-session", "call-1"))
        assert gateway.notify_recorder.calls == [
            ("known-session", "call-1", "bash", {"summary": "rm -rf /tmp/x"})
        ]
        gateway.resolve("known-session", "call-1", True)
        await request

    async def test_resolve_false_when_nothing_pending(self, gateway: ApprovalGateway):
        assert gateway.resolve("known-session", "no-such-call", True) is False

    async def test_double_resolve_second_call_is_a_no_op(self, gateway: ApprovalGateway):
        request = asyncio.ensure_future(_post(gateway))
        await _wait_for_pending(gateway, ("known-session", "call-1"))
        assert gateway.resolve("known-session", "call-1", True) is True
        assert gateway.resolve("known-session", "call-1", True) is False
        await request


class TestFailClosed:
    async def test_unknown_session_denies_without_notifying(self, gateway: ApprovalGateway):
        response = await _post(gateway, sessionId="ghost-session")
        assert response.json() == {"outcome": "rejected"}
        assert gateway.notify_recorder.calls == []

    async def test_missing_fields_returns_400_and_denies(self, gateway: ApprovalGateway):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(f"{gateway.url}/approval", json={"sessionId": "known-session"})
        assert response.status_code == 400
        assert response.json() == {"outcome": "rejected"}

    async def test_malformed_json_returns_400_and_denies(self, gateway: ApprovalGateway):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"{gateway.url}/approval",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 400
        assert response.json() == {"outcome": "rejected"}

    async def test_timeout_denies_and_clears_pending(self):
        gw = ApprovalGateway(
            notify=NotifyRecorder(), session_exists=lambda _sid: True, timeout_seconds=0.05
        )
        await gw.start()
        try:
            response = await _post(gw)
            assert response.json() == {"outcome": "rejected"}
            # The gateway's own timeout already cleared it — a late card tap
            # (or a duplicate) is a no-op, not a crash.
            assert gw.resolve("known-session", "call-1", True) is False
        finally:
            await gw.stop()

    async def test_stop_denies_in_flight_requests_promptly(self):
        gw = ApprovalGateway(
            notify=NotifyRecorder(), session_exists=lambda _sid: True, timeout_seconds=30.0
        )
        await gw.start()
        request = asyncio.ensure_future(_post(gw))
        await _wait_for_pending(gw, ("known-session", "call-1"))
        await asyncio.wait_for(gw.stop(), timeout=5.0)
        # Whatever the exact transport-level outcome of an in-flight request
        # racing server shutdown, it must resolve promptly rather than hang
        # until the (here, 30s) approval timeout.
        try:
            await asyncio.wait_for(request, timeout=5.0)
        except (httpx.HTTPError, asyncio.CancelledError):
            pass


async def _wait_for_pending(gateway: ApprovalGateway, key: tuple[str, str], *, timeout: float = 5.0) -> None:
    async def _poll():
        while key not in gateway._pending:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)
