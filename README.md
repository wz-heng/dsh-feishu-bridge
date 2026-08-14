# dsh-feishu-bridge

English | [中文](README.zh.md)

A Feishu (Lark) channel bridge for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`): message a Feishu bot, it runs a `dsh` agent turn, the reply comes back to the chat.

**This is an independent community project. It is not built, maintained, or endorsed by DeepSeek.** It drives `dsh` entirely through its public Python SDK (`deepseek-harness-sdk`) — a subprocess boundary, no forked/patched harness code.

## What this is

- A production-grade Feishu bot bridge (fail-closed allowlist, one-time card nonces, per-chat verbosity, sticky sessions, both `ws` and `webhook` transports) ported from a mature bridge in another agent-harness project and adapted to drive `dsh` instead.
- The thin adapter that talks to `deepseek-harness-sdk` lives in one file, `src/dsh_feishu_bridge/dsh_adapter.py`, and the SDK version is pinned exactly — the harness is a v0.1 developer preview that documents breaking changes between releases.

## Quickstart (5 minutes)

```sh
git clone <this-repo-url> dsh-feishu-bridge
cd dsh-feishu-bridge
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Set your credentials as environment variables — never in a committed file:

```sh
export DEEPSEEK_API_KEY=sk-your-key-here
# export DEEPSEEK_BASE_URL=http://127.0.0.1:8000/v1   # only if using a proxy

export FEISHU_APP_ID=cli_xxxxxxxx
export FEISHU_APP_SECRET=xxxxxxxx
export FEISHU_TRANSPORT=ws          # or "webhook" if you have a public URL
# export FEISHU_VERIFICATION_TOKEN=xxxx   # required when FEISHU_TRANSPORT=webhook
# export FEISHU_ENCRYPT_KEY=xxxx          # optional, if you enabled encryption

# Fail-closed allowlist — REQUIRED. With no ids configured the bot answers
# no one; every message is rejected by design (see "Security posture" below).
export FEISHU_ALLOWED_OPEN_IDS=ou_xxxxxxxxxxxxxxxx
# export FEISHU_ALLOWED_CHAT_IDS=oc_xxxxxxxxxxxxxxxx   # optional group allowlist
```

Run it:

```sh
python -m dsh_feishu_bridge
# or: dsh-feishu-bridge
```

Message the bot in Feishu. First message from an unlisted `open_id` is silently rejected and logged — that log line is how you discover your own `open_id` to put in the allowlist (see "Getting your open_id" below).

### Getting your open_id

Send the bot a message once (it will not reply — this is expected, fail-closed). Check the server log for a line like:

```
Feishu: rejecting message from unauthorized open_id=ou_xxxxxxxxxxxxxxxx (chat=oc_xxxx)
```

Copy that `open_id` into `FEISHU_ALLOWED_OPEN_IDS` and restart.

## Commands

| Command | What it does |
|---|---|
| `/new [name]` | Start a fresh session |
| `/sessions` | List sessions (tap one to switch) |
| `/switch <id>` | Point at an existing session |
| `/current` | Show current session info |
| `/quiet` | Only show replies (default) |
| `/verbose` | Also show status/result lines |
| `/help` | List commands |

## Configuration reference

Everything is an environment variable. An optional YAML file (path via `DSH_FEISHU_BRIDGE_CONFIG`, or `--config`) can supply the non-secret knobs (allowlists, model, provider) — see `examples/config.example.yaml`. Env vars always win when both are set, and credentials are never read from the YAML file on purpose.

| Env var | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | Required. Same var the SDK itself reads. |
| `DEEPSEEK_BASE_URL` | — | Optional, for an OpenAI-compatible proxy. |
| `DSH_PROVIDER` | `deepseek-official` | Provider route (see SDK docs). |
| `DSH_MODEL` | `deepseek-v4-flash` | Model id. |
| `DSH_MAX_TOKENS` | unset | Optional per-request output cap. |
| `DSH_CORDIS` | unset | Path to a custom Cordis composition; omit to use the bundled default. |
| `DSH_SESSION_ROOT` | unset | Where the runtime writes its JSONL session logs. |
| `DSH_WORKSPACE` | current dir | The workspace the agent's tools operate in. |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | — | Both required together, or leave both unset. |
| `FEISHU_TRANSPORT` | `ws` | `ws` (no public URL needed) or `webhook`. |
| `FEISHU_VERIFICATION_TOKEN` | — | Required when `FEISHU_TRANSPORT=webhook`. |
| `FEISHU_ENCRYPT_KEY` | unset | Optional, if you enabled event encryption. |
| `FEISHU_DOMAIN` | `https://open.feishu.cn` | Change for Lark international / a proxy. |
| `FEISHU_ALLOWED_OPEN_IDS` | *(empty)* | Comma-separated. **Required** — empty means nobody is authorized. |
| `FEISHU_ALLOWED_CHAT_IDS` | *(empty = no restriction)* | Comma-separated group allowlist. |
| `DSH_FEISHU_BRIDGE_HOST` | `0.0.0.0` | HTTP server bind address (health check + webhook route). |
| `DSH_FEISHU_BRIDGE_PORT` | `8788` | HTTP server port. |

## Security posture

- **Fail-closed by default.** No configured `FEISHU_ALLOWED_OPEN_IDS` means every sender is rejected — there is no implicit allow-all. This is deliberate: an agent bridge with a blank allowlist would otherwise let anyone in your tenant run arbitrary agent turns.
- **Webhook mode requires a verification token.** Without one, the webhook route is never registered — the process refuses to boot half-configured rather than silently accepting unverified events.
- **Card buttons (session-switch) use one-time, identity-bound nonces.** A nonce is minted for one exact action + session; a second click, a replayed nonce, or a tampered card value is rejected without being honored.
- **Sessions are owned by the chat that created them.** `/sessions` only lists (and `/switch` only accepts) sessions owned by the requesting chat — even between two allowlisted chats, one can't list or hijack another's session id and start receiving its replies.
- Run this bridge's process with the least privilege the composition needs. The bundled default `dsh` composition (`examples/jsonrpc-agent` upstream) uses `danger-full-access` bash + file editing — run it in a disposable workspace/container, not against a machine you care about.

## Limitations (v1, by design)

These are deliberate scope decisions driven by what `deepseek-harness-sdk` v0.1 actually exposes today — documented here rather than silently missing:

- **No incremental streaming.** `DeepSeekHarness.run()` is a synchronous call that blocks until the turn is idle; the SDK's `on_notification` hook receives raw protocol notifications mid-call, but their event schema isn't part of the documented v0.1 contract. So the bridge posts one status line at turn start and the full reply once the turn completes — not a token-by-token stream like some other bridges.
- **No tool-approval flow.** The bundled example `dsh` composition runs Bash/editor tools without an interactive approval prompt, and the SDK's high-level `Session` API has no hook to surface a server-initiated approval request even if a future composition added one (only the low-level `HarnessClient.next_request()/respond()` does). The approve/deny card machinery is ported and unit-tested for interface parity, but no session backend triggers it today.
- **Sessions are sticky only within one bridge process.** A restart starts a fresh `DeepSeekHarness` subprocess, and cross-restart resume via a shared `session_root` isn't a behavior the SDK's v0.1 docs commit to — so this bridge doesn't build undocumented persistence on top of it. A chat's sticky session pointer and its `/quiet`/`/verbose` preference both reset on restart.
- **Text messages only** — no voice, image, or file attachments, and no topic/thread replies (one sticky session per chat would silently cross wires across threads).
- **One model configuration per bridge process** — provider/model/cordis composition are subprocess-wide, not per-chat. There's no `/agent`-style rebind command; run a second bridge process (different port, different Feishu app or allowlist) if you need a second configuration.

## Development

```sh
pip install -e ".[dev]"
pytest                       # fast — no network, no subprocess, no API quota
pytest -m real_sdk           # real smoke test: needs DEEPSEEK_API_KEY + the runtime; auto-skips otherwise
```

The test suite fakes both edges: a scripted `DshBackend` stands in for the real SDK (no subprocess spawned, no quota spent), and a local `FakeFeishuServer` stands in for `open.feishu.cn` to assert what the bridge actually sends outbound. See `tests/`.

If your network runs through a proxy (e.g. Clash) without a `127.0.0.1`/`localhost` exemption, export `no_proxy=127.0.0.1,localhost` before running the loopback-server tests — otherwise the proxy can swallow the bridge's own outbound calls to the fake server. The bridge itself already forces `trust_env=False` for loopback domains at runtime, so this only matters for the test process.

## License

MIT — see [LICENSE](LICENSE).
