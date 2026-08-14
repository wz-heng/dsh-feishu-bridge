import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply, name } from '../lib/index.mjs'
import { FakeChild } from './helpers/fake-child.mjs'

function makeCtx() {
  const logs = { info: [], warn: [] }
  const ctx = {
    logger: {
      info: (msg) => logs.info.push(msg),
      warn: (msg) => logs.warn.push(msg),
    },
    dispose: undefined,
    effect(setup) {
      const result = setup()
      if (typeof result === 'function') ctx.dispose = result
      return result
    },
  }
  return { ctx, logs }
}

function neverHealthy() {
  // Resolves the health promise's rejection branch quickly without a real timer wait.
  return async () => { throw new Error('connect ECONNREFUSED') }
}

test('plugin name matches the cordis.patch.yml insert row', () => {
  assert.equal(name, 'dsh-feishu-bridge')
})

test('apply spawns python -m dsh_feishu_bridge with cwd=repoRoot and merged env', async () => {
  const { ctx } = makeCtx()
  const spawnCalls = []
  const spawnImpl = (cmd, args, opts) => {
    spawnCalls.push({ cmd, args, opts })
    return new FakeChild()
  }
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: '/repo/.venv/bin/python',
    port: 8788,
    env: { EXTRA: '1' },
    internals: {
      spawnImpl,
      fetchImpl: neverHealthy(),
      readFile: () => 'FROM_DOTENV=yes\n',
    },
  })
  assert.equal(spawnCalls.length, 1)
  assert.equal(spawnCalls[0].cmd, '/repo/.venv/bin/python')
  assert.deepEqual(spawnCalls[0].args, ['-m', 'dsh_feishu_bridge'])
  assert.equal(spawnCalls[0].opts.cwd, '/repo')
  assert.equal(spawnCalls[0].opts.env.FROM_DOTENV, 'yes')
  assert.equal(spawnCalls[0].opts.env.EXTRA, '1')
})

test('apply: explicit config.env overrides the .env file for the same key', async () => {
  const { ctx } = makeCtx()
  let capturedEnv
  const spawnImpl = (cmd, args, opts) => {
    capturedEnv = opts.env
    return new FakeChild()
  }
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: '/repo/.venv/bin/python',
    env: { FEISHU_TRANSPORT: 'webhook' },
    internals: {
      spawnImpl,
      fetchImpl: neverHealthy(),
      readFile: () => 'FEISHU_TRANSPORT=ws\n',
    },
  })
  assert.equal(capturedEnv.FEISHU_TRANSPORT, 'webhook')
})

test('apply resolves the health-check URL against 127.0.0.1 when host is 0.0.0.0', async () => {
  const { ctx } = makeCtx()
  const urlsProbed = []
  const spawnImpl = () => new FakeChild()
  const fetchImpl = async (url) => {
    urlsProbed.push(url)
    return { ok: true, status: 200, json: async () => ({ status: 'ok' }) }
  }
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: 'python3',
    host: '0.0.0.0',
    port: 9999,
    internals: { spawnImpl, fetchImpl, readFile: () => '' },
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(urlsProbed[0], 'http://127.0.0.1:9999/health')
})

test('apply logs a warning when the health check never succeeds within the timeout', async () => {
  const { ctx, logs } = makeCtx()
  const spawnImpl = () => new FakeChild()
  const fetchImpl = async () => { throw new Error('connect ECONNREFUSED') }
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: 'python3',
    startupTimeoutMs: 20,
    internals: { spawnImpl, fetchImpl, readFile: () => '' },
  })
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.ok(logs.warn.some((line) => line.includes('did not become healthy')), logs.warn.join('\n'))
})

test('apply logs healthy once the health check succeeds', async () => {
  const { ctx, logs } = makeCtx()
  const spawnImpl = () => new FakeChild()
  const fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok' }) })
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: 'python3',
    internals: { spawnImpl, fetchImpl, readFile: () => '' },
  })
  await new Promise((resolve) => setImmediate(resolve))
  await new Promise((resolve) => setImmediate(resolve))
  assert.ok(logs.info.some((line) => line.includes('healthy at')), logs.info.join('\n'))
})

test('dispose (ctx.effect cleanup) kills the child and waits for confirmation', async () => {
  const { ctx } = makeCtx()
  let child
  const spawnImpl = () => {
    child = new FakeChild()
    return child
  }
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: 'python3',
    internals: { spawnImpl, fetchImpl: neverHealthy(), readFile: () => '' },
  })
  assert.equal(typeof ctx.dispose, 'function')
  await ctx.dispose()
  assert.deepEqual(child.killCalls, ['SIGTERM'])
  assert.equal(child.exitCode, 0)
})

test('an unexpected exit before dispose logs a warning; a dispose-triggered exit does not', async () => {
  const { ctx, logs } = makeCtx()
  let child
  const spawnImpl = () => {
    child = new FakeChild()
    return child
  }
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: 'python3',
    internals: { spawnImpl, fetchImpl: neverHealthy(), readFile: () => '' },
  })
  child.emit('exit', 1, null)
  assert.ok(logs.warn.some((line) => line.includes('exited unexpectedly')))

  logs.warn.length = 0
  await ctx.dispose()
  assert.ok(!logs.warn.some((line) => line.includes('exited unexpectedly')))
})

test('apply relays child stdout/stderr lines through ctx.logger', () => {
  const { ctx, logs } = makeCtx()
  let child
  const spawnImpl = () => {
    child = new FakeChild()
    return child
  }
  apply(ctx, {
    repoRoot: '/repo',
    pythonBin: 'python3',
    internals: { spawnImpl, fetchImpl: neverHealthy(), readFile: () => '' },
  })
  child.stdout.emit('data', Buffer.from('bot identity resolved — open_id=ou_x\n'))
  child.stderr.emit('data', Buffer.from('WARNING: deprecated flag\n'))
  assert.ok(logs.info.some((line) => line.includes('bot identity resolved')))
  assert.ok(logs.warn.some((line) => line.includes('deprecated flag')))
})

test('apply defaults to the repo-root .venv interpreter when it exists and no pythonBin is configured', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-feishu-bridge-test-'))
  try {
    await mkdir(join(root, '.venv', 'bin'), { recursive: true })
    await writeFile(join(root, '.venv', 'bin', 'python'), '#!/bin/sh\n', { mode: 0o755 })
    const { ctx } = makeCtx()
    const spawnCalls = []
    const spawnImpl = (cmd, args, opts) => {
      spawnCalls.push({ cmd, args, opts })
      return new FakeChild()
    }
    apply(ctx, { repoRoot: root, internals: { spawnImpl, fetchImpl: neverHealthy(), readFile: () => '' } })
    assert.equal(spawnCalls[0].cmd, join(root, '.venv', 'bin', 'python'))
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('apply falls back to "python3" on PATH when no repo-root .venv exists and no pythonBin is configured', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-feishu-bridge-test-'))
  try {
    const { ctx } = makeCtx()
    const spawnCalls = []
    const spawnImpl = (cmd, args, opts) => {
      spawnCalls.push({ cmd, args, opts })
      return new FakeChild()
    }
    apply(ctx, { repoRoot: root, internals: { spawnImpl, fetchImpl: neverHealthy(), readFile: () => '' } })
    assert.equal(spawnCalls[0].cmd, 'python3')
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
