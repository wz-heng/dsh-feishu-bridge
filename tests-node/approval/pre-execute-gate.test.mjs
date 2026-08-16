import { test } from 'node:test'
import assert from 'node:assert/strict'
import { apply, name } from '../../src/dsh_feishu_bridge/approval_runtime/pre-execute-gate.mjs'

function makeCtx() {
  const handlers = new Map()
  const ctx = {
    on(event, handler) {
      handlers.set(event, handler)
      return () => handlers.delete(event)
    },
  }
  return { ctx, handlers }
}

const NEXT_ALLOW = () => ({ kind: 'allow' })

test('plugin name is stable', () => {
  assert.equal(name, 'approval-pre-execute-gate')
})

test('gates the default tool list (bash) with kind:ask', () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx)
  const handler = handlers.get('tools/pre-execute')
  const result = handler({ name: 'bash', arguments: { command: 'ls -la' } }, NEXT_ALLOW)
  assert.deepEqual(result, { kind: 'ask', reason: 'ls -la' })
})

test('passes through an ungated tool via next()', () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx)
  const handler = handlers.get('tools/pre-execute')
  const result = handler({ name: 'job_output', arguments: {} }, NEXT_ALLOW)
  assert.deepEqual(result, { kind: 'allow' })
})

test('config.tools overrides the default gated set', () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx, { tools: ['bash', 'str_replace_editor'] })
  const handler = handlers.get('tools/pre-execute')
  assert.deepEqual(
    handler({ name: 'str_replace_editor', arguments: { file_path: '/etc/passwd' } }, NEXT_ALLOW),
    { kind: 'ask', reason: '/etc/passwd' }
  )
  assert.deepEqual(handler({ name: 'bash', arguments: {} }, NEXT_ALLOW), { kind: 'ask', reason: 'bash call' })
})

test('an empty override list gates nothing', () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx, { tools: [] })
  const handler = handlers.get('tools/pre-execute')
  assert.deepEqual(handler({ name: 'bash', arguments: { command: 'rm -rf /' } }, NEXT_ALLOW), { kind: 'allow' })
})

test('summary falls back to a generic label with no command/path field', () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx)
  const handler = handlers.get('tools/pre-execute')
  const result = handler({ name: 'bash', arguments: { foo: 'bar' } }, NEXT_ALLOW)
  assert.deepEqual(result, { kind: 'ask', reason: 'bash call' })
})

test('summary truncates a very long command', () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx)
  const handler = handlers.get('tools/pre-execute')
  const longCommand = 'x'.repeat(600)
  const result = handler({ name: 'bash', arguments: { command: longCommand } }, NEXT_ALLOW)
  assert.equal(result.kind, 'ask')
  assert.ok(result.reason.length <= 501)
  assert.ok(result.reason.endsWith('…'))
})

test('missing arguments does not throw', () => {
  const { ctx, handlers } = makeCtx()
  apply(ctx)
  const handler = handlers.get('tools/pre-execute')
  const result = handler({ name: 'bash', arguments: undefined }, NEXT_ALLOW)
  assert.deepEqual(result, { kind: 'ask', reason: 'bash call' })
})
