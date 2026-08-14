import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseDotEnv, readDotEnv } from '../lib/env.mjs'

test('parseDotEnv reads KEY=value pairs', () => {
  const result = parseDotEnv('FOO=bar\nBAZ=qux')
  assert.deepEqual(result, { FOO: 'bar', BAZ: 'qux' })
})

test('parseDotEnv skips blank lines and comments', () => {
  const result = parseDotEnv('# a comment\n\nFOO=bar\n  # indented comment\nBAZ=qux\n')
  assert.deepEqual(result, { FOO: 'bar', BAZ: 'qux' })
})

test('parseDotEnv strips one layer of matching quotes', () => {
  const result = parseDotEnv('FOO="bar baz"\nSINGLE=\'quux\'\nUNQUOTED=plain')
  assert.deepEqual(result, { FOO: 'bar baz', SINGLE: 'quux', UNQUOTED: 'plain' })
})

test('parseDotEnv ignores lines with no "="', () => {
  const result = parseDotEnv('not-a-line\nFOO=bar')
  assert.deepEqual(result, { FOO: 'bar' })
})

test('parseDotEnv trims whitespace around key and value', () => {
  const result = parseDotEnv('  FOO   =   bar  ')
  assert.deepEqual(result, { FOO: 'bar' })
})

test('parseDotEnv keeps "=" characters inside the value', () => {
  const result = parseDotEnv('FOO=a=b=c')
  assert.deepEqual(result, { FOO: 'a=b=c' })
})

test('readDotEnv returns {} when the file does not exist', () => {
  const readFile = () => {
    const err = new Error('not found')
    err.code = 'ENOENT'
    throw err
  }
  assert.deepEqual(readDotEnv('/nonexistent/.env', readFile), {})
})

test('readDotEnv rethrows non-ENOENT errors', () => {
  const readFile = () => {
    throw new Error('permission denied')
  }
  assert.throws(() => readDotEnv('/no-access/.env', readFile), /permission denied/)
})

test('readDotEnv parses the file content via the injected reader', () => {
  const readFile = (path) => {
    assert.equal(path, '/repo/.env')
    return 'FEISHU_APP_ID=cli_test\n'
  }
  assert.deepEqual(readDotEnv('/repo/.env', readFile), { FEISHU_APP_ID: 'cli_test' })
})
