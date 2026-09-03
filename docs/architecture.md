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

## Remote tool approval

Opt-in (`DSH_APPROVAL_MODE=1`; off by default — existing deployments are
unaffected). When on, every `bash` tool call blocks until a human taps
Allow/Deny on a Feishu card in the session's owning chat, defaulting to deny
on timeout.

The bundled default composition (`deepseek-harness-runtime-bin`'s
`danger-full-access` bash — no other tool is even model-facing today; the
mounted spine, `@deepseek-ai/dsh-agent-spine-demo`, wires up `bash` plus
skill/job/goal tools and nothing file-write-shaped, so an earlier draft of
this doc's claim of a bundled `str_replace_editor` tool was simply wrong)
never raises an approval request. Reaching one needs a different
composition, and reverse-engineering the compiled runtime binary
(`deepseek-harness-runtime-bin`'s `dsh-jsonrpc-agent-pkg-*`, which embeds its
own unminified source — extracted with `strings -a` and cross-referenced by
package name) turned up two paths, not the one originally assumed:

- **Dead end**: `HarnessClient.next_request()`/`respond()` exist on the
  Python SDK side and the wire protocol can carry a server-initiated
  request in principle. But `@deepseek-ai/dsh-sdk-jsonrpc-server` — the
  specific server plugin this SDK talks to — never sends one:
  `HarnessSdkJsonRpcServer.handleRequest()` is a closed switch over exactly
  `initialize`/`session/prompt`/`shutdown`, and the `JsonRpcLineTransport`
  instance is a local variable inside that package's `apply()`, never
  registered as an injectable cordis service and never wired to the
  approval seam. `@deepseek-ai/dsh-acp` DOES answer approval requests over
  a real request (`session/request_permission`, the standard Agent Client
  Protocol method — it embeds `@agentclientprotocol/sdk`), but it binds its
  own `AgentSideConnection` directly to stdio with a wire format
  incompatible with this SDK (`session/new`/`session/prompt`/
  `session/update` vs. this SDK's `initialize`/`session/prompt`/
  `session.event`), and composing both over the same stdio would corrupt
  both protocols. Adopting it would mean writing a second Python ACP client
  from scratch and discarding `deepseek_harness_sdk` entirely — out of
  scope.
- **What's actually used**: approval gating is a *generic, tool-agnostic*
  mechanism, independent of both of the above. Any composed plugin can
  return `{kind: 'ask', reason}` from the `tools/pre-execute` waterfall
  (`@deepseek-ai/dsh-tool-bash`'s own source even carries a
  `TODO(permissions)` comment naming this exact extension point); that
  routes through the tool registry's `serviceAsk()` into
  `@deepseek-ai/dsh-user-approval`'s `ApprovalService.request()`, which
  raises an `approval/request` waterfall any composed answerer can settle.
  Critically, none of this needs a sandboxing bash executor —
  `@deepseek-ai/dsh-permission-presets` (the preset/sandbox-mode package)
  requires one, but the raw `ask`/`ApprovalService` seam does not.

So `approval_runtime/cordis.yml` composes the bundled default (unconfined
`@deepseek-ai/dsh-bash-local`, unchanged) plus `@deepseek-ai/dsh-user-approval`
and two small first-party cordis plugins:
`approval_runtime/pre-execute-gate.mjs` marks `bash` calls `{kind:'ask'}`,
and `approval_runtime/approval-relay.mjs` answers the resulting
`approval/request` with a plain HTTP POST — a side channel of this bridge's
own, not the SDK's stdio protocol — to `ApprovalGateway`
(`approval_gateway.py`), a loopback-only server (`127.0.0.1`, never the
public app) started alongside the FastAPI app. `ApprovalGateway.callback_url`
is handed to the harness subprocess via `DSH_APPROVAL_CALLBACK_URL` when
`DshAdapterConfig.env` is built (see `app.py`) — deliberately distinct from
the bare `.url` origin, since a caller building the callback path by hand
from `.url` has nothing tying it to the route `start()` actually registers
(exactly how an earlier version of this wiring silently 404'd every
approval request). The gateway publishes a
`tool_approval_request` broadcast through the *existing*
`SessionManager`/`Bridge.handle_event` fan-out (`FeishuBridge.
send_tool_approval_request` and the "approval" nonce-kind branch in
`_on_card_action` were ported from the source bridge and unit-tested from
day one — this is the first thing that actually drives them), and
`BridgeManager.handle_tool_decision` resolves the gateway's pending future
once a card is tapped, after re-checking session ownership server-side (the
same check `switch_session` applies — nonce scoping alone already limits a
card to the chat it was sent to, but this is defense in depth, not the only
gate). Every failure path — timeout, malformed callback body, an unknown
session, the harness subprocess never calling back at all — resolves to
deny; nothing in this design can fail open.

`run_turn`'s blocking `harness.run()` call needed no changes: the whole
approval round-trip happens on the loopback side channel, fully orthogonal
to the stdio JSON-RPC channel `run_turn` is already blocked on.

**Loopback callback is proxy-immune, on both ends.** The harness subprocess
inherits its parent's FULL environment (`deepseek_harness.client.
HarnessClient.start()` merges `DshAdapterConfig.env` on top of
`os.environ.copy()`, never replacing it) — so an ambient `HTTP_PROXY`/
`http_proxy` from a system-wide proxy (Clash and friends) is present unless
exempted. A real-machine acceptance run hit exactly this: the proxy declined
to forward the loopback callback address (502), and the pending approval
never got far enough to notify anyone — no card, no deny, an unbounded
hang, because the gateway's own deny-timeout only starts counting once a
request has actually arrived and been parsed; it can't time out a request
that never gets there. Two independent defenses close this, deliberately
redundant: `app.py`'s lifespan injects `no_proxy=127.0.0.1,localhost`
(merged with any caller-supplied value, never overwriting it) into the
subprocess env; and `approval-relay.mjs`'s callback POST goes over
`node:http`/`node:https` directly rather than the global `fetch` — plain
`http.request` has no proxy awareness at all, so the callback cannot be
proxied regardless of what the process environment says or what a future
Node default (`--use-env-proxy`/`NODE_USE_ENV_PROXY`) does. On top of that,
the relay's own fallback timeout (`DSH_APPROVAL_TIMEOUT_MS`) is set a few
seconds LARGER than the gateway's `timeout_seconds`, not equal to it — so
the gateway's own (more informative) deny response always has time to
travel back over loopback before the relay gives up on its own; the relay's
timeout is purely a backstop for "the callback never reaches the gateway at
all," which the gateway-side timeout structurally cannot cover.

## SDK compat: `cordis`/`session_root` vs. `profile`/`patches`/`dsh_home`

`deepseek-harness-sdk` 0.1.2a3 (an alpha pre-release; the SDK canary
installs it unpinned — see README) dropped `DeepSeekHarnessConfig.cordis`
and `.session_root` for `.profile`/`.patches`/`.dsh_home`. The pinned range
this repo ships (0.1.0rc6–0.1.1rc1) still has the old shape, so
`dsh_adapter._build_harness_config` probes the INSTALLED SDK's actual
`DeepSeekHarnessConfig` fields via `inspect.signature` (cached — see
`_harness_config_field_names`) and builds whichever shape it supports,
rather than branching on a parsed version number — a future pre-release
could rename fields again, and this keeps working without another code
change.

Two things don't translate 1:1, verified by actually installing 0.1.2a3 +
the matching `deepseek-harness-runtime-bin` into an isolated venv and
booting the real runtime (not just reading source):

- **`session_root` → `dsh_home` is role-equivalent, not byte-identical.**
  The old `session_root` pointed directly at the JSONL storage directory.
  The new `dsh_home` is a broader "harness home" (session JSONLs land one
  level down, at `$dsh_home/sessions`) and — unlike the old implicit
  `./.sessions` default — is now MANDATORY; the SDK never falls back to
  `~/.dsh`. `_build_harness_config` passes `session_root`'s value straight
  through as `dsh_home` (same directory now anchors more state, not just
  sessions) and falls back to `<cwd>/.dsh-home` when unset, so `dsh_home`
  is never omitted.
- **`cordis` (full composition) → `profile` + `patches` (named profile +
  overlay).** The old `cordis` kwarg pointed at a file that fully replaced
  the plugin composition (`DSH_CORDIS_CONFIG`). The new model boots a
  named, pre-materialized `profile` (default `"sdk"`) and applies
  `patches` — a tuple of overlay YAML files using the SAME id-targeted
  insert/config-override/disable format this repo's own top-level
  `cordis.patch.yml` already uses — on top of it. `"sdk"`'s new default is
  a much richer agent than the old bundled default (subagents, web tools,
  skills, and a SANDBOXED bash executor, `@deepseek-ai/dsh-bash-sandbox`,
  in place of the old unconfined `@deepseek-ai/dsh-bash-local`). Rather
  than fight that — an attempt to reassemble the old minimal
  `@deepseek-ai/dsh-agent-spine-demo` composition standalone under the new
  model surfaced an undocumented `loader` service-dependency risk (it's
  normally layered under `@deepseek-ai/dsh-base`, which supplies that
  service) — approval mode's new-SDK path (`approval_runtime/
  approval.patch.yml`, distinct from the old full-composition
  `cordis.yml`) leaves the runtime's own `"sdk"` profile untouched and
  patches in only the two first-party approval plugins, plus an explicit
  `{id: approval, config: {policy: ask}}` override (defense in depth —
  don't rely on the new profile's own default policy never drifting).
  `approval-relay.mjs` and `pre-execute-gate.mjs` needed NO code changes:
  a live `harness.start()` against the patched `"sdk"` profile booted
  cleanly, and both plugins already key off stable, version-independent
  extension points (`tools/pre-execute`, `approval/request`) and the tool
  NAME `'bash'` rather than a specific executor plugin — their own module
  docstrings already anticipated `@deepseek-ai/dsh-tool-bash` alongside the
  old `dsh-bash-local`.

`pyproject.toml`'s pin stays at `0.1.0rc6` — this task's research covered
the 0.1.2a3 signature break specifically, not a behavior audit of
0.1.0rc7/0.1.1rc1 (both otherwise still old-shaped), so bumping the pin is
left as its own, separately-tested decision rather than a side effect of
this compat shim.

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
