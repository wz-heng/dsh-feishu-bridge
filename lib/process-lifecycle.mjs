/**
 * Spawn and tear down the bridge's Python child process.
 *
 * The zombie-process discipline this project has been burned by before:
 * `killGracefully` always confirms the child actually exited (SIGTERM, wait,
 * SIGKILL escalation) rather than firing a signal and hoping.
 * @module process-lifecycle
 */

import { spawn } from 'node:child_process'

/**
 * Spawn `<pythonBin> -m dsh_feishu_bridge` with the given cwd/env.
 * @param {object} opts
 * @param {string} opts.pythonBin
 * @param {string} opts.cwd
 * @param {NodeJS.ProcessEnv} opts.env
 * @param {typeof spawn} [opts.spawnImpl] - injectable for tests.
 * @returns {import('node:child_process').ChildProcess}
 */
export function spawnBridge({ pythonBin, cwd, env, spawnImpl = spawn }) {
  return spawnImpl(pythonBin, ['-m', 'dsh_feishu_bridge'], {
    cwd,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function hasExited(child) {
  return child.exitCode !== null || child.signalCode !== null
}

function onceExit(child) {
  return new Promise((resolve) => child.once('exit', resolve))
}

/**
 * Terminate `child` and wait for confirmation: SIGTERM first, escalating to
 * SIGKILL if it has not exited within `graceMs`. A no-op if the child never
 * got a pid (spawn failed before starting) or has already exited.
 * @param {import('node:child_process').ChildProcess} child
 * @param {object} [opts]
 * @param {number} [opts.graceMs]
 * @returns {Promise<void>}
 */
export async function killGracefully(child, { graceMs = 5000 } = {}) {
  if (child.pid === undefined || hasExited(child)) return
  const exited = onceExit(child)
  child.kill('SIGTERM')
  const outcome = await Promise.race([exited.then(() => 'exited'), sleep(graceMs).then(() => 'timeout')])
  if (outcome === 'timeout' && !hasExited(child)) {
    child.kill('SIGKILL')
    await exited
  }
}
