import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnBridge, killGracefully } from '../lib/process-lifecycle.mjs'
import { FakeChild } from './helpers/fake-child.mjs'

test('spawnBridge invokes python -m dsh_feishu_bridge with the given cwd/env', () => {
  const calls = []
  const spawnImpl = (cmd, args, opts) => {
    calls.push({ cmd, args, opts })
    return new FakeChild()
  }
  spawnBridge({ pythonBin: '/repo/.venv/bin/python', cwd: '/repo', env: { FOO: 'bar' }, spawnImpl })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].cmd, '/repo/.venv/bin/python')
  assert.deepEqual(calls[0].args, ['-m', 'dsh_feishu_bridge'])
  assert.equal(calls[0].opts.cwd, '/repo')
  assert.deepEqual(calls[0].opts.env, { FOO: 'bar' })
})

test('killGracefully sends SIGTERM and resolves once the child confirms exit', async () => {
  const child = new FakeChild()
  await killGracefully(child, { graceMs: 1000 })
  assert.deepEqual(child.killCalls, ['SIGTERM'])
  assert.equal(child.exitCode, 0)
})

test('killGracefully escalates to SIGKILL when the child ignores SIGTERM', async () => {
  const child = new FakeChild({ respondsToSigterm: false })
  await killGracefully(child, { graceMs: 15 })
  assert.deepEqual(child.killCalls, ['SIGTERM', 'SIGKILL'])
  assert.equal(child.signalCode, 'SIGKILL')
})

test('killGracefully is a no-op once the child has already exited', async () => {
  const child = new FakeChild()
  child.exitCode = 0
  await killGracefully(child, { graceMs: 1000 })
  assert.deepEqual(child.killCalls, [])
})

test('killGracefully is a no-op when spawn never produced a pid', async () => {
  const child = new FakeChild()
  // A default constructor param can't represent "no pid" via `{ pid: undefined }" —
  // JS treats an explicit `undefined` the same as an omitted key and re-applies the
  // default. Overwrite the property directly to model a spawn that never got a pid.
  child.pid = undefined
  await killGracefully(child, { graceMs: 1000 })
  assert.deepEqual(child.killCalls, [])
})

test('killGracefully leaves no pending child process behind after escalation (no zombie)', async () => {
  const child = new FakeChild({ respondsToSigterm: false })
  await killGracefully(child, { graceMs: 15 })
  assert.notEqual(child.signalCode, null, 'child must report a terminal signal, not hang indefinitely')
})
