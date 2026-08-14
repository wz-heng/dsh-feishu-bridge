import { EventEmitter } from 'node:events'

/** A fake ChildProcess: tracks kill() calls and simulates OS reaping on signal. */
export class FakeChild extends EventEmitter {
  constructor({ pid = 4242, respondsToSigterm = true } = {}) {
    super()
    this.pid = pid
    this.exitCode = null
    this.signalCode = null
    this.killCalls = []
    this.respondsToSigterm = respondsToSigterm
    this.stdout = new EventEmitter()
    this.stderr = new EventEmitter()
  }

  kill(signal) {
    this.killCalls.push(signal)
    const shouldDie = signal === 'SIGKILL' || (signal === 'SIGTERM' && this.respondsToSigterm)
    if (shouldDie) {
      queueMicrotask(() => {
        this.exitCode = signal === 'SIGKILL' ? null : 0
        this.signalCode = signal === 'SIGKILL' ? 'SIGKILL' : null
        this.emit('exit', this.exitCode, this.signalCode)
      })
    }
    return true
  }
}
