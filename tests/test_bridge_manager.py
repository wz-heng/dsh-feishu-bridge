from __future__ import annotations

import asyncio

import pytest

from dsh_feishu_bridge.bridges.base import Bridge
from dsh_feishu_bridge.bridges.manager import BridgeManager
from dsh_feishu_bridge.session_manager import SessionManager

from .fakes import FakeDshBackend


class MockBridge(Bridge):
    name = "mock"

    def __init__(self, manager=None) -> None:
        super().__init__(manager=manager)
        self.sent: list[tuple[str, str, dict]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append(("send_text", chat_id, {"text": text}))

    async def send_tool_approval_request(self, chat_id, session_id, tool_use_id, tool_name, tool_input):
        self.sent.append(("send_tool_approval_request", chat_id, {}))

    async def send_tool_use(self, chat_id, tool_name, tool_input):
        self.sent.append(("send_tool_use", chat_id, {}))

    async def send_tool_result(self, chat_id, output, is_error):
        self.sent.append(("send_tool_result", chat_id, {}))

    async def send_status(self, chat_id, status):
        self.sent.append(("send_status", chat_id, {"status": status}))

    async def send_result(self, chat_id, cost, is_error):
        self.sent.append(("send_result", chat_id, {"is_error": is_error}))

    async def send_error(self, chat_id, message):
        self.sent.append(("send_error", chat_id, {"message": message}))

    async def send_session_list(self, chat_id, sessions, note=None):
        self.sent.append(("send_session_list", chat_id, {"sessions": sessions, "note": note}))


async def _drain(mgr: SessionManager, bridge: MockBridge | None = None) -> None:
    """Await pending turn tasks, then force-flush the bridge's TextBuffer —
    Bridge.handle_event buffers assistant_text and flushes on a 0.5s idle
    timer (base.py), which a fast test can't just outrun."""
    tasks = list(mgr._tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if bridge is not None:
        for buf in list(bridge._text_buffers.values()):
            await buf.flush()


@pytest.fixture
def backend() -> FakeDshBackend:
    return FakeDshBackend(reply="pong")


@pytest.fixture
def session_mgr(backend: FakeDshBackend) -> SessionManager:
    return SessionManager(backend)


@pytest.fixture
def manager(session_mgr: SessionManager) -> BridgeManager:
    m = BridgeManager(session_mgr)
    m.register_broadcast()
    return m


@pytest.fixture
def bridge(manager: BridgeManager) -> MockBridge:
    b = MockBridge(manager)
    manager.register_bridge(b)
    return b


class TestHandleIncoming:
    async def test_first_message_creates_sticky_session(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        sid = manager.get_session_id("mock", "c1")
        assert sid is not None
        assert session_mgr.get_session(sid) is not None
        texts = [s for s in bridge.sent if s[0] == "send_text"]
        assert any("pong" in t[2]["text"] for t in texts)

    async def test_second_message_reuses_sticky_session(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        sid1 = manager.get_session_id("mock", "c1")
        await manager.handle_incoming("mock", "c1", "again", bridge)
        await _drain(session_mgr, bridge)
        assert manager.get_session_id("mock", "c1") == sid1

    async def test_different_chats_get_different_sessions(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await manager.handle_incoming("mock", "c2", "hello", bridge)
        await _drain(session_mgr, bridge)
        sid1 = manager.get_session_id("mock", "c1")
        sid2 = manager.get_session_id("mock", "c2")
        assert sid1 != sid2

    async def test_quiet_by_default_suppresses_status_and_result(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        kinds = {s[0] for s in bridge.sent}
        assert kinds == {"send_text"}  # only the reply — no status/result noise

    async def test_verbose_shows_status_and_result(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        manager.set_verbose("mock", "c1", True)
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        kinds = {s[0] for s in bridge.sent}
        assert "send_status" in kinds
        assert "send_result" in kinds


class TestCommands:
    async def test_new_creates_named_session(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "/new work thread", bridge)
        sid = manager.get_session_id("mock", "c1")
        assert session_mgr.get_session(sid).name == "work thread"

    async def test_new_rolls_the_sticky_pointer(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        sid1 = manager.get_session_id("mock", "c1")
        await manager.handle_incoming("mock", "c1", "/new", bridge)
        sid2 = manager.get_session_id("mock", "c1")
        assert sid1 != sid2

    async def test_sessions_empty(self, manager: BridgeManager, bridge: MockBridge):
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        assert bridge.sent[-1][0] == "send_text"
        assert "No sessions" in bridge.sent[-1][2]["text"]

    async def test_sessions_lists_with_current_flag(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        listing = [s for s in bridge.sent if s[0] == "send_session_list"][-1]
        items = listing[2]["sessions"]
        assert len(items) == 1
        assert items[0]["current"] is True

    async def test_switch_to_unknown_session(self, manager: BridgeManager, bridge: MockBridge):
        await manager.handle_incoming("mock", "c1", "/switch nope", bridge)
        assert "not found" in bridge.sent[-1][2]["text"]

    async def test_switch_to_existing_session(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        s = await session_mgr.create_session()
        await manager.handle_incoming("mock", "c1", f"/switch {s.id}", bridge)
        assert manager.get_session_id("mock", "c1") == s.id

    async def test_current_no_session(self, manager: BridgeManager, bridge: MockBridge):
        await manager.handle_incoming("mock", "c1", "/current", bridge)
        assert "No session connected" in bridge.sent[-1][2]["text"]

    async def test_current_with_session(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        await manager.handle_incoming("mock", "c1", "/current", bridge)
        assert "Status:" in bridge.sent[-1][2]["text"]

    async def test_quiet_verbose_toggle(self, manager: BridgeManager, bridge: MockBridge):
        await manager.handle_incoming("mock", "c1", "/verbose", bridge)
        assert manager._binding("mock", "c1").verbose is True
        await manager.handle_incoming("mock", "c1", "/quiet", bridge)
        assert manager._binding("mock", "c1").verbose is False

    async def test_help_lists_commands(self, manager: BridgeManager, bridge: MockBridge):
        await manager.handle_incoming("mock", "c1", "/help", bridge)
        assert "/new" in bridge.sent[-1][2]["text"]


class TestSessionOwnership:
    """A session created by one chat must not be listable or switchable by
    another chat — Snape's blocker: process-global /sessions + no ownership
    check on /switch let any allowlisted chat hijack another chat's session
    and start receiving its broadcasts."""

    async def test_sessions_only_shows_own(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        await manager.handle_incoming("mock", "c2", "hello", bridge)
        await _drain(session_mgr, bridge)

        bridge.sent.clear()
        await manager.handle_incoming("mock", "c1", "/sessions", bridge)
        listing = [s for s in bridge.sent if s[0] == "send_session_list"][-1]
        ids = {item["id"] for item in listing[2]["sessions"]}
        assert ids == {manager.get_session_id("mock", "c1")}

    async def test_switch_to_other_chats_session_rejected(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        owned_by_c1 = manager.get_session_id("mock", "c1")

        await manager.handle_incoming("mock", "c2", f"/switch {owned_by_c1}", bridge)
        assert "not found" in bridge.sent[-1][2]["text"]
        assert manager.get_session_id("mock", "c2") is None

    async def test_switch_card_button_also_enforces_ownership(
        self, manager: BridgeManager, bridge: MockBridge, session_mgr: SessionManager
    ):
        # switch_session() is the shared path for both /switch text and the
        # Feishu card button — exercise it directly like the card handler does.
        await manager.handle_incoming("mock", "c1", "hello", bridge)
        await _drain(session_mgr, bridge)
        owned_by_c1 = manager.get_session_id("mock", "c1")

        msg = await manager.switch_session("mock", "c2", owned_by_c1)
        assert "not found" in msg
        assert manager.get_session_id("mock", "c2") is None


class TestStartAllPropagatesFailure:
    async def test_bridge_start_failure_propagates(self, session_mgr: SessionManager):
        # Snape should-fix: this app registers exactly one bridge, so a
        # swallowed start() failure would leave the process looking healthy
        # while the bot is actually not connected to anything.
        manager = BridgeManager(session_mgr)

        class FailingBridge(MockBridge):
            async def start(self) -> None:
                raise RuntimeError("ws connect failed")

        manager.register_bridge(FailingBridge(manager))
        with pytest.raises(RuntimeError, match="ws connect failed"):
            await manager.start_all()

    async def test_unknown_command(self, manager: BridgeManager, bridge: MockBridge):
        await manager.handle_incoming("mock", "c1", "/bogus", bridge)
        assert "Unknown command" in bridge.sent[-1][2]["text"]

    async def test_tool_decision_always_false(self, manager: BridgeManager):
        applied = await manager.handle_tool_decision("mock", "c1", "s1", "t1", True)
        assert applied is False
