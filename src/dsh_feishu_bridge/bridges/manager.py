from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..approval_gateway import ApprovalGateway
    from ..session_manager import SessionManager

from .base import QUIET_SUPPRESSED_EVENTS, Bridge

logger = logging.getLogger(__name__)

BRIDGE_COMMANDS = {
    "/new": "Start a fresh session",
    "/sessions": "List sessions (tap one to switch)",
    "/switch": "Point at an existing session (/switch <session_id>)",
    "/current": "Show current session info",
    "/quiet": "Show only the agent's replies (hide tool activity)",
    "/verbose": "Also show tool activity (result, status)",
    "/help": "Show available commands",
}


@dataclass
class ChatBinding:
    """A chat's in-memory state: the sticky session pointer (the currently
    open thread, nullable) and its output verbosity.

    Not persisted across a bridge restart — a restart already starts fresh
    dsh sessions (dsh_adapter.py module docstring), so there is nothing
    durable to point a sticky pointer at anyway. See README "Limitations".
    """

    session_id: str | None = None
    verbose: bool = False


class BridgeManager:
    """Routes messages between messaging platforms and the SessionManager.

    Responsibilities:
    - Maintains (platform, chat_id) -> ChatBinding (sticky session, verbosity)
    - Handles slash commands (/new, /sessions, /switch, /current, /quiet,
      /verbose, /help)
    - Forwards user messages to SessionManager.start_message()
    - Routes SessionManager events back to the correct bridge + chat
    - Manages bridge lifecycle (start/stop)
    """

    # Most-recent sessions to render as tappable buttons in /sessions. Card
    # button lists are bounded; older sessions stay reachable via /switch <id>.
    SESSION_LIST_LIMIT = 30

    def __init__(
        self,
        session_mgr: "SessionManager",
        approval_gateway: "ApprovalGateway | None" = None,
    ) -> None:
        self.session_mgr = session_mgr
        self._approval_gateway = approval_gateway
        self._bridges: dict[str, Bridge] = {}
        # "platform:chat_id" -> ChatBinding
        self._mappings: dict[str, ChatBinding] = {}

    def register_bridge(self, bridge: Bridge) -> None:
        self._bridges[bridge.name] = bridge

    async def start_all(self) -> None:
        """Start every registered bridge. A failure here is a boot failure,
        not a background warning: this app registers exactly one bridge
        (Feishu), so swallowing its start error would leave the FastAPI
        lifespan reporting a healthy, fully-started process while the bot is
        actually not connected to anything — silent degradation, not
        graceful degradation. Let it propagate so uvicorn fails to boot.

        NOT yet safe for multiple bridges: if a second bridge were registered
        and failed after a first one already started, this raises without
        rolling the first one back — app.py's lifespan `try/finally` only
        wraps `yield`, which starts after this returns. FeishuBridge itself
        rolls back its own partial start on failure (see its `start()`), so
        v1's single-bridge case has no leak; a second bridge type would need
        start_all() itself to stop whatever it already started before
        re-raising.
        """
        for name, bridge in self._bridges.items():
            await bridge.start()
            logger.info("Bridge '%s' started", name)

    async def stop_all(self) -> None:
        for name, bridge in self._bridges.items():
            try:
                await bridge.shutdown()
                logger.info("Bridge '%s' stopped", name)
            except Exception:
                logger.exception("Error stopping bridge '%s'", name)

    def _mapping_key(self, platform: str, chat_id: str) -> str:
        return f"{platform}:{chat_id}"

    def _binding(self, platform: str, chat_id: str) -> ChatBinding | None:
        return self._mappings.get(self._mapping_key(platform, chat_id))

    def _ensure_binding(self, platform: str, chat_id: str) -> ChatBinding:
        key = self._mapping_key(platform, chat_id)
        binding = self._mappings.get(key)
        if binding is None:
            binding = ChatBinding()
            self._mappings[key] = binding
        return binding

    def get_session_id(self, platform: str, chat_id: str) -> str | None:
        """The chat's current sticky session id (used by broadcast routing
        and tool-decision handling). None when unbound or no live thread."""
        binding = self._binding(platform, chat_id)
        return binding.session_id if binding else None

    def set_sticky_session(
        self, platform: str, chat_id: str, session_id: str | None
    ) -> None:
        self._ensure_binding(platform, chat_id).session_id = session_id

    def set_verbose(self, platform: str, chat_id: str, verbose: bool) -> None:
        self._ensure_binding(platform, chat_id).verbose = verbose

    def remove_mapping(self, platform: str, chat_id: str) -> None:
        self._mappings.pop(self._mapping_key(platform, chat_id), None)

    # --- Incoming message handling ---

    async def handle_incoming(
        self, platform: str, chat_id: str, text: str, bridge: Bridge
    ) -> None:
        """Process an incoming message from any platform."""
        text = text.strip()

        if text.startswith("/"):
            await self._handle_command(platform, chat_id, text, bridge)
            return

        session_id = self.get_session_id(platform, chat_id)
        session = self.session_mgr.get_session(session_id) if session_id else None
        if session is None:
            # No sticky thread (first contact, or it was garbage-collected) —
            # open a fresh one, owned by this chat, and make it sticky.
            session = await self.session_mgr.create_session(
                owner=self._mapping_key(platform, chat_id)
            )
            self.set_sticky_session(platform, chat_id, session.id)
            session_id = session.id

        try:
            await self.session_mgr.start_message(session_id, text)
        except ValueError as e:
            await bridge.send_text(chat_id, f"Error: {e}")

    # --- Tool approval ---

    async def handle_tool_decision(
        self,
        platform: str,
        chat_id: str,
        session_id: str,
        tool_use_id: str,
        approved: bool,
    ) -> bool:
        """Settle a pending tool-approval decision from a card tap.

        Returns False (and settles nothing) when: no ``ApprovalGateway`` is
        configured for this manager (approval mode off — the default, see
        ``docs/architecture.md`` "Remote tool approval"); the session is
        unknown; the session isn't owned by THIS (platform, chat_id) — the
        nonce alone already scopes a card to the chat it was sent to, but
        this is the same server-side ownership check ``switch_session``
        applies, not just card-plumbing; or the gateway reports nothing was
        pending (stale/duplicate tap, or its own timeout already denied it).
        """
        if self._approval_gateway is None:
            return False
        session = self.session_mgr.get_session(session_id)
        if session is None or session.owner != self._mapping_key(platform, chat_id):
            return False
        return self._approval_gateway.resolve(session_id, tool_use_id, approved)

    # --- Broadcast handler ---

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        """Handle broadcast events from SessionManager.

        Routes events to every chat whose sticky session is the event's
        session. A chat in quiet mode (the default) drops tool-activity and
        bookkeeping events (`QUIET_SUPPRESSED_EVENTS`); replies, errors and
        approval prompts always pass.
        """
        session_id = msg.get("session_id")
        if not session_id:
            return

        event_type = msg.get("type")
        for key, binding in self._mappings.items():
            if binding.session_id != session_id:
                continue
            if not binding.verbose and event_type in QUIET_SUPPRESSED_EVENTS:
                continue
            platform, chat_id = key.split(":", 1)
            bridge = self._bridges.get(platform)
            if bridge:
                try:
                    await bridge.handle_event(chat_id, msg)
                except Exception:
                    logger.exception("Broadcast to %s failed", key)

    def register_broadcast(self) -> None:
        self.session_mgr.on_broadcast("bridge_manager", self._on_broadcast)

    def unregister_broadcast(self) -> None:
        self.session_mgr.remove_broadcast("bridge_manager")

    # --- Session switching ---

    async def switch_session(
        self, platform: str, chat_id: str, session_id: str
    ) -> str:
        """Repoint the chat's sticky session to an existing session OWNED BY
        THIS CHAT. Returns a user-facing status line (shared by the
        `/switch` command and the `/sessions` inline-button callback).

        Ownership is enforced here, not just in what `/sessions` chooses to
        list: without it, any allowlisted chat could `/switch` straight to
        another chat's session id (guessed or leaked) and both chats would
        then receive that session's broadcasts — a cross-chat context leak.
        """
        session = self.session_mgr.get_session(session_id)
        if session is None:
            return f"Session '{session_id}' not found."
        # Exact match required — an unowned (owner=None) session is NOT an
        # implicit allow-all. Every production creation path (handle_incoming,
        # /new) always sets owner; a None owner only happens by calling
        # SessionManager.create_session() directly with no owner (e.g. a test
        # or a future non-bridge caller), and fail-closed means that session
        # stays switchable by nobody until something explicitly claims it.
        if session.owner != self._mapping_key(platform, chat_id):
            return f"Session '{session_id}' not found."  # don't confirm existence of another chat's session
        self.set_sticky_session(platform, chat_id, session.id)
        return f"Switched to session '{session.name}' ({session.id})."

    # --- Command handling ---

    async def _handle_command(
        self, platform: str, chat_id: str, text: str, bridge: Bridge
    ) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if command == "/new":
            session = await self.session_mgr.create_session(
                name=args or None, owner=self._mapping_key(platform, chat_id)
            )
            self.set_sticky_session(platform, chat_id, session.id)
            await bridge.send_text(
                chat_id,
                f"Created session '{session.name}' ({session.id}). "
                "You can start sending messages.",
            )

        elif command == "/sessions":
            # Scoped to sessions THIS chat created — listing every session
            # process-wide would leak other chats' session ids (and, via
            # /switch, their conversations) to anyone on the allowlist.
            own_key = self._mapping_key(platform, chat_id)
            sessions = sorted(
                (s for s in self.session_mgr.list_sessions() if s.owner == own_key),
                key=lambda s: s.created_at,
                reverse=True,
            )
            if not sessions:
                await bridge.send_text(
                    chat_id, "No sessions yet. Use /new to start one."
                )
                return
            current_sid = self.get_session_id(platform, chat_id)
            shown = sessions[: self.SESSION_LIST_LIMIT]
            items = [
                {
                    "id": s.id,
                    "name": s.name,
                    "status": s.status.value,
                    "current": s.id == current_sid,
                }
                for s in shown
            ]
            note = None
            if len(sessions) > self.SESSION_LIST_LIMIT:
                note = (
                    f"Showing the {self.SESSION_LIST_LIMIT} most recent of "
                    f"{len(sessions)}. Use /switch <id> for older ones."
                )
            await bridge.send_session_list(chat_id, items, note)

        elif command == "/switch":
            if not args:
                await bridge.send_text(chat_id, "Usage: /switch <session_id>")
                return
            msg = await self.switch_session(platform, chat_id, args.strip())
            await bridge.send_text(chat_id, msg)

        elif command in ("/quiet", "/verbose"):
            verbose = command == "/verbose"
            self.set_verbose(platform, chat_id, verbose)
            await bridge.send_text(
                chat_id,
                "Verbose mode on — I'll also show status and result lines."
                if verbose
                else "Quiet mode on — I'll send only my replies.",
            )

        elif command == "/current":
            session_id = self.get_session_id(platform, chat_id)
            if not session_id:
                await bridge.send_text(chat_id, "No session connected.")
                return
            session = self.session_mgr.get_session(session_id)
            if not session:
                await bridge.send_text(chat_id, "Session no longer exists.")
                return
            await bridge.send_text(
                chat_id,
                f"Current session: {session.name} ({session.id})\n"
                f"Status: {session.status.value}\n"
                f"Messages: {session.message_count}",
            )

        elif command == "/help":
            lines = [f"  {cmd} - {desc}" for cmd, desc in BRIDGE_COMMANDS.items()]
            await bridge.send_text(chat_id, "Commands:\n" + "\n".join(lines))

        else:
            await bridge.send_text(
                chat_id, f"Unknown command: {command}. Use /help."
            )
