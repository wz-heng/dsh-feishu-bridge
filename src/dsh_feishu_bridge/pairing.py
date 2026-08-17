"""One-time pairing code onboarding (README "Getting your open_id" / `/pair`).

Problem this replaces: the only way to get on the allowlist used to be
message the bot, get silently rejected, dig the sender's `open_id` out of
the server log, edit `FEISHU_ALLOWED_OPEN_IDS`, and restart the process.
Instead, the bridge mints one random code at startup and prints it to the
console; the operator hands it to whoever they want to onboard, who sends
`/pair <code>` to the bot in a private chat. A correct code adds their
`open_id` to the in-memory allowlist immediately (no restart) and persists
it to disk (survives the next restart).

Trust boundary is unchanged by this feature: the code is printed to console
stdout only, the same place `.env` already lives — "can read the console"
and "can edit `.env`" are the same operator. A stranger who never sees the
console gets nothing from `/pair` beyond the generic replies below, which
carry no information about whether pairing is on, locked, or expired beyond
what's needed to self-serve or give up.

One round per process: exactly one code is live at a time (no concurrent
codes — see README "Limitations"), consumed by the first correct submission,
irrecoverably retired by `PairingGate.max_attempts` wrong submissions or by
its own TTL. Getting a fresh code means restarting the bridge.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Characters that are easy to mis-type or mis-read (0/O, 1/I) are excluded
# so a human can copy a code off a terminal or read it over voice/chat
# without a transcription error burning one of their limited attempts.
# Uppercase-only (submitted codes are upper()'d before comparing — see
# FeishuBridge._handle_pair_command); L stays in since, on its own with no
# lowercase l around to confuse it with, it isn't actually ambiguous.
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

DEFAULT_CODE_LENGTH = 8
DEFAULT_TTL_SECONDS = 900.0  # 15 minutes
DEFAULT_MAX_ATTEMPTS = 5

# Every outcome _try_pair_locked can return. Distinct terminal reasons (vs.
# one generic "unavailable") are kept internally so tests and log lines can
# tell them apart — the bridge is free to collapse them into one user-facing
# reply.
OUTCOME_OK = "ok"
OUTCOME_INVALID = "invalid"
OUTCOME_EXPIRED = "expired"
OUTCOME_LOCKED = "locked"
OUTCOME_CONSUMED = "consumed"


def generate_code(length: int = DEFAULT_CODE_LENGTH) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def load_paired_open_ids(path: Path) -> frozenset[str]:
    """`open_id`s persisted at `path` from previous successful pairings.

    A missing file is the normal first-boot case (empty set). A present but
    corrupt file is NOT a boot failure the way a bad user-authored config
    file is (`config.py`'s `ConfigError`) — this file is bridge-owned state,
    nobody hand-edits it under normal operation, and refusing to start over
    it would turn a scratched disk sector into a full outage. Log it and
    start empty instead; the next successful `/pair` overwrites it with a
    clean file.
    """
    if not path.is_file():
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = data["open_ids"]
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ValueError("'open_ids' must be a list of strings")
    except Exception:
        logger.error(
            "Feishu pairing: state file %s is unreadable/corrupt — starting "
            "with an empty paired set (env allowlist is unaffected)",
            path,
        )
        return frozenset()
    return frozenset(ids)


def _write_paired_open_ids(path: Path, open_ids: frozenset[str]) -> None:
    """Persist `open_ids` — ONLY ids that came from a successful `/pair`,
    never the env-configured allowlist (see PairingGate) — atomically, so a
    crash mid-write can never leave a truncated/corrupt file for the next
    boot's `load_paired_open_ids` to choke on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"open_ids": sorted(open_ids)}, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


class PairingGate:
    """The single pairing round live for this bridge process.

    Minted with a fresh random code at construction; the caller (FeishuBridge)
    prints `.code` to the console once, at startup. `try_pair` is the only
    way to consume it — success adds the submitted `open_id` to
    `paired_open_ids` and persists that set to `state_path`; repeated
    failures beyond `max_attempts` retire the round for the rest of the
    process's life (fresh code = restart the process).
    """

    def __init__(
        self,
        *,
        state_path: Path,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        code_length: int = DEFAULT_CODE_LENGTH,
    ) -> None:
        self._state_path = state_path
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts
        self.code = generate_code(code_length)
        self._minted_at = time.monotonic()
        self._attempts = 0
        self._locked = False
        self._consumed = False
        # Guards the whole check-then-commit sequence in try_pair: the disk
        # write below is the only await in that sequence, so without this
        # lock two concurrent submissions of the CORRECT code could both
        # pass the (still-False) `_consumed` check before either write
        # finishes, and both go on to "succeed" — a one-time code used
        # twice. Serializing the method closes that window; see try_pair.
        self._lock = asyncio.Lock()
        # Ids from previous successful pairings ONLY — never the env
        # allowlist (the caller unions the two in memory; this set is what
        # gets written back to state_path).
        self.paired_open_ids: set[str] = set(load_paired_open_ids(state_path))

    @property
    def _expired(self) -> bool:
        return time.monotonic() - self._minted_at >= self.ttl_seconds

    @property
    def active(self) -> bool:
        """True while this round can still accept a submission — false once
        consumed, locked out, or past its TTL."""
        return not self._consumed and not self._locked and not self._expired

    async def try_pair(self, open_id: str, submitted_code: str) -> str:
        """Attempt to redeem `submitted_code` for `open_id`.

        Returns one of the ``OUTCOME_*`` constants; never raises on bad
        input, and never logs `submitted_code` or `self.code` on any path —
        the printed console line is the code's only appearance anywhere.

        The whole check-then-commit sequence runs under `self._lock`. Two
        reasons, both found by review rather than a passing test suite:
        concurrent submissions of the correct code must not both succeed
        (only one `await` — the disk write — sits between the check and the
        commit, which is exactly the window a lock needs to close), and a
        failed write must not burn the one-time code: `_consumed` /
        `paired_open_ids` are only updated AFTER `_write_paired_open_ids`
        returns, so a write error (disk full, bad permissions) propagates
        with the round still active for a retry, instead of silently
        locking a user out with nothing to show for it.
        """
        async with self._lock:
            if self._consumed:
                return OUTCOME_CONSUMED
            if self._locked:
                return OUTCOME_LOCKED
            if self._expired:
                return OUTCOME_EXPIRED

            # Constant-time: a naive `==` short-circuits on the first
            # mismatched character, letting a network-timing attacker
            # recover the code position by position.
            if hmac.compare_digest(self.code, submitted_code):
                updated = frozenset(self.paired_open_ids | {open_id})
                await asyncio.to_thread(_write_paired_open_ids, self._state_path, updated)
                self._consumed = True
                self.paired_open_ids.add(open_id)
                return OUTCOME_OK

            self._attempts += 1
            if self._attempts >= self.max_attempts:
                self._locked = True
                logger.warning(
                    "Feishu pairing: %d invalid attempts reached — pairing "
                    "locked until the bridge restarts",
                    self.max_attempts,
                )
            return OUTCOME_INVALID
