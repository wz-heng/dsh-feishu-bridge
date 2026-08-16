from __future__ import annotations

import asyncio

import pytest

from dsh_feishu_bridge.dsh_adapter import DshTurnResult
from dsh_feishu_bridge.session_manager import SessionManager, SessionStatus

from .fakes import FakeDshBackend


async def _drain(mgr: SessionManager) -> None:
    tasks = list(mgr._tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def backend() -> FakeDshBackend:
    return FakeDshBackend(reply="hi there")


@pytest.fixture
def events() -> list[dict]:
    return []


@pytest.fixture
async def mgr(backend: FakeDshBackend, events: list[dict]) -> SessionManager:
    async def record(event: dict) -> None:
        events.append(event)

    m = SessionManager(backend)
    m.on_broadcast("test", record)
    return m


class TestSessionCRUD:
    async def test_create_session_unique_ids(self, mgr: SessionManager):
        a = await mgr.create_session()
        b = await mgr.create_session()
        assert a.id != b.id
        assert mgr.get_session(a.id) is a
        assert mgr.get_session("nope") is None

    async def test_create_session_custom_name(self, mgr: SessionManager):
        s = await mgr.create_session(name="my thread")
        assert s.name == "my thread"

    async def test_list_sessions(self, mgr: SessionManager):
        a = await mgr.create_session()
        b = await mgr.create_session()
        assert {s.id for s in mgr.list_sessions()} == {a.id, b.id}


class TestTurns:
    async def test_unknown_session_raises(self, mgr: SessionManager):
        with pytest.raises(ValueError):
            await mgr.start_message("nope", "hi")

    async def test_success_broadcasts_in_order(
        self, mgr: SessionManager, backend: FakeDshBackend, events: list[dict]
    ):
        session = await mgr.create_session()
        await mgr.start_message(session.id, "hello")
        await _drain(mgr)

        types = [e["type"] for e in events]
        assert types == ["status", "assistant_text", "result", "status"]
        assert events[1]["content"] == "hi there"
        assert events[2]["is_error"] is False
        assert events[0]["status"] == "running"
        assert events[3]["status"] == "idle"
        assert session.status == SessionStatus.IDLE
        assert session.message_count == 1
        assert backend.calls == [(session.id, "hello")]

    async def test_backend_error_broadcasts_error_then_result(
        self, mgr: SessionManager, backend: FakeDshBackend, events: list[dict]
    ):
        session = await mgr.create_session()
        backend.errors_by_session[session.id] = "runtime crashed"
        await mgr.start_message(session.id, "hello")
        await _drain(mgr)

        types = [e["type"] for e in events]
        assert types == ["status", "error", "result", "status"]
        assert events[1]["message"] == "runtime crashed"
        assert events[2]["is_error"] is True
        assert session.status == SessionStatus.ERROR

    async def test_non_completed_finish_reason_without_text_is_error(
        self, mgr: SessionManager, backend: FakeDshBackend, events: list[dict]
    ):
        backend.reply = ""
        backend.finish_reason = "max-tokens"
        session = await mgr.create_session()
        await mgr.start_message(session.id, "hello")
        await _drain(mgr)

        types = [e["type"] for e in events]
        assert types == ["status", "error", "result", "status"]
        assert "max-tokens" in events[1]["message"]
        assert events[2]["is_error"] is True

    async def test_error_detail_appended_to_broadcast_message_when_present(
        self, mgr: SessionManager, backend: FakeDshBackend, events: list[dict]
    ):
        backend.reply = ""
        backend.finish_reason = "error"
        backend.error = "TRANSPORT: DeepSeek API request to  failed"
        session = await mgr.create_session()
        await mgr.start_message(session.id, "hello")
        await _drain(mgr)

        types = [e["type"] for e in events]
        assert types == ["status", "error", "result", "status"]
        assert "reason: error" in events[1]["message"]
        assert "TRANSPORT: DeepSeek API request to  failed" in events[1]["message"]

    async def test_second_turn_increments_message_count(
        self, mgr: SessionManager, backend: FakeDshBackend
    ):
        session = await mgr.create_session()
        await mgr.start_message(session.id, "one")
        await _drain(mgr)
        await mgr.start_message(session.id, "two")
        await _drain(mgr)
        assert session.message_count == 2

    async def test_shutdown_closes_backend(
        self, mgr: SessionManager, backend: FakeDshBackend
    ):
        await mgr.shutdown()
        assert backend.closed is True

    async def test_unexpected_exception_still_resolves_to_idle(
        self, mgr: SessionManager, backend: FakeDshBackend, events: list[dict]
    ):
        # Snape should-fix: DshAdapter.run_turn only wraps HarnessError, but
        # the SDK's low-level client can raise a bare TimeoutError too. Any
        # exception type must still resolve the chat back to idle instead of
        # leaving it stuck at "running" forever.
        async def boom(session_id, text):
            raise TimeoutError("dsh runtime timed out")

        backend.run_turn = boom
        session = await mgr.create_session()
        await mgr.start_message(session.id, "hello")
        await _drain(mgr)

        types = [e["type"] for e in events]
        assert types == ["status", "error", "result", "status"]
        assert "dsh runtime timed out" in events[1]["message"]
        assert events[3]["status"] == "idle"
        assert session.status == SessionStatus.ERROR


class TestConcurrency:
    async def test_same_session_turns_are_serialized(self):
        # Snape blocker: two fast messages to the same session must not call
        # backend.run_turn() concurrently — the dsh SDK filters notifications
        # by session id, so overlapping calls on the same id can cross-talk.
        concurrent = 0
        max_concurrent = 0
        order: list[str] = []

        class SlowBackend:
            async def run_turn(self, session_id: str, text: str) -> DshTurnResult:
                nonlocal concurrent, max_concurrent
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
                order.append(f"start:{text}")
                await asyncio.sleep(0.05)
                order.append(f"end:{text}")
                concurrent -= 1
                return DshTurnResult(session_id=session_id, text=text, finish_reason="completed")

            async def close(self) -> None:
                pass

        mgr = SessionManager(SlowBackend())
        session = await mgr.create_session()
        await mgr.start_message(session.id, "one")
        await mgr.start_message(session.id, "two")
        await asyncio.gather(*list(mgr._tasks), return_exceptions=True)

        assert max_concurrent == 1
        assert order == ["start:one", "end:one", "start:two", "end:two"]

    async def test_different_sessions_run_concurrently(self):
        concurrent = 0
        max_concurrent = 0

        class SlowBackend:
            async def run_turn(self, session_id: str, text: str) -> DshTurnResult:
                nonlocal concurrent, max_concurrent
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
                await asyncio.sleep(0.05)
                concurrent -= 1
                return DshTurnResult(session_id=session_id, text=text, finish_reason="completed")

            async def close(self) -> None:
                pass

        mgr = SessionManager(SlowBackend())
        s1 = await mgr.create_session()
        s2 = await mgr.create_session()
        await mgr.start_message(s1.id, "one")
        await mgr.start_message(s2.id, "two")
        await asyncio.gather(*list(mgr._tasks), return_exceptions=True)

        assert max_concurrent == 2  # different sessions are NOT serialized against each other

    async def test_shutdown_does_not_hang_on_in_flight_turn(self):
        class SlowBackend:
            def __init__(self) -> None:
                self.closed = False

            async def run_turn(self, session_id: str, text: str) -> DshTurnResult:
                await asyncio.sleep(10)
                return DshTurnResult(session_id=session_id, text="late", finish_reason="completed")

            async def close(self) -> None:
                self.closed = True

        backend = SlowBackend()
        mgr = SessionManager(backend)
        session = await mgr.create_session()
        await mgr.start_message(session.id, "hello")
        await asyncio.sleep(0)  # let the task start and enter the turn

        await asyncio.wait_for(mgr.shutdown(), timeout=2.0)
        assert backend.closed is True
