# dsh-feishu-bridge

English | [中文](README.zh.md)

A Feishu (Lark) channel bridge for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`): message a Feishu bot, it runs a `dsh` agent turn, the reply comes back to the chat.

**This is an independent community project. It is not built, maintained, or endorsed by DeepSeek.** It drives `dsh` entirely through its public Python SDK (`deepseek-harness-sdk`) — a subprocess boundary, no forked/patched harness code.

## What this is

- A production-grade Feishu bot bridge (fail-closed allowlist, one-time card nonces, per-chat verbosity, sticky sessions, both `ws` and `webhook` transports) ported from a mature bridge in another agent-harness project and adapted to drive `dsh` instead.
- The thin adapter that talks to `deepseek-harness-sdk` lives in one file, `src/dsh_feishu_bridge/dsh_adapter.py`, and the SDK version is pinned exactly — the harness is a v0.1 developer preview that documents breaking changes between releases.

## Screenshots

![A real turn in Feishu: the agent reads the workspace and summarizes a file](docs/screenshots/chat-agent-turn.png)

![Fail-closed by default: boot, reject, allowlist, reply](docs/screenshots/fail-closed-boot.png)

![Install as a dsh plugin](docs/screenshots/dsh-plugin-add.png)

![Architecture: Feishu → fail-closed boundary → DeepSeek Harness](docs/screenshots/architecture.png)

## Quickstart (5 minutes)

```sh
git clone https://github.com/wz-heng/dsh-feishu-bridge.git
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
# export FEISHU_ENCRYPT_KEY=xxxx          # required when FEISHU_TRANSPORT=webhook

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

## Install as a dsh plugin

Instead of running the standalone process above, `dsh plugin add` can install this repo into a `dsh` profile: the plugin is a thin Node/cordis shell (`package.json`, `cordis.patch.yml`, `lib/`) that spawns and supervises the same unmodified Python process — it does not reimplement or patch any bridge logic.

**Two steps, in order — the plugin never installs Python dependencies for you:**

1. **Install the Python side yourself first**, exactly as in the Quickstart above:

   ```sh
   git clone https://github.com/wz-heng/dsh-feishu-bridge.git
   cd dsh-feishu-bridge
   python3.12 -m venv .venv
   . .venv/bin/activate
   pip install -e .
   ```

   Set `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_ALLOWED_OPEN_IDS` / etc. — either exported in the shell that starts `dsh`, or in a `.env` file at this repo's root (`KEY=value` per line; the plugin reads it directly and merges it into the spawned process's inherited environment, since the Python side itself only reads `os.environ`).

2. **Then add the plugin to your profile:**

   ```sh
   dsh plugin --profile <name> add /path/to/dsh-feishu-bridge
   ```

   `dsh` starts the bridge as a managed child the next time that profile boots: it spawns `<repo>/.venv/bin/python -m dsh_feishu_bridge` (falling back to `python3` on `PATH` if no `.venv` exists at the repo root), waits for `GET /health` to report `{"status": "ok"}`, and on profile/plugin dispose sends `SIGTERM`, escalating to `SIGKILL` if the process hasn't exited within 5 seconds — the same clean-shutdown behavior as `Ctrl-C`-ing the standalone process, just automatic.

   Every row config field is optional (`host`, `port`, `pythonBin`, `startupTimeoutMs`, `env`) — a bare `add` with no row edits works as long as step 1 is done and the defaults (`0.0.0.0:8788`, repo-root `.venv`) match your setup. `host`/`port` set `DSH_FEISHU_BRIDGE_HOST`/`DSH_FEISHU_BRIDGE_PORT` in the spawned process's env (see "Configuration reference" below) — they change where the Python side actually binds, and the plugin's own health check follows the same value, so the two never drift apart. Override in your profile's own `cordis.patch.yml`, e.g. to point at a different interpreter and port:

   ```yaml
   - insert:
       - id: feishu-bridge
         name: dsh-feishu-bridge
         config:
           pythonBin: /usr/local/bin/python3.12
           port: 8799
   ```

This wrapper is v1: no build step (plain ESM under `lib/`), zero npm dependencies, and it never bootstraps a Python environment — there's no established convention for that among installable dsh plugins wrapping an external process today, so this repo doesn't invent one. Its own tests live under `tests-node/` (`node --test tests-node/**/*.test.mjs`), separate from the Python suite in `tests/`.

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
| `FEISHU_ENCRYPT_KEY` | unset | Required when `FEISHU_TRANSPORT=webhook` — enable "Encrypt Key" for this event subscription in the Feishu console and paste the same value here. Used to verify each request's `X-Lark-Signature` (see "Security posture"). |
| `FEISHU_DOMAIN` | `https://open.feishu.cn` | Change for Lark international / a proxy. |
| `FEISHU_ALLOWED_OPEN_IDS` | *(empty)* | Comma-separated. **Required** — empty means nobody is authorized. |
| `FEISHU_ALLOWED_CHAT_IDS` | *(empty = no restriction)* | Comma-separated group allowlist. |
| `DSH_FEISHU_BRIDGE_HOST` | `0.0.0.0` | HTTP server bind address (health check + webhook route). |
| `DSH_FEISHU_BRIDGE_PORT` | `8788` | HTTP server port. |

## Security posture

- **Fail-closed by default.** No configured `FEISHU_ALLOWED_OPEN_IDS` means every sender is rejected — there is no implicit allow-all. This is deliberate: an agent bridge with a blank allowlist would otherwise let anyone in your tenant run arbitrary agent turns.
- **Webhook mode requires both a verification token and an encrypt key.** Without either, the webhook route is never registered — the process refuses to boot half-configured rather than silently accepting unverified events. The encrypt key is not optional: a verification token alone is a static value carried in the request body, not a per-request signature, so it cannot authenticate where a request actually came from.
- **Every webhook request is signature-, timestamp-, and replay-verified at this bridge's own boundary** — before it is ever handed to the underlying SDK. `X-Lark-Signature` is checked against `sha256(timestamp + nonce + encrypt_key + body)`; the timestamp must fall within a 5-minute window of "now"; and a `(timestamp, nonce)` pair already seen is rejected as a replay. A request that fails any of these checks gets a `401` and never reaches message handling. The one deliberate exception is Feishu's own "save request URL" console step: that handshake is never signed (no subscription is confirmed yet to sign against), so this bridge checks only `FEISHU_VERIFICATION_TOKEN` for it and echoes the challenge back directly — the same, already-mandatory check the underlying SDK would otherwise perform.
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

The dsh plugin shell (`lib/`, see "Install as a dsh plugin" above) has its own, separate JS test suite — no Python involved:

```sh
node --test tests-node/**/*.test.mjs
```

## License

MIT — see [LICENSE](LICENSE).
