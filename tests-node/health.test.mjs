import { test } from 'node:test'
import assert from 'node:assert/strict'
import { waitForHealthy } from '../lib/health.mjs'

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

test('waitForHealthy resolves immediately on a healthy first response', async () => {
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    return jsonResponse(200, { status: 'ok', feishu_healthy: true })
  }
  const body = await waitForHealthy({ url: 'http://x/health', timeoutMs: 1000, intervalMs: 10, fetchImpl })
  assert.deepEqual(body, { status: 'ok', feishu_healthy: true })
  assert.equal(calls, 1)
})

test('waitForHealthy retries until healthy', async () => {
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    if (calls < 3) return jsonResponse(503, {})
    return jsonResponse(200, { status: 'ok' })
  }
  const body = await waitForHealthy({ url: 'http://x/health', timeoutMs: 1000, intervalMs: 5, fetchImpl })
  assert.deepEqual(body, { status: 'ok' })
  assert.equal(calls, 3)
})

test('waitForHealthy retries through connection errors (ECONNREFUSED before the server binds)', async () => {
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    if (calls < 2) throw new Error('connect ECONNREFUSED')
    return jsonResponse(200, { status: 'ok' })
  }
  await waitForHealthy({ url: 'http://x/health', timeoutMs: 1000, intervalMs: 5, fetchImpl })
  assert.equal(calls, 2)
})

test('waitForHealthy throws after timeoutMs elapses without a healthy response', async () => {
  const fetchImpl = async () => jsonResponse(500, {})
  await assert.rejects(
    waitForHealthy({ url: 'http://x/health', timeoutMs: 30, intervalMs: 10, fetchImpl }),
    /unhealthy response/,
  )
})

test('waitForHealthy treats a 200 with the wrong body shape as unhealthy', async () => {
  const fetchImpl = async () => jsonResponse(200, { status: 'starting' })
  await assert.rejects(
    waitForHealthy({ url: 'http://x/health', timeoutMs: 30, intervalMs: 10, fetchImpl }),
    /unexpected health body/,
  )
})

test('waitForHealthy rejects promptly when aborted mid-sleep, without waiting out timeoutMs', async () => {
  const controller = new AbortController()
  const fetchImpl = async () => jsonResponse(503, {})
  const promise = waitForHealthy({
    url: 'http://x/health',
    timeoutMs: 5000,
    intervalMs: 2000,
    fetchImpl,
    signal: controller.signal,
  })
  const started = Date.now()
  // Abort well before the first retry's sleep would naturally elapse.
  setTimeout(() => controller.abort(), 20)
  await assert.rejects(promise, /aborted/)
  assert.ok(Date.now() - started < 500, 'abort must interrupt the sleep, not wait out intervalMs/timeoutMs')
})

test('waitForHealthy rejects immediately when already aborted before the first attempt', async () => {
  const controller = new AbortController()
  controller.abort()
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    return jsonResponse(200, { status: 'ok' })
  }
  await assert.rejects(
    waitForHealthy({ url: 'http://x/health', timeoutMs: 1000, intervalMs: 10, fetchImpl, signal: controller.signal }),
    /aborted/,
  )
  assert.equal(calls, 0, 'an already-aborted signal must skip fetching entirely')
})

test('waitForHealthy leaves no pending sleep timer after abort (process can exit promptly)', async () => {
  const controller = new AbortController()
  const fetchImpl = async () => jsonResponse(503, {})
  const promise = waitForHealthy({
    url: 'http://x/health',
    timeoutMs: 60_000,
    intervalMs: 30_000,
    fetchImpl,
    signal: controller.signal,
  })
  await new Promise((resolve) => setTimeout(resolve, 10))
  controller.abort()
  await assert.rejects(promise, /aborted/)
  // If the sleep's setTimeout were still pending and referenced, this test
  // file's process would hang for up to 30s waiting for it — the original
  // bug this fixes. Reaching this point quickly is the assertion.
})
