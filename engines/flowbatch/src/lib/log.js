const LEVELS = { debug: 10, info: 20, warn: 30, error: 40, silent: 99 };

let currentLevel = LEVELS.info;
let useColor = process.stdout.isTTY === true;

const COLOR = {
  reset: '\u001b[0m',
  dim: '\u001b[2m',
  red: '\u001b[31m',
  green: '\u001b[32m',
  yellow: '\u001b[33m',
  blue: '\u001b[34m',
  cyan: '\u001b[36m',
};

function paint(color, text) {
  if (!useColor) return text;
  return `${COLOR[color] ?? ''}${text}${COLOR.reset}`;
}

export function setLevel(level) {
  if (level == null) return;
  const normalized = String(level).toLowerCase();
  if (!(normalized in LEVELS)) {
    throw new Error(`Unknown log level "${level}". Use one of: ${Object.keys(LEVELS).join(', ')}`);
  }
  currentLevel = LEVELS[normalized];
}

export function setColor(enabled) {
  useColor = Boolean(enabled);
}

export function levelValue(level) {
  return LEVELS[String(level).toLowerCase()] ?? LEVELS.info;
}

function stamp() {
  return paint('dim', new Date().toISOString().slice(11, 19));
}

function emit(level, color, stream, args) {
  if (LEVELS[level] < currentLevel) return;
  stream.write(`${stamp()} ${paint(color, level.toUpperCase().padEnd(5))} ${args.map(stringify).join(' ')}\n`);
}

function stringify(value) {
  if (typeof value === 'string') return value;
  if (value instanceof Error) return value.stack ?? value.message;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export const log = {
  debug: (...args) => emit('debug', 'dim', process.stdout, args),
  info: (...args) => emit('info', 'blue', process.stdout, args),
  ok: (...args) => emit('info', 'green', process.stdout, args),
  warn: (...args) => emit('warn', 'yellow', process.stderr, args),
  error: (...args) => emit('error', 'red', process.stderr, args),
  /** Blank line plus a bold-ish heading, used to separate phases of a run. */
  heading: (text) => {
    if (LEVELS.info < currentLevel) return;
    process.stdout.write(`\n${paint('cyan', `== ${text} ==`)}\n`);
  },
  raw: (text) => {
    if (LEVELS.info < currentLevel) return;
    process.stdout.write(`${text}\n`);
  },
};
