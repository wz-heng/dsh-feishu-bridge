/**
 * Poll the bridge's `/health` endpoint until it reports ready.
 * @module health
 */

function abortError() {
  const err = new Error('health check aborted')
  err.name = 'AbortError'
  return err
}

/** A `setTimeout` that rejects immediately (and clears the timer) if `signal` aborts mid-sleep. */
function sleepAbortable(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    function onAbort() {
      clearTimeout(timer)
      reject(abortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * Poll `url` until it returns `{ status: 'ok' }` or `timeoutMs` elapses.
 *
 * `signal`, when given, stops the loop promptly on dispose: it is checked
 * before every attempt and interrupts the inter-attempt sleep immediately,
 * so a caller that aborts never waits out the remaining `timeoutMs` (up to
 * 30s by default) for cleanup to complete. An attempt already in flight is
 * bounded by its own short per-request timeout regardless, so the worst
 * case after abort is that one in-flight fetch, not the whole poll.
 * @param {object} opts
 * @param {string} opts.url
 * @param {number} opts.timeoutMs
 * @param {number} [opts.intervalMs]
 * @param {typeof fetch} [opts.fetchImpl] - injectable for tests.
 * @param {AbortSignal} [opts.signal] - stop polling early (plugin dispose).
 * @returns {Promise<unknown>} the parsed health body.
 */
export async function waitForHealthy({ url, timeoutMs, intervalMs = 300, fetchImpl = fetch, signal }) {
  const deadline = Date.now() + timeoutMs
  let lastError = new Error(`health check timed out after ${timeoutMs}ms`)
  while (true) {
    if (signal?.aborted) throw abortError()
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
    if (signal?.aborted) throw abortError()
    const remaining = deadline - Date.now()
    if (remaining <= 0) throw lastError
    await sleepAbortable(Math.min(intervalMs, remaining), signal)
  }
}
