/**
 * Cordis plugin: answers the `approval/request` waterfall (raised by
 * `@deepseek-ai/dsh-user-approval`'s `ApprovalService` for any tool call
 * `pre-execute-gate.mjs` marked `{kind:'ask'}`) by relaying the decision to
 * a Python-owned loopback HTTP callback — NOT the runtime's stdio JSON-RPC
 * SDK channel.
 *
 * Why not the stdio channel: `deepseek_harness.HarnessClient.next_request()`
 * / `.respond()` exist on the Python side, and the wire protocol can in
 * principle carry a server-initiated request. But the SPECIFIC server plugin
 * our Python SDK talks to, `@deepseek-ai/dsh-sdk-jsonrpc-server`, never
 * sends one: its `handleRequest()` is a closed switch over exactly
 * `initialize` / `session/prompt` / `shutdown`, and its `JsonRpcLineTransport`
 * instance is a local variable in that package's `apply()`, never registered
 * as an injectable cordis service and never wired to `approval/request` —
 * confirmed by reading the compiled runtime's own bundled source (see the
 * task's Phase 1 recon comment). `@deepseek-ai/dsh-acp` DOES answer this
 * waterfall over a real request (`session/request_permission`), but it binds
 * its own `AgentSideConnection` directly to stdio with an incompatible wire
 * format, so composing it alongside `dsh-sdk-jsonrpc-server` would fight over
 * the same stdin/stdout. Hence a fully independent side channel here.
 *
 * Fail-closed everywhere: a missing config env var refuses to load rather
 * than composing in a silently-inert gate (see the check below), and every
 * failure path of the relay call itself (non-2xx, malformed body, network
 * error, timeout) denies rather than allows.
 * @module dsh-feishu-bridge/approval-runtime/approval-relay
 */

const DEFAULT_TIMEOUT_MS = 60_000

const ALLOWED_OUTCOMES = new Set(['allowed-once', 'rejected', 'cancelled'])

/** Stable cordis plugin name. */
export const name = 'approval-relay'

/** No hard service dependencies — this only needs the `approval/request` event. */
export const inject = []

/**
 * @typedef {object} Config
 * @property {string} [callbackUrl] - overrides `$DSH_APPROVAL_CALLBACK_URL`.
 * @property {number} [timeoutMs] - overrides `$DSH_APPROVAL_TIMEOUT_MS` (default 60000).
 * @property {{ fetchImpl?: Function }} [internals] - test seam, mirroring the `internals` hook used elsewhere in this repo (see `lib/index.mjs`).
 */

/**
 * Plugin entry.
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {Config} [config]
 */
export function apply(ctx, config = {}) {
  const callbackUrl = config.callbackUrl ?? process.env.DSH_APPROVAL_CALLBACK_URL
  if (!callbackUrl) {
    // A composed-but-unreachable relay would mean every gated tool call
    // hangs until ApprovalService's own no-answerer default ('unavailable')
    // — but ONLY once a call actually needs approval, i.e. silent failure
    // discovered live in production. Refuse to load instead.
    throw new Error(
      'approval-relay: DSH_APPROVAL_CALLBACK_URL is required when this plugin is composed '
      + '(approval mode with no callback target is a misconfiguration, not a valid deployment)'
    )
  }
  const timeoutMs = config.timeoutMs
    ?? (process.env.DSH_APPROVAL_TIMEOUT_MS ? Number(process.env.DSH_APPROVAL_TIMEOUT_MS) : DEFAULT_TIMEOUT_MS)
  const fetchImpl = config.internals?.fetchImpl ?? fetch

  ctx.on('approval/request', async (req, next) => {
    // No callId means nothing routes a decision back to a specific tool
    // call on the Python side (mirrors dsh-acp's own guard for the same
    // field) — fall through to the next answerer rather than hang.
    if (req.callId === undefined) return next()
    const sessionId = req.agent?.session?.id
    if (sessionId === undefined) return next()

    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    const onSignalAbort = () => controller.abort()
    req.signal?.addEventListener('abort', onSignalAbort, { once: true })
    try {
      const response = await fetchImpl(callbackUrl, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          sessionId: String(sessionId),
          toolName: req.toolName,
          callId: req.callId,
          reason: req.reason ?? null,
        }),
        signal: controller.signal,
      })
      if (!response.ok) {
        ctx.logger?.warn(`approval-relay: callback returned HTTP ${response.status}`)
        return 'rejected'
      }
      const body = await response.json()
      return ALLOWED_OUTCOMES.has(body?.outcome) ? body.outcome : 'rejected'
    } catch (error) {
      // Network failure, malformed body, or an abort (our own timeout, or
      // the tool call's own signal) — fail closed either way. Only
      // distinguish 'cancelled' from 'rejected' when the ORIGINAL request
      // signal (not our timeout) is what aborted, so the model sees the
      // right one of ApprovalService's two "closed" outcome messages.
      const reason = error instanceof Error ? error.message : String(error)
      ctx.logger?.warn(`approval-relay: callback failed, denying (${sessionId}/${req.callId}): ${reason}`)
      return req.signal?.aborted ? 'cancelled' : 'rejected'
    } finally {
      clearTimeout(timer)
      req.signal?.removeEventListener('abort', onSignalAbort)
    }
  })
}

export default { name, inject, apply }
