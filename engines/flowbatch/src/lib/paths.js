import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(fileURLToPath(new URL('../../', import.meta.url)));

const DEFAULTS = {
  profileDir: 'profile',
  outputDir: 'output',
  refsDir: 'refs',
  stateDir: 'state',
  debugDir: 'debug',
  discoverDir: 'discover',
  downloadsDir: 'downloads',
};

/**
 * Resolve a configured path. Absolute paths are returned untouched, relative
 * paths are resolved against the project root.
 */
export function fromRoot(value) {
  if (!value) return null;
  return path.isAbsolute(value) ? path.normalize(value) : path.resolve(ROOT, value);
}

/**
 * Build the full directory map from the `paths` block of settings.
 */
export function buildDirs(pathConfig = {}) {
  const merged = { ...DEFAULTS, ...pathConfig };
  const dirs = {};
  for (const [key, value] of Object.entries(merged)) {
    dirs[key] = fromRoot(value);
  }
  dirs.root = ROOT;
  dirs.config = path.join(ROOT, 'config');
  return dirs;
}

export function ensureDirs(dirs, keys = Object.keys(dirs)) {
  for (const key of keys) {
    const dir = dirs[key];
    if (typeof dir === 'string' && dir.length > 0) {
      fs.mkdirSync(dir, { recursive: true });
    }
  }
}

export function ensureParent(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

/**
 * Make a filesystem-safe slug out of an arbitrary id.
 */
export function slugify(value, fallback = 'item') {
  const slug = String(value ?? '')
    .normalize('NFKD')
    .replace(/[^\w.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
  return slug || fallback;
}
