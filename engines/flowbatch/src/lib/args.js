/**
 * Minimal argv parser.
 *
 * `--key=value` always works. `--key value` works only when `key` is listed in
 * `valueFlags`, which keeps boolean flags from swallowing the next token.
 * `--no-key` sets `key: false`.
 */
export function parseArgs(argv, { valueFlags = [] } = {}) {
  const values = new Set(valueFlags);
  const flags = {};
  const positionals = [];

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];

    if (token === '--') {
      positionals.push(...argv.slice(index + 1));
      break;
    }

    if (!token.startsWith('--')) {
      positionals.push(token);
      continue;
    }

    const body = token.slice(2);

    if (body.startsWith('no-')) {
      flags[body.slice(3)] = false;
      continue;
    }

    const eq = body.indexOf('=');
    if (eq !== -1) {
      flags[body.slice(0, eq)] = body.slice(eq + 1);
      continue;
    }

    if (values.has(body)) {
      const next = argv[index + 1];
      if (next === undefined || next.startsWith('--')) {
        throw new Error(`Flag --${body} requires a value.`);
      }
      // Repeating a value flag collects the values into an array.
      if (body in flags) {
        flags[body] = Array.isArray(flags[body]) ? [...flags[body], next] : [flags[body], next];
      } else {
        flags[body] = next;
      }
      index += 1;
      continue;
    }

    flags[body] = true;
  }

  return { flags, positionals };
}

export function flagValue(flags, name, fallback = undefined) {
  const value = flags[name];
  if (value === undefined) return fallback;
  if (value === true) return true;
  if (value === false) return false;
  return value;
}

export function intFlag(flags, name, fallback) {
  const value = flags[name];
  if (value === undefined) return fallback;
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed)) throw new Error(`Flag --${name} must be an integer, got "${value}".`);
  return parsed;
}

export function listFlag(flags, name) {
  const value = flags[name];
  if (value === undefined || value === true) return null;
  const parts = Array.isArray(value) ? value : String(value).split(',');
  return parts.map((part) => String(part).trim()).filter(Boolean);
}

/** Value flag that may be repeated, returned as a flat array. */
export function repeatFlag(flags, name) {
  const value = flags[name];
  if (value === undefined || value === true) return [];
  return (Array.isArray(value) ? value : [value]).map((part) => String(part));
}
