"""Tests for the Feishu bridge (docs/architecture.md).

Outbound is patched at the bridge's own `_send` boundary (which enqueues to
the worker thread) for the fast unit tests, and the low-level HTTP
(`_sync_request`) is mocked in the REST-layer tests — no network is touched
there. `TestOutboundIntegration` is the exception: it points the bridge at a
real local `FakeFeishuServer` and lets the outbound worker thread make real
HTTP calls, asserting on what actually arrived — the "fake Feishu server
asserts outbound" coverage.

We drive `_on_message` / `_on_card_action` with constructed SDK events,
mirroring the source bridge's own test suite: fail-closed allowlist,
card-action integrity (nonce bound to its kind + record fields,
replay/tamper rejected without consuming), one-time nonce, transport config
matrix (incl. half-cred boot failure), the REST layer (token fetch,
refresh-on-invalid, backoff-retry, hard-fail -> None), and start/stop
lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from lark_channel.channel.types import (
    CardActionEvent,
    CardActionPayload,
    Conversation,
    EventOperator,
    Identity,
    InboundMessage,
)

from dsh_feishu_bridge.bridges.feishu import (
    FeishuBridge,
    FeishuConfigError,
    build_feishu_bridge,
)

from .fake_feishu_server import FakeFeishuServer

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _settings(**over):
    base = dict(
        feishu_app_id="cli_x",
        feishu_app_secret="sec",
        feishu_transport="webhook",
        feishu_verification_token="vtok",
        feishu_encrypt_key="ekey",
        feishu_domain="http://127.0.0.1:9",
        feishu_allowed_open_ids=["ou_me"],
        feishu_allowed_chat_ids=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _make_bridge(**over) -> FeishuBridge:
    """A bridge with a stub manager and patched outbound (no network)."""
    manager = SimpleNamespace(
        handle_incoming=AsyncMock(),
        handle_tool_decision=AsyncMock(return_value=True),
        switch_session=AsyncMock(return_value="Switched to session 's1'."),
    )
    bridge = build_feishu_bridge(manager, _settings(**over))
    assert bridge is not None
    bridge._send = AsyncMock(return_value=None)
    return bridge


def _inbound(
    *,
    text: str = "hello",
    chat_id: str = "oc_1",
    chat_type: str = "p2p",
    sender: str = "ou_me",
    mentioned_bot: bool = False,
    thread_id: str | None = None,
    raw_content_type: str = "text",
) -> InboundMessage:
    msg = InboundMessage(
        id="m1",
        create_time=0,
        conversation=Conversation(chat_id=chat_id, chat_type=chat_type, thread_id=thread_id),
        sender=Identity(open_id=sender),
    )
    msg.content_text = text
    msg.body_text = text
    msg.mentioned_bot = mentioned_bot
    msg.raw_content_type = raw_content_type
    return msg


def _card_event(value, *, operator: str = "ou_me", chat_id: str = "oc_1") -> CardActionEvent:
    return CardActionEvent(
        message_id="om_card",
        chat_id=chat_id,
        operator=EventOperator(open_id=operator),
        action=CardActionPayload(value=value, tag="button"),
    )


def _button_value(send_mock, index: int = 0, button: int = 0) -> dict:
    _, message = send_mock.await_args_list[index].args
    card = message["card"]
    action_el = next(e for e in card["elements"] if e["tag"] == "action")
    return action_el["actions"][button]["value"]


# --------------------------------------------------------------------------
# Transport / config matrix
# --------------------------------------------------------------------------


class TestBuildMatrix:
    def test_neither_credential_returns_none(self):
        assert build_feishu_bridge(object(), _settings(feishu_app_id=None, feishu_app_secret=None)) is None

    def test_half_credential_is_boot_failure(self):
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_app_secret=None))
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_app_id=None))

    def test_webhook_without_token_is_boot_failure(self):
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_verification_token=None))

    def test_webhook_without_encrypt_key_is_boot_failure(self):
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_encrypt_key=None))
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_encrypt_key=""))

    def test_ws_transport_without_encrypt_key_is_allowed(self):
        b = build_feishu_bridge(
            object(), _settings(feishu_transport="ws", feishu_verification_token=None, feishu_encrypt_key=None)
        )
        assert b is not None and b.transport == "ws"

    def test_ws_transport_without_token_is_allowed(self):
        b = build_feishu_bridge(
            object(), _settings(feishu_transport="ws", feishu_verification_token=None)
        )
        assert b is not None and b.transport == "ws"

    def test_bad_transport_value_rejected(self):
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_transport="carrier-pigeon"))

    def test_strict_security_enabled(self):
        b = _make_bridge()
        assert b._channel.config.security.is_strict is True

    def test_loopback_domain_disables_env_proxy(self):
        b = _make_bridge(feishu_domain="http://127.0.0.1:9")
        assert b._channel.config.transport.trust_env_proxy is False

    def test_real_domain_honors_env_proxy(self):
        b = _make_bridge(feishu_domain="https://open.feishu.cn")
        assert b._channel.config.transport.trust_env_proxy is None

    def test_loopback_flag_set(self):
        assert _make_bridge(feishu_domain="http://127.0.0.1:9")._loopback is True
        assert _make_bridge(feishu_domain="https://open.feishu.cn")._loopback is False


# --------------------------------------------------------------------------
# Webhook boundary: signature / timestamp / replay verification enforced
# HERE, at the bridge's own boundary, never delegated to the SDK. Snape's
# blocker: an invalid signature + an expired timestamp still returned
# success and reached `manager.handle_incoming` — every test below asserts
# both the response status AND that `_channel.handle_webhook_request` (the
# only path to `handle_incoming`) was never awaited on a rejected request.
# --------------------------------------------------------------------------


def _webhook_sign(body: bytes, *, timestamp: str, nonce: str, encrypt_key: str) -> str:
    return hashlib.sha256(
        (timestamp + nonce + encrypt_key).encode("utf-8") + body
    ).hexdigest()


def _webhook_headers(
    body: bytes,
    *,
    timestamp: str,
    nonce: str,
    encrypt_key: str = "ekey",
    signature: str | None = None,
) -> dict[str, str]:
    sig = (
        signature
        if signature is not None
        else _webhook_sign(body, timestamp=timestamp, nonce=nonce, encrypt_key=encrypt_key)
    )
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": sig,
    }


class TestWebhookVerification:
    BODY = b'{"schema":"2.0","header":{"event_type":"im.message.receive_v1","token":"vtok"}}'

    def _bridge(self) -> FeishuBridge:
        b = _make_bridge()
        # The SDK dispatch itself (decrypt/challenge/routing) is exercised
        # elsewhere in the SDK's own test suite; here we're proving our OWN
        # gate never lets a bad request reach it.
        b._channel.handle_webhook_request = AsyncMock(return_value=(200, b'{"msg":"success"}'))
        return b

    async def test_missing_signature_headers_rejected(self):
        b = self._bridge()
        status, _ = await b.handle_webhook({}, self.BODY)
        assert status != 200
        b._channel.handle_webhook_request.assert_not_awaited()

    async def test_invalid_signature_rejected(self):
        b = self._bridge()
        now = str(int(time.time()))
        headers = _webhook_headers(self.BODY, timestamp=now, nonce="n1", signature="deadbeef")
        status, _ = await b.handle_webhook(headers, self.BODY)
        assert status != 200
        b._channel.handle_webhook_request.assert_not_awaited()

    async def test_wrong_encrypt_key_signature_rejected(self):
        b = self._bridge()
        now = str(int(time.time()))
        headers = _webhook_headers(self.BODY, timestamp=now, nonce="n1", encrypt_key="wrong-key")
        status, _ = await b.handle_webhook(headers, self.BODY)
        assert status != 200
        b._channel.handle_webhook_request.assert_not_awaited()

    async def test_expired_timestamp_rejected_even_with_valid_signature(self):
        b = self._bridge()
        old = str(int(time.time()) - 3600)  # 1h old, outside the 5-min window
        headers = _webhook_headers(self.BODY, timestamp=old, nonce="n1")
        status, _ = await b.handle_webhook(headers, self.BODY)
        assert status != 200
        b._channel.handle_webhook_request.assert_not_awaited()

    async def test_future_timestamp_rejected(self):
        b = self._bridge()
        future = str(int(time.time()) + 3600)
        headers = _webhook_headers(self.BODY, timestamp=future, nonce="n1")
        status, _ = await b.handle_webhook(headers, self.BODY)
        assert status != 200
        b._channel.handle_webhook_request.assert_not_awaited()

    async def test_invalid_signature_and_expired_timestamp_rejected(self):
        """Snape's exact repro path: invalid signature + a stale timestamp
        must be rejected, and `handle_incoming` must never fire."""
        b = self._bridge()
        old = str(int(time.time()) - 3600)
        headers = _webhook_headers(
            self.BODY, timestamp=old, nonce="n1", signature="not-a-real-signature"
        )
        status, _ = await b.handle_webhook(headers, self.BODY)
        assert status != 200
        b._channel.handle_webhook_request.assert_not_awaited()
        b.manager.handle_incoming.assert_not_awaited()

    async def test_replayed_timestamp_and_nonce_rejected(self):
        b = self._bridge()
        now = str(int(time.time()))
        headers = _webhook_headers(self.BODY, timestamp=now, nonce="n1")
        status1, _ = await b.handle_webhook(headers, self.BODY)
        assert status1 == 200
        status2, _ = await b.handle_webhook(headers, self.BODY)
        assert status2 != 200
        assert b._channel.handle_webhook_request.await_count == 1

    async def test_same_timestamp_different_nonce_is_not_a_replay(self):
        b = self._bridge()
        now = str(int(time.time()))
        h1 = _webhook_headers(self.BODY, timestamp=now, nonce="n1")
        h2 = _webhook_headers(self.BODY, timestamp=now, nonce="n2")
        s1, _ = await b.handle_webhook(h1, self.BODY)
        s2, _ = await b.handle_webhook(h2, self.BODY)
        assert s1 == 200 and s2 == 200
        assert b._channel.handle_webhook_request.await_count == 2

    async def test_valid_request_passes_through_to_sdk(self):
        b = self._bridge()
        now = str(int(time.time()))
        headers = _webhook_headers(self.BODY, timestamp=now, nonce="n1")
        status, content = await b.handle_webhook(headers, self.BODY)
        assert status == 200
        assert content == b'{"msg":"success"}'
        b._channel.handle_webhook_request.assert_awaited_once_with(headers, self.BODY)

    async def test_header_lookup_is_case_insensitive(self):
        b = self._bridge()
        now = str(int(time.time()))
        headers = _webhook_headers(self.BODY, timestamp=now, nonce="n1")
        lowered = {k.lower(): v for k, v in headers.items()}
        status, _ = await b.handle_webhook(lowered, self.BODY)
        assert status == 200


# --------------------------------------------------------------------------
# Inbound: fail-closed authorization + routing
# --------------------------------------------------------------------------


class TestInbound:
    async def test_empty_allowlist_rejects_everyone(self):
        b = _make_bridge(feishu_allowed_open_ids=[])
        await b._on_message(_inbound(sender="ou_anyone"))
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_not_awaited()  # silent drop, no presence leak

    async def test_unauthorized_sender_rejected(self):
        b = _make_bridge()
        await b._on_message(_inbound(sender="ou_stranger"))
        b.manager.handle_incoming.assert_not_awaited()

    async def test_authorized_p2p_text_routes(self):
        b = _make_bridge()
        await b._on_message(_inbound(text="do a thing", chat_id="oc_p2p"))
        b.manager.handle_incoming.assert_awaited_once_with(
            "feishu", "oc_p2p", "do a thing", b
        )

    async def test_group_without_mention_ignored(self):
        b = _make_bridge()
        await b._on_message(_inbound(chat_type="group", mentioned_bot=False))
        b.manager.handle_incoming.assert_not_awaited()

    async def test_group_with_mention_routes(self):
        b = _make_bridge()
        await b._on_message(
            _inbound(text="summarize", chat_type="group", mentioned_bot=True, chat_id="oc_grp")
        )
        b.manager.handle_incoming.assert_awaited_once_with(
            "feishu", "oc_grp", "summarize", b
        )

    async def test_group_chat_allowlist_enforced(self):
        b = _make_bridge(feishu_allowed_chat_ids=["oc_ok"])
        await b._on_message(
            _inbound(chat_type="group", mentioned_bot=True, chat_id="oc_blocked")
        )
        b.manager.handle_incoming.assert_not_awaited()
        await b._on_message(
            _inbound(text="hi", chat_type="group", mentioned_bot=True, chat_id="oc_ok")
        )
        b.manager.handle_incoming.assert_awaited_once()

    async def test_thread_message_rejected_with_notice(self):
        b = _make_bridge()
        await b._on_message(_inbound(thread_id="omt_123"))
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_awaited_once()

    async def test_non_text_rejected_with_notice(self):
        b = _make_bridge()
        await b._on_message(_inbound(raw_content_type="image", text=""))
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_awaited_once()

    async def test_bare_mention_empty_text_nudges(self):
        b = _make_bridge()
        await b._on_message(_inbound(text="", chat_type="group", mentioned_bot=True))
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_awaited_once()


# --------------------------------------------------------------------------
# Card actions: nonce one-time use + operator authorization
# --------------------------------------------------------------------------


class TestCardActions:
    async def test_approval_button_carries_full_identity(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {"command": "ls"})
        value = _button_value(b._send)
        assert value["session_id"] == "sess-A"
        assert value["tool_use_id"] == "tu-1"
        assert value["action"] == "approve"
        assert value["nonce"]

    async def test_approve_click_routes_then_nonce_consumed(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {"command": "ls"})
        value = _button_value(b._send)

        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "sess-A", "tu-1", True
        )

        b.manager.handle_tool_decision.reset_mock()
        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_allow_and_deny_have_independent_nonces(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        allow = _button_value(b._send, button=0)
        deny = _button_value(b._send, button=1)
        assert allow["action"] == "approve" and deny["action"] == "deny"
        assert allow["nonce"] and deny["nonce"] and allow["nonce"] != deny["nonce"]
        assert b._nonces[allow["nonce"]]["action"] == "approve"
        assert b._nonces[deny["nonce"]]["action"] == "deny"

    async def test_deny_click_routes(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        value = _button_value(b._send, button=1)
        assert value["action"] == "deny"
        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "sess-A", "tu-1", False
        )
        b.manager.handle_tool_decision.reset_mock()
        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_unauthorized_operator_rejected(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        value = _button_value(b._send)
        await b._on_card_action(_card_event(value, operator="ou_intruder"))
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_unknown_nonce_rejected(self):
        b = _make_bridge()
        await b._on_card_action(
            _card_event({"action": "approve", "session_id": "s", "tool_use_id": "t", "nonce": "forged"})
        )
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_switch_button_carries_nonce_and_routes_once(self):
        b = _make_bridge()
        await b.send_session_list(
            "oc_1", [{"id": "s1", "name": "One", "status": "idle", "current": False}]
        )
        value = _button_value(b._send)
        assert value["action"] == "switch" and value["session_id"] == "s1" and value["nonce"]

        await b._on_card_action(_card_event(value))
        b.manager.switch_session.assert_awaited_once_with("feishu", "oc_1", "s1")

        b.manager.switch_session.reset_mock()
        await b._on_card_action(_card_event(value))
        b.manager.switch_session.assert_not_awaited()

    async def test_approval_applied_false_settles_as_no_longer_pending(self):
        # Exercises the "record.kind == approval" path end to end with the
        # real BridgeManager.handle_tool_decision stub (always False, since
        # dsh v1 has no approval flow) instead of a mock — the card should
        # still settle without raising.
        from dsh_feishu_bridge.bridges.manager import BridgeManager
        from dsh_feishu_bridge.session_manager import SessionManager

        from .fakes import FakeDshBackend

        real_manager = BridgeManager(SessionManager(FakeDshBackend()))
        bridge = build_feishu_bridge(real_manager, _settings())
        bridge._send = AsyncMock(return_value=None)
        bridge._settle_card = Mock()

        await bridge.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        value = _button_value(bridge._send)
        await bridge._on_card_action(_card_event(value))
        bridge._settle_card.assert_called_once()
        assert bridge._settle_card.call_args.args[1] == "No longer pending."


# --------------------------------------------------------------------------
# Outbound surface: failures surfaced, chunking, plain vs card
# --------------------------------------------------------------------------


class TestOutbound:
    async def test_long_text_chunks_into_multiple_cards(self):
        b = _make_bridge()
        await b.send_text("oc_1", "x" * 8000)
        assert b._send.await_count >= 2

    async def test_result_and_error_use_plain_text(self):
        b = _make_bridge()
        await b.send_result("oc_1", 0.01, is_error=False)
        _, message = b._send.await_args.args
        assert "text" in message and "card" not in message
        b._send.reset_mock()
        await b.send_error("oc_1", "nope")
        _, message = b._send.await_args.args
        assert "text" in message

    async def test_agent_text_uses_card(self):
        b = _make_bridge()
        await b.send_text("oc_1", "**bold**")
        _, message = b._send.await_args.args
        assert "card" in message

    async def test_status_noise_suppressed(self):
        b = _make_bridge()
        await b.send_status("oc_1", "running")
        b._send.assert_not_awaited()
        await b.send_status("oc_1", "some meaningful status")
        b._send.assert_awaited_once()


# --------------------------------------------------------------------------
# REST layer: the worker-thread `_api_sync` — token fetch, refresh-on-invalid,
# backoff-retry on transient codes, hard-fail -> None.
# --------------------------------------------------------------------------

_TOKEN_OK = {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
_MSG_OK = {"code": 0, "data": {"message_id": "om_1"}}


def _http_bridge(responses: list) -> FeishuBridge:
    manager = SimpleNamespace()
    bridge = build_feishu_bridge(manager, _settings())
    assert bridge is not None
    bridge._sync_request = Mock(side_effect=responses)
    return bridge


class TestApiLayer:
    def test_fetches_token_then_posts(self):
        b = _http_bridge([_TOKEN_OK, _MSG_OK])
        assert b._api_sync("POST", "/open-apis/im/v1/messages", {"x": 1}) == _MSG_OK
        urls = [c.args[1] for c in b._sync_request.call_args_list]
        assert "tenant_access_token" in urls[0]
        assert "/im/v1/messages" in urls[1]

    def test_invalid_token_refreshes_and_retries(self):
        b = _http_bridge([_TOKEN_OK, {"code": 99991663, "msg": "invalid"}, _TOKEN_OK, _MSG_OK])
        assert b._api_sync("POST", "/x", {}) == _MSG_OK
        assert b._sync_request.call_count == 4

    def test_transient_code_backs_off_and_retries(self):
        b = _http_bridge([_TOKEN_OK, {"code": 230020, "msg": "rate"}, _MSG_OK])
        assert b._api_sync("POST", "/x", {}) == _MSG_OK

    def test_hard_error_returns_none(self):
        b = _http_bridge([_TOKEN_OK, {"code": 99999, "msg": "nope"}])
        assert b._api_sync("POST", "/x", {}) is None

    def test_sync_request_exception_returns_none(self):
        b = _http_bridge([_TOKEN_OK, RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
        assert b._api_sync("POST", "/x", {}) is None

    def test_token_is_cached_across_calls(self):
        b = _http_bridge([_TOKEN_OK, _MSG_OK, _MSG_OK])
        b._api_sync("POST", "/x", {})
        b._api_sync("POST", "/y", {})
        urls = [c.args[1] for c in b._sync_request.call_args_list]
        assert sum("tenant_access_token" in u for u in urls) == 1

    def test_token_fetch_failure_aborts(self):
        b = _http_bridge([{"code": 99991400, "msg": "bad app"}])
        assert b._api_sync("POST", "/open-apis/im/v1/messages", {}) is None
        assert all("/im/v1/messages" not in c.args[1] for c in b._sync_request.call_args_list)

    async def test_send_enqueues_text_not_blocking(self):
        b = _http_bridge([])
        await b._send("oc_1", {"text": "hi"})
        method, path, body = b._out_q.get_nowait()
        assert method == "POST" and "/im/v1/messages" in path
        assert body["receive_id"] == "oc_1" and body["msg_type"] == "text"
        b._sync_request.assert_not_called()

    async def test_send_card_enqueues_interactive(self):
        b = _http_bridge([])
        await b._send("oc_1", {"card": {"elements": []}})
        _, _, body = b._out_q.get_nowait()
        assert body["msg_type"] == "interactive"

    def test_loopback_sync_request_disables_env_proxy(self, monkeypatch):
        import dsh_feishu_bridge.bridges.feishu as feishu_mod
        captured = {}

        class _FakeResp:
            def json(self):
                return _MSG_OK

        class _FakeSession:
            def __init__(self):
                self.trust_env = True

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def request(self, *a, **k):
                captured["trust_env"] = self.trust_env
                return _FakeResp()

        monkeypatch.setattr(feishu_mod, "_is_loopback", lambda d: True)
        b = build_feishu_bridge(SimpleNamespace(), _settings())
        monkeypatch.setattr("requests.Session", _FakeSession)
        b._sync_request("POST", "http://127.0.0.1:9/x", {}, {})
        assert captured["trust_env"] is False


# --------------------------------------------------------------------------
# Outbound integration: a REAL local HTTP server stands in for
# open.feishu.cn. No mocking below `_send` — the outbound worker thread makes
# real requests and we assert on what the fake server actually recorded.
# --------------------------------------------------------------------------


class TestOutboundIntegration:
    @pytest.fixture
    def fake_server(self):
        server = FakeFeishuServer()
        server.start()
        yield server
        server.stop()

    async def test_send_text_reaches_fake_server(self, fake_server: FakeFeishuServer):
        manager = SimpleNamespace()
        bridge = build_feishu_bridge(manager, _settings(feishu_domain=fake_server.base_url))
        assert bridge is not None
        await bridge._ensure_worker()
        try:
            await bridge.send_text("oc_1", "hello from the bridge")

            async def _sent() -> bool:
                return len(fake_server.sent) > 0

            for _ in range(100):
                if await _sent():
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("outbound never reached the fake server")

            posted = [item for item in fake_server.sent if item["method"] == "POST"]
            assert posted, "no POST /messages recorded"
            assert posted[0]["receive_id"] == "oc_1"
            assert posted[0]["msg_type"] == "interactive"
        finally:
            bridge._worker_stop.set()
            if bridge._worker is not None:
                await asyncio.to_thread(bridge._worker.join, 2.0)

    async def test_card_approval_and_settle_reach_fake_server(
        self, fake_server: FakeFeishuServer
    ):
        manager = SimpleNamespace(
            handle_tool_decision=AsyncMock(return_value=True),
        )
        bridge = build_feishu_bridge(manager, _settings(feishu_domain=fake_server.base_url))
        assert bridge is not None
        await bridge._ensure_worker()
        try:
            await bridge.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {"command": "ls"})

            for _ in range(100):
                if fake_server.sent:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("approval card never reached the fake server")

            posted = [item for item in fake_server.sent if item["method"] == "POST"]
            assert posted[0]["msg_type"] == "interactive"

            # Now approve it — the settle PATCH should also land.
            allow_nonce = next(iter(bridge._nonces))
            record = bridge._nonces[allow_nonce]
            value = {
                "action": record["action"],
                "session_id": record["session_id"],
                "tool_use_id": record["tool_use_id"],
                "nonce": allow_nonce,
            }
            await bridge._on_card_action(_card_event(value))

            for _ in range(100):
                if any(item["method"] == "PATCH" for item in fake_server.sent):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("settle PATCH never reached the fake server")
        finally:
            bridge._worker_stop.set()
            if bridge._worker is not None:
                await asyncio.to_thread(bridge._worker.join, 2.0)


# --------------------------------------------------------------------------
# Card-action integrity: the nonce record is trusted, the card `value` is
# not. EVERY identity field of the click (action, session, tool) must match
# the nonce record; execution uses the RECORD's fields. Any mismatch is
# rejected WITHOUT consuming the nonce (so a legit later click still works).
# --------------------------------------------------------------------------


class TestCardActionSecurity:
    async def _mint_switch(self, b) -> dict:
        await b.send_session_list(
            "oc_1", [{"id": "s1", "name": "One", "status": "idle", "current": False}]
        )
        return _button_value(b._send)

    async def _mint_approval(self, b) -> dict:
        await b.send_tool_approval_request("oc_1", "REAL-sess", "REAL-tool", "Bash", {})
        return _button_value(b._send)

    async def test_switch_nonce_cannot_be_replayed_as_approval(self):
        b = _make_bridge()
        switch_value = await self._mint_switch(b)
        forged = {"action": "approve", "session_id": "victim-sess",
                  "tool_use_id": "victim-tool", "nonce": switch_value["nonce"]}
        await b._on_card_action(_card_event(forged))
        b.manager.handle_tool_decision.assert_not_awaited()
        await b._on_card_action(_card_event(switch_value))
        b.manager.switch_session.assert_awaited_once_with("feishu", "oc_1", "s1")

    async def test_switch_nonce_action_tamper_rejected(self):
        b = _make_bridge()
        switch_value = await self._mint_switch(b)
        for bad_action in ("approve", "deny"):
            forged = {**switch_value, "action": bad_action}
            await b._on_card_action(_card_event(forged))
        b.manager.switch_session.assert_not_awaited()
        b.manager.handle_tool_decision.assert_not_awaited()
        assert switch_value["nonce"] in b._nonces
        await b._on_card_action(_card_event(switch_value))
        b.manager.switch_session.assert_awaited_once_with("feishu", "oc_1", "s1")

    async def test_approval_missing_action_rejected(self):
        b = _make_bridge()
        appr = await self._mint_approval(b)
        forged = {k: v for k, v in appr.items() if k != "action"}
        await b._on_card_action(_card_event(forged))
        b.manager.handle_tool_decision.assert_not_awaited()
        assert appr["nonce"] in b._nonces
        await b._on_card_action(_card_event(appr))
        b.manager.handle_tool_decision.assert_awaited_once()

    async def test_approval_nonce_cannot_be_replayed_as_switch(self):
        b = _make_bridge()
        appr = await self._mint_approval(b)
        forged = {"action": "switch", "session_id": "other", "nonce": appr["nonce"]}
        await b._on_card_action(_card_event(forged))
        b.manager.switch_session.assert_not_awaited()
        await b._on_card_action(_card_event(appr))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "REAL-sess", "REAL-tool", True
        )


# --------------------------------------------------------------------------
# Lifecycle: outbound worker start/stop idempotency
# --------------------------------------------------------------------------


class TestLifecycle:
    async def test_ensure_worker_is_idempotent(self):
        b = _make_bridge()
        await b._ensure_worker()
        first = b._worker
        await b._ensure_worker()
        assert b._worker is first
        b._worker_stop.set()
        await asyncio.to_thread(b._worker.join, 2.0)

    async def test_stop_join_timeout_raises_on_relaunch(self):
        b = _make_bridge()
        b._worker_join_timeout = 0.05
        await b._ensure_worker()
        worker = b._worker

        # Simulate a worker stuck mid-send: flag stop but don't let it exit.
        b._worker_stop.set()
        # The real worker thread will see the stop flag and exit almost
        # immediately (it's idle), so patch is_alive to simulate "stuck".
        worker.is_alive = lambda: True  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await b._ensure_worker()

        # Cleanup: let the real thread (which never actually saw is_alive
        # patched, since the daemon thread ignores it) terminate naturally.
        b._worker_stop.set()
