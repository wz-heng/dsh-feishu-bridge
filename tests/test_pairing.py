"""Unit tests for pairing.py in isolation from the Feishu bridge — code
generation, persistence, and the PairingGate state machine (see
test_bridge_feishu.py::TestPairing for the bridge-integration coverage:
`/pair` routing, group-chat rejection, already-allowlisted notice, etc.)."""

from __future__ import annotations

import asyncio
import json

import pytest

from dsh_feishu_bridge.pairing import (
    OUTCOME_CONSUMED,
    OUTCOME_EXPIRED,
    OUTCOME_INVALID,
    OUTCOME_LOCKED,
    OUTCOME_OK,
    PairingGate,
    generate_code,
    load_paired_open_ids,
)


class TestGenerateCode:
    def test_default_length(self):
        assert len(generate_code()) == 8

    def test_custom_length(self):
        assert len(generate_code(12)) == 12

    def test_excludes_easily_confused_characters(self):
        # 0/O and 1/I are excluded from the alphabet (pairing.py) — L stays,
        # see its comment for why that's not the same kind of ambiguity.
        confusing = set("01OI")
        code = generate_code(200)  # long enough that any excluded char would show up
        assert not (set(code) & confusing)

    def test_randomized(self):
        codes = {generate_code() for _ in range(50)}
        assert len(codes) == 50  # no collisions in 50 draws from this alphabet


class TestLoadPairedOpenIds:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_paired_open_ids(tmp_path / "nope.json") == frozenset()

    def test_corrupt_json_treated_as_empty(self, tmp_path, caplog):
        caplog.set_level("ERROR")
        path = tmp_path / "paired.json"
        path.write_text("{not valid json")
        assert load_paired_open_ids(path) == frozenset()
        assert any("corrupt" in r.getMessage() for r in caplog.records)

    def test_wrong_shape_treated_as_empty(self, tmp_path):
        path = tmp_path / "paired.json"
        path.write_text(json.dumps({"open_ids": "not-a-list"}))
        assert load_paired_open_ids(path) == frozenset()

    def test_non_string_entries_treated_as_empty(self, tmp_path):
        path = tmp_path / "paired.json"
        path.write_text(json.dumps({"open_ids": ["ou_ok", 123]}))
        assert load_paired_open_ids(path) == frozenset()


class TestPairingGate:
    def _gate(self, tmp_path, **over):
        over.setdefault("state_path", tmp_path / "paired.json")
        over.setdefault("max_attempts", 5)
        return PairingGate(**over)

    async def test_correct_code_succeeds_once(self, tmp_path):
        gate = self._gate(tmp_path)
        assert await gate.try_pair("ou_a", gate.code) == OUTCOME_OK
        assert gate.paired_open_ids == {"ou_a"}
        assert gate.active is False

    async def test_success_persists_to_state_path(self, tmp_path):
        path = tmp_path / "paired.json"
        gate = self._gate(tmp_path, state_path=path)
        await gate.try_pair("ou_a", gate.code)
        assert load_paired_open_ids(path) == frozenset({"ou_a"})
        assert json.loads(path.read_text()) == {"open_ids": ["ou_a"]}

    async def test_wrong_code_does_not_consume(self, tmp_path):
        gate = self._gate(tmp_path)
        assert await gate.try_pair("ou_a", "wrongwrong") == OUTCOME_INVALID
        assert gate.paired_open_ids == set()
        assert gate.active is True

    async def test_consumed_code_rejects_further_attempts(self, tmp_path):
        gate = self._gate(tmp_path)
        await gate.try_pair("ou_a", gate.code)
        assert await gate.try_pair("ou_b", gate.code) == OUTCOME_CONSUMED
        assert "ou_b" not in gate.paired_open_ids

    async def test_max_attempts_locks(self, tmp_path):
        gate = self._gate(tmp_path, max_attempts=3)
        for _ in range(2):
            assert await gate.try_pair("ou_a", "nope") == OUTCOME_INVALID
        assert gate.active is True
        assert await gate.try_pair("ou_a", "nope") == OUTCOME_INVALID
        assert gate.active is False
        # Locked — even the real code is rejected now.
        assert await gate.try_pair("ou_a", gate.code) == OUTCOME_LOCKED

    async def test_locked_event_is_logged_without_the_code(self, tmp_path, caplog):
        caplog.set_level("WARNING")
        gate = self._gate(tmp_path, max_attempts=1)
        await gate.try_pair("ou_a", "nope")
        assert any("locked" in r.getMessage() for r in caplog.records)
        for r in caplog.records:
            assert gate.code not in r.getMessage()

    async def test_expiry(self, tmp_path):
        gate = self._gate(tmp_path, ttl_seconds=1.0)
        gate._minted_at -= 2.0
        assert gate.active is False
        assert await gate.try_pair("ou_a", gate.code) == OUTCOME_EXPIRED

    async def test_load_unions_persisted_ids_at_construction(self, tmp_path):
        path = tmp_path / "paired.json"
        path.write_text(json.dumps({"open_ids": ["ou_old"]}))
        gate = self._gate(tmp_path, state_path=path)
        assert gate.paired_open_ids == {"ou_old"}

    async def test_concurrent_correct_submissions_only_one_succeeds(self, tmp_path):
        """Snape: try_pair's only await (the disk write) used to sit BETWEEN
        the `_consumed` check and setting it — two concurrent submissions of
        the correct code could both pass the check before either write
        finished, both "succeed", and the one-time code would have paired
        two different open_ids. The whole method is now lock-serialized."""
        gate = self._gate(tmp_path)
        outcomes = await asyncio.gather(
            gate.try_pair("ou_a", gate.code),
            gate.try_pair("ou_b", gate.code),
        )
        assert sorted(outcomes) == sorted([OUTCOME_OK, OUTCOME_CONSUMED])
        assert gate.paired_open_ids in ({"ou_a"}, {"ou_b"})  # exactly one winner

    async def test_write_failure_does_not_consume_the_code(self, tmp_path):
        """Snape's repro: point state_path at a directory so the write
        raises IsADirectoryError. Before the fix, `_consumed` was set
        BEFORE the write — the code was burned even though nobody actually
        got paired. Now the round must survive to be retried."""
        state_path = tmp_path / "not-a-file"
        state_path.mkdir()
        gate = PairingGate(state_path=state_path, max_attempts=5)

        with pytest.raises(OSError):
            await gate.try_pair("ou_a", gate.code)

        assert gate.active is True
        assert gate.paired_open_ids == set()

        # A retry (e.g. after the operator fixes the path) can still work —
        # nothing about the round was invalidated by the failed attempt.
        state_path.rmdir()
        assert await gate.try_pair("ou_a", gate.code) == OUTCOME_OK
