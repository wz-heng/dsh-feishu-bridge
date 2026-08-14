/**
 * Poll the bridge's `/health` endpoint until it reports ready.
 * @module health
 */

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Poll `url` until it returns `{ status: 'ok' }` or `timeoutMs` elapses.
 * @param {object} opts
 * @param {string} opts.url
 * @param {number} opts.timeoutMs
 * @param {number} [opts.intervalMs]
 * @param {typeof fetch} [opts.fetchImpl] - injectable for tests.
 * @returns {Promise<unknown>} the parsed health body.
 */
export async function waitForHealthy({ url, timeoutMs, intervalMs = 300, fetchImpl = fetch }) {
  const deadline = Date.now() + timeoutMs
  let lastError = new Error(`health check timed out after ${timeoutMs}ms`)
  while (true) {
    try {
      const res = await fetchImpl(url, { signal: AbortSignal.timeout(Math.min(intervalMs * 3, 5000)) })
      if (res.ok) {
        const body = await res.json().catch(() => null)
        if (body && body.status === 'ok') return body
        lastError = new Error(`unexpected health body: ${JSON.stringify(body)}`)
      } else {
        lastError = new Error(`unhealthy response: HTTP ${res.status}`)
      }
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err))
    }
    const remaining = deadline - Date.now()
    if (remaining <= 0) throw lastError
    await sleep(Math.min(intervalMs, remaining))
  }
}
