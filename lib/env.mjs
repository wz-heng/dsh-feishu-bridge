/**
 * Minimal dotenv reader for the dsh plugin shell.
 *
 * The Python side (`dsh_feishu_bridge.config`) reads credentials straight
 * from `process.env` and never loads `.env` itself (see its module
 * docstring) — a repo-root `.env` only takes effect if *something* parses
 * it into the child's environment before spawning. That something is this
 * file, not a `python-dotenv` dependency, so the Python package stays
 * untouched.
 * @module env
 */

/**
 * Parse dotenv-format text into a plain key/value object.
 *
 * Supports `KEY=value` lines, blank lines, `#`-prefixed comments, and a
 * single layer of matching single/double quotes around the value. Anything
 * else (multi-line values, `export` prefixes, variable expansion) is out of
 * scope — the bridge's own `.env` only ever needs flat scalars.
 * @param {string} text
 * @returns {Record<string, string>}
 */
export function parseDotEnv(text) {
  /** @type {Record<string, string>} */
  const result = {}
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (line === '' || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq === -1) continue
    const key = line.slice(0, eq).trim()
    if (key === '') continue
    let value = line.slice(eq + 1).trim()
    const quoted = value.length >= 2
      && ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))
    if (quoted) value = value.slice(1, -1)
    result[key] = value
  }
  return result
}

/**
 * Read and parse a dotenv file, returning `{}` when it does not exist.
 * @param {string} path
 * @param {(path: string) => string} readFile - injectable for tests.
 * @returns {Record<string, string>}
 */
export function readDotEnv(path, readFile) {
  try {
    return parseDotEnv(readFile(path))
  } catch (err) {
    if (err && err.code === 'ENOENT') return {}
    throw err
  }
}
