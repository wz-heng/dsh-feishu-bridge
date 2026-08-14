/**
 * dsh-feishu-bridge — DSH plugin shell.
 *
 * A thin cordis plugin: it spawns and supervises the existing
 * `dsh_feishu_bridge` Python process (unchanged — see `src/`), confirms it
 * came up via its `/health` endpoint, and kills it cleanly on dispose. No
 * bridge logic lives here; every surface touch (manifest keys, cordis
 * lifecycle calls) is confined to this directory so a future cordis/dsh
 * version bump only ever means fixing this file.
 *
 * The cordis plugin shape — `export const name` / `export const inject`,
 * `apply(ctx, config)`, and `ctx.effect(setup)` where `setup` runs
 * immediately and its return value (if a function, sync or async) is the
 * cleanup awaited on scope dispose — is confirmed against
 * `deepseek-ai/deepseek-harness`'s own `packages/sandbox/sandbox-local`
 * provider, `pc439527/dsh-notify-bark`, the harness's
 * `docs/cordis-tutorial/02-lifecycle-and-effects.md`, and
 * `vendor/cordis/src/fiber.ts` (async disposers are awaited by
 * `fiber.dispose()`).
 * @module dsh-feishu-bridge
 */

import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readDotEnv } from './env.mjs'
import { spawnBridge, killGracefully } from './process-lifecycle.mjs'
import { waitForHealthy } from './health.mjs'

const REPO_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))

const DEFAULT_HOST = '0.0.0.0'
const DEFAULT_PORT = 8788
const DEFAULT_STARTUP_TIMEOUT_MS = 30_000

/** Stable cordis plugin name (matches the cordis.patch.yml insert id's `name`). */
export const name = 'dsh-feishu-bridge'

/** No hard service dependencies — the wrapper only needs Node's own child_process/fetch. */
export const inject = []

/**
 * Pick the Python interpreter: an explicit override, else the repo's own
 * `.venv` (created by the Quickstart's `python3.12 -m venv .venv`) if
 * present, else `python3` resolved from PATH. Never installs anything — see
 * "Install as a dsh plugin" in README.md for why v1 requires a pre-existing
 * environment.
 * @param {string | undefined} configured
 * @param {string} root
 * @returns {string}
 */
function resolvePythonBin(configured, root) {
  if (configured) return configured
  const venvPython = join(root, '.venv', 'bin', 'python')
  return existsSync(venvPython) ? venvPython : 'python3'
}

/**
 * Forward each complete line of a stdio chunk to a sink, dropping the
 * trailing empty split from a final newline.
 * @param {Buffer} chunk
 * @param {(line: string) => void} sink
 */
function forwardLines(chunk, sink) {
  for (const line of chunk.toString('utf8').split(/\r?\n/)) {
    if (line.length > 0) sink(line)
  }
}

/**
 * Plugin config. Every field is optional — a bare `dsh plugin add` works
 * against the repo's own `.env` / `.venv` with no row config at all.
 * @typedef {object} Config
 * @property {string} [host] - overrides `DSH_FEISHU_BRIDGE_HOST` in the child's env (see `src/dsh_feishu_bridge/config.py`), so the Python process actually binds where this wrapper health-checks. Unset means "whatever `.env`/the Python default resolves to".
 * @property {number} [port] - overrides `DSH_FEISHU_BRIDGE_PORT` likewise.
 * @property {string} [pythonBin] - interpreter override; see {@link resolvePythonBin}.
 * @property {number} [startupTimeoutMs]
 * @property {Record<string, string>} [env] - extra env merged in last, after `.env` and the host/port overrides above — the final override layer.
 * @property {string} [repoRoot] - override the resolved repo root (tests).
 * @property {{ spawnImpl?: Function, fetchImpl?: Function, readFile?: Function }} [internals] - test seams, mirroring the `internals` hook used by @deepseek-ai/dsh-sandbox-local.
 */

/**
 * Plugin entry.
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {Config} [config]
 */
export function apply(ctx, config = {}) {
  const root = config.repoRoot ?? REPO_ROOT
  const pythonBin = resolvePythonBin(config.pythonBin, root)
  const startupTimeoutMs = config.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS
  const internals = config.internals ?? {}
  const readFile = internals.readFile ?? readFileSyncUtf8

  ctx.effect(() => {
    let disposing = false

    const dotenv = readDotEnv(join(root, '.env'), readFile)
    // Explicit host/port config must reach the Python process itself (not
    // just this wrapper's health-check URL) or the two drift: the plugin
    // would probe a port the child was never told to bind. Layered after
    // `.env` (an explicit row config beats the repo's background default)
    // but before `config.env` (the row's final, unconditional escape hatch).
    const hostPortOverrides = {}
    if (config.host !== undefined) hostPortOverrides.DSH_FEISHU_BRIDGE_HOST = config.host
    if (config.port !== undefined) hostPortOverrides.DSH_FEISHU_BRIDGE_PORT = String(config.port)
    const env = { ...process.env, ...dotenv, ...hostPortOverrides, ...(config.env ?? {}) }

    // The health-check target is derived from this same merged env — the
    // single source of truth the child actually receives — rather than
    // from `config.host`/`config.port` directly, so a port set only via
    // `.env` (not the patch row) still resolves correctly too.
    const resolvedHost = env.DSH_FEISHU_BRIDGE_HOST || DEFAULT_HOST
    const resolvedPort = env.DSH_FEISHU_BRIDGE_PORT || String(DEFAULT_PORT)
    const healthHost = resolvedHost === '0.0.0.0' ? '127.0.0.1' : resolvedHost
    const healthUrl = `http://${healthHost}:${resolvedPort}/health`

    const child = spawnBridge({ pythonBin, cwd: root, env, spawnImpl: internals.spawnImpl })

    child.once('exit', (code, signal) => {
      if (!disposing) {
        ctx.logger.warn(`dsh-feishu-bridge: process exited unexpectedly (code=${code ?? 'null'} signal=${signal ?? 'null'})`)
      }
    })
    child.once('error', (err) => {
      ctx.logger.warn(`dsh-feishu-bridge: failed to spawn '${pythonBin}': ${err.message}`)
    })
    child.stdout?.on('data', (chunk) => forwardLines(chunk, (line) => ctx.logger.info(`[dsh-feishu-bridge] ${line}`)))
    child.stderr?.on('data', (chunk) => forwardLines(chunk, (line) => ctx.logger.warn(`[dsh-feishu-bridge] ${line}`)))

    // Aborted on dispose so a plugin unload/reload never leaves the health
    // poll's timer or in-flight fetch running past the scope that owns it
    // (up to `startupTimeoutMs`, 30s by default) — see the "should" finding
    // this fixed: the loop used to run to completion regardless of dispose.
    const healthAbort = new AbortController()
    void waitForHealthy({ url: healthUrl, timeoutMs: startupTimeoutMs, fetchImpl: internals.fetchImpl, signal: healthAbort.signal })
      .then(() => {
        if (!disposing) ctx.logger.info(`dsh-feishu-bridge: healthy at ${healthUrl}`)
      })
      .catch((err) => {
        if (!disposing) ctx.logger.warn(`dsh-feishu-bridge: did not become healthy within ${startupTimeoutMs}ms: ${err.message}`)
      })

    return async () => {
      disposing = true
      healthAbort.abort()
      await killGracefully(child)
    }
  })
}

/** Default `readFile` for {@link readDotEnv} — a named function so it shows up in stack traces. */
function readFileSyncUtf8(path) {
  return readFileSync(path, 'utf8')
}

export default { name, inject, apply }
