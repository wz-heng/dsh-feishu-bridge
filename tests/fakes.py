"""Test doubles shared across the suite."""

from __future__ import annotations

from dataclasses import dataclass, field

from dsh_feishu_bridge.dsh_adapter import DshAdapterError, DshTurnResult


@dataclass
class FakeDshBackend:
    """A scripted DshBackend: no subprocess, no network, no API quota.

    ``reply`` is returned verbatim for every turn unless a session id has a
    scripted override in ``replies_by_session`` (a list consumed in order) or
    is present in ``errors_by_session`` (raises instead of returning).
    """

    reply: str = "ok"
    finish_reason: str = "completed"
    error: str | None = None
    replies_by_session: dict[str, list[str]] = field(default_factory=dict)
    errors_by_session: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    closed: bool = False

    async def run_turn(self, session_id: str, text: str) -> DshTurnResult:
        self.calls.append((session_id, text))
        if session_id in self.errors_by_session:
            raise DshAdapterError(self.errors_by_session[session_id])
        queued = self.replies_by_session.get(session_id)
        if queued:
            text_out = queued.pop(0)
        else:
            text_out = self.reply
        return DshTurnResult(
            session_id=session_id,
            text=text_out,
            finish_reason=self.finish_reason,
            error=self.error,
        )

    async def close(self) -> None:
        self.closed = True
