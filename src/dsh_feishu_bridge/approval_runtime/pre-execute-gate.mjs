/**
 * Cordis plugin: marks selected tool calls as needing human approval before
 * they execute.
 *
 * This is deliberately independent of the mounted bash executor's
 * confinement (`@deepseek-ai/dsh-bash-local` vs `-sandbox`) — the gate is
 * `tools/pre-execute`, the SAME extension point `@deepseek-ai/dsh-tool-bash`'s
 * own source names for "deployment policy" (its module docstring: "deployment
 * policy belongs in tools/pre-execute and sandboxing executors"). Composing
 * `@deepseek-ai/dsh-permission-presets` or a sandboxing executor is NOT
 * required: this plugin alone, plus `@deepseek-ai/dsh-user-approval`, is
 * enough to gate every call to a named tool. Verified by reading the
 * compiled runtime's own source (`deepseek-harness-runtime-bin`) — see the
 * task's Phase 1 recon comment for the exact call chain
 * (`tools/pre-execute` → `{kind:'ask'}` → tool registry's `serviceAsk()` →
 * `ctx.approval.request()` → the `approval/request` waterfall this
 * composition's `approval-relay.mjs` answers).
 *
 * A listener on this waterfall receives `(exec, next)`: returning `next()`
 * passes the call through unchanged; returning a `PreToolDecision` (here,
 * always `{kind:'ask', reason}` for a gated tool) short-circuits it.
 * @module dsh-feishu-bridge/approval-runtime/pre-execute-gate
 */

const DEFAULT_GATED_TOOLS = Object.freeze(['bash'])

// Long enough to show a real command/path, short enough that a card can
// never carry an enormous or sensitive blob straight from model-controlled
// tool arguments.
const REASON_MAX_CHARS = 500

/** Stable cordis plugin name. */
export const name = 'approval-pre-execute-gate'

/** No hard service dependencies — this only needs the `tools/pre-execute` event. */
export const inject = []

/**
 * @typedef {object} Config
 * @property {string[]} [tools] - tool names requiring approval before execution. Defaults to `['bash']`.
 */

/**
 * Truncate to {@link REASON_MAX_CHARS}, appending an ellipsis when cut.
 * @param {string} text
 * @returns {string}
 */
function truncate(text) {
  return text.length > REASON_MAX_CHARS ? `${text.slice(0, REASON_MAX_CHARS)}…` : text
}

/**
 * Build a short, human-readable summary of a gated tool call for the
 * approval card — never the raw arguments object verbatim (it may carry
 * large or sensitive content the model produced), just the command/path
 * shaped field a reviewer actually needs to decide.
 * @param {string} toolName
 * @param {Record<string, unknown> | undefined} args
 * @returns {string}
 */
function summarize(toolName, args) {
  if (args && typeof args === 'object') {
    if (typeof args.command === 'string') return truncate(args.command)
    const path = typeof args.file_path === 'string' ? args.file_path : args.path
    if (typeof path === 'string') return truncate(path)
  }
  return truncate(`${toolName} call`)
}

/**
 * Plugin entry.
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {Config} [config]
 */
export function apply(ctx, config = {}) {
  const gated = new Set(config.tools ?? DEFAULT_GATED_TOOLS)
  ctx.on('tools/pre-execute', (exec, next) => {
    if (!gated.has(exec.name)) return next()
    return { kind: 'ask', reason: summarize(exec.name, exec.arguments) }
  })
}

export default { name, inject, apply }
