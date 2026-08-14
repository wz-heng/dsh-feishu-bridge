# Architecture notes

Design record for maintainers — not user-facing (see README for that).

## Why one `DeepSeekHarness` per process, not one per chat

`deepseek-harness-sdk`'s `DeepSeekHarness` is documented as reusable across
calls and owns a single lazily-started runtime subprocess. Because the wire
protocol addresses notifications by session id, one subprocess can service
many concurrent sessions — so `dsh_adapter.DshAdapter` starts exactly one
`DeepSeekHarness` for the bridge process's lifetime, and every Feishu chat
gets its own dsh session id against that shared subprocess
(`session_manager.SessionManager` mints one per `BridgeSession`). This avoids
paying a subprocess-start cost per chat and matches how the SDK's own
tutorial uses it (a context-managed instance, `run()` called repeatedly).

## Why turns are not streamed incrementally

`DeepSeekHarness.run()` / `Session.run()` are synchronous and block until the
turn's session goes idle. The `on_notification` hook receives raw
protocol-level notifications while blocked, but v0.1's documented contract
doesn't specify their event `type` values beyond what `RunResult`'s own
helpers rely on (`assistant/message`, `turn/end`, `agent/inbox/spliced`) —
not enough to safely reconstruct a token-level or even message-level stream
without guessing at undocumented shapes. `DshAdapter.run_turn` therefore
treats the SDK call as atomic: `SessionManager._run_turn` broadcasts a
`status: running` marker, awaits the whole turn on a worker thread
(`asyncio.to_thread`, since the SDK call is blocking Python), then broadcasts
the final text as one `assistant_text` event. Revisit this once the SDK
documents its notification event schema for v1.

## Why there's no tool-approval flow

Same root cause: the bundled example `dsh` composition
(`deepseek-harness-runtime-bin`'s default, `danger-full-access` bash +
`str_replace_editor`) never raises an interactive approval request, and the
high-level `Session` API used here has no callback for a server-initiated
`request` even if a future composition added one — only the low-level
`HarnessClient.next_request()`/`respond()` does, and adopting that would mean
dropping to the low-level client entirely (no more `Session.run()`
convenience). `FeishuBridge.send_tool_approval_request` and the "approval"
nonce-kind branch in `_on_card_action` are kept (ported from the source
bridge, unit-tested directly) so the interface and card-security machinery
are ready if a future dsh composition adds approval-gated tools and this
adapter grows a hook for it — but no code path exercises them today.
`BridgeManager.handle_tool_decision` always returns `False` for the same
reason: there is never a pending approval to settle.

## Why sticky sessions don't survive a restart

`docs/user/guide/python-sdk.md` confirms session continuity *within* one
`DeepSeekHarness` instance (same subprocess, same session id → same
conversation, same owned Bash process) and mentions JSONL persistence under
`session_root`, but does not document cross-process resume as a supported
behavior for v0.1. Building persistence on top of an unconfirmed contract
would be exactly the kind of speculative work this project's ground rules
rule out. So a bridge restart deliberately starts fresh: new subprocess, new
sessions, and `BridgeManager`'s chat→session/verbosity mappings reset too
(they were never persisted — there's no durable state whose loss would
matter once the underlying dsh sessions themselves don't survive either).
Revisit if/when the SDK documents a resume path.

## Why there's no multi-agent / `/agent` command

The source bridge this was ported from binds each chat to one of several
registered agents (different model configs, different working directories).
This bridge has exactly one dsh backend per process — `provider`/`model`/
`cordis` are process-wide settings (`DshAdapterConfig`), not chosen per chat.
Running two configurations means running two bridge processes (different
port, Feishu app, or allowlist) rather than one process routing between them.
This is a deliberate simplification, not a missing feature: the source
bridge's multi-agent registry existed to support its host application's
unrelated multi-agent product surface, which has no analogue here.
