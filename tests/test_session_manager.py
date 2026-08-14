from __future__ import annotations

import asyncio

import pytest

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
