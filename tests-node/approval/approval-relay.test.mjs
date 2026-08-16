import { test } from 'node:test'
import assert from 'node:assert/strict'
import { apply, name } from '../../src/dsh_feishu_bridge/approval_runtime/approval-relay.mjs'

function makeCtx() {
  const handlers = new Map()
  const warnings = []
  const ctx = {
    on(event, handler) {
      handlers.set(event, handler)
      return () => handlers.delete(event)
    },
    logger: { warn: (msg) => warnings.push(msg) },
  }
  return { ctx, handlers, warnings }
}

function baseReq(overrides = {}) {
  return {
    agent: { session: { id: 'sess-1' } },
    toolName: 'bash',
    callId: 'call-1',
    reason: 'ls -la',
    signal: undefined,
    ...overrides,
  }
}

const NEXT_UNAVAILABLE = async () => 'unavailable'

/** A fetchImpl that resolves immediately with a canned outcome body. */
function fakeFetchResolving(outcome, { ok = true, status = 200 } = {}) {
  const calls = []
  const fetchImpl = async (url, options) => {
    calls.push({ url, options, body: JSON.parse(options.body) })
    return {
      ok,
      status,
      json: async () => ({ outcome }),
    }
  }
  fetchImpl.calls = calls
  return fetchImpl
}

/** A fetchImpl that only settles (by rejecting, like real fetch does) once
 * its AbortSignal fires — simulates a hung network call. */
function fakeFetchHangingUntilAborted() {
  return async (url, options) =>
    new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => {
        const err = new Error('The operation was aborted')
        err.name = 'AbortError'
        reject(err)
      })
    })
}

test('plugin name is stable', () => {
  assert.equal(name, 'approval-relay')
})

test('refuses to load without a callback URL', () => {
  const { ctx } = makeCtx()
  assert.throws(() => apply(ctx, {}), /DSH_APPROVAL_CALLBACK_URL/)
})

test('falls through to next() when the request has no callId', async () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx, { callbackUrl: 'http://127.0.0.1:1', internals: { fetchImpl: fakeFetchResolving('allowed-once') } })
  const handler = handlers.get('approval/request')
  const outcome = await handler(baseReq({ callId: undefined }), NEXT_UNAVAILABLE)
  assert.equal(outcome, 'unavailable')
})

test('falls through to next() when the request has no session id', async () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx, { callbackUrl: 'http://127.0.0.1:1', internals: { fetchImpl: fakeFetchResolving('allowed-once') } })
  const handler = handlers.get('approval/request')
  const outcome = await handler(baseReq({ agent: undefined }), NEXT_UNAVAILABLE)
  assert.equal(outcome, 'unavailable')
})

test('posts sessionId/toolName/callId/reason and returns the callback outcome', async () => {
  const { ctx, handlers } = makeCtx()
  const fetchImpl = fakeFetchResolving('allowed-once')
  apply(ctx, { callbackUrl: 'http://127.0.0.1:9999', internals: { fetchImpl } })
  const handler = handlers.get('approval/request')
  const outcome = await handler(baseReq(), NEXT_UNAVAILABLE)
  assert.equal(outcome, 'allowed-once')
  assert.equal(fetchImpl.calls.length, 1)
  assert.equal(fetchImpl.calls[0].url, 'http://127.0.0.1:9999')
  assert.deepEqual(fetchImpl.calls[0].body, {
    sessionId: 'sess-1',
    toolName: 'bash',
    callId: 'call-1',
    reason: 'ls -la',
  })
})

test('null reason is relayed as null, not omitted', async () => {
  const { ctx, handlers } = makeCtx()
  const fetchImpl = fakeFetchResolving('rejected')
  apply(ctx, { callbackUrl: 'http://127.0.0.1:9999', internals: { fetchImpl } })
  const handler = handlers.get('approval/request')
  await handler(baseReq({ reason: undefined }), NEXT_UNAVAILABLE)
  assert.equal(fetchImpl.calls[0].body.reason, null)
})

test('denies on a non-2xx callback response', async () => {
  const { ctx, handlers, warnings } = makeCtx()
  apply(ctx, {
    callbackUrl: 'http://127.0.0.1:9999',
    internals: { fetchImpl: fakeFetchResolving('allowed-once', { ok: false, status: 500 }) },
  })
  const handler = handlers.get('approval/request')
  const outcome = await handler(baseReq(), NEXT_UNAVAILABLE)
  assert.equal(outcome, 'rejected')
  assert.ok(warnings.some((w) => w.includes('500')))
})

test('denies on an outcome the closed vocabulary does not recognize', async () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx, {
    callbackUrl: 'http://127.0.0.1:9999',
    internals: { fetchImpl: fakeFetchResolving('something-unexpected') },
  })
  const handler = handlers.get('approval/request')
  const outcome = await handler(baseReq(), NEXT_UNAVAILABLE)
  assert.equal(outcome, 'rejected')
})

test('denies on a network error', async () => {
  const { ctx, handlers } = makeCtx()
  const fetchImpl = async () => {
    throw new Error('ECONNREFUSED')
  }
  apply(ctx, { callbackUrl: 'http://127.0.0.1:1', internals: { fetchImpl } })
  const handler = handlers.get('approval/request')
  const outcome = await handler(baseReq(), NEXT_UNAVAILABLE)
  assert.equal(outcome, 'rejected')
})

test('denies on its own timeout (fail-closed)', async () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx, {
    callbackUrl: 'http://127.0.0.1:1',
    timeoutMs: 20,
    internals: { fetchImpl: fakeFetchHangingUntilAborted() },
  })
  const handler = handlers.get('approval/request')
  const outcome = await handler(baseReq(), NEXT_UNAVAILABLE)
  assert.equal(outcome, 'rejected')
})

test("reports 'cancelled' when the ORIGINAL request signal aborts, not our timeout", async () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx, {
    callbackUrl: 'http://127.0.0.1:1',
    timeoutMs: 30_000,
    internals: { fetchImpl: fakeFetchHangingUntilAborted() },
  })
  const handler = handlers.get('approval/request')
  const controller = new AbortController()
  const pending = handler(baseReq({ signal: controller.signal }), NEXT_UNAVAILABLE)
  controller.abort()
  assert.equal(await pending, 'cancelled')
})

test('reads DSH_APPROVAL_CALLBACK_URL / DSH_APPROVAL_TIMEOUT_MS from the environment', async () => {
  const { ctx, handlers } = makeCtx()
  const fetchImpl = fakeFetchResolving('allowed-once')
  const previousUrl = process.env.DSH_APPROVAL_CALLBACK_URL
  const previousTimeout = process.env.DSH_APPROVAL_TIMEOUT_MS
  process.env.DSH_APPROVAL_CALLBACK_URL = 'http://127.0.0.1:4242'
  process.env.DSH_APPROVAL_TIMEOUT_MS = '15000'
  try {
    apply(ctx, { internals: { fetchImpl } })
    const handler = handlers.get('approval/request')
    await handler(baseReq(), NEXT_UNAVAILABLE)
    assert.equal(fetchImpl.calls[0].url, 'http://127.0.0.1:4242')
  } finally {
    if (previousUrl === undefined) delete process.env.DSH_APPROVAL_CALLBACK_URL
    else process.env.DSH_APPROVAL_CALLBACK_URL = previousUrl
    if (previousTimeout === undefined) delete process.env.DSH_APPROVAL_TIMEOUT_MS
    else process.env.DSH_APPROVAL_TIMEOUT_MS = previousTimeout
  }
})
