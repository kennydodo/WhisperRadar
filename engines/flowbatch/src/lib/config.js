import path from 'node:path';
import { readJson } from './json.js';
import { ConfigError } from './errors.js';
import { buildDirs, ensureDirs } from './paths.js';

export function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Recursively merge `override` onto `base`. Arrays and scalars replace.
 */
export function deepMerge(base, override) {
  if (!isPlainObject(base) || !isPlainObject(override)) {
    return override === undefined ? base : override;
  }
  const result = { ...base };
  for (const [key, value] of Object.entries(override)) {
    result[key] = key in base ? deepMerge(base[key], value) : value;
  }
  return result;
}

function stripPrivate(value) {
  if (!isPlainObject(value)) return value;
  const result = {};
  for (const [key, entry] of Object.entries(value)) {
    if (key.startsWith('_')) continue;
    result[key] = entry;
  }
  return result;
}

export function loadSettings(configDir) {
  const basePath = path.join(configDir, 'settings.json');
  const localPath = path.join(configDir, 'settings.local.json');

  const base = readJson(basePath);
  if (!isPlainObject(base)) throw new ConfigError(`${basePath} must contain a JSON object.`);

  const local = readJson(localPath, { required: false });
  const merged = deepMerge(base, local ?? {});
  merged.dirs = buildDirs(merged.paths);
  return merged;
}

export function loadSelectors(configDir) {
  const basePath = path.join(configDir, 'selectors.json');
  const localPath = path.join(configDir, 'selectors.local.json');

  const base = stripPrivate(readJson(basePath));
  if (!isPlainObject(base)) throw new ConfigError(`${basePath} must contain a JSON object.`);

  const local = stripPrivate(readJson(localPath, { required: false }) ?? {});
  return deepMerge(base, local);
}

export function prepareRuntimeDirs(settings, { profile = true } = {}) {
  const keys = ['outputDir', 'refsDir', 'stateDir', 'debugDir', 'discoverDir', 'downloadsDir'];
  if (profile) keys.push('profileDir');
  ensureDirs(settings.dirs, keys);
  return settings.dirs;
}


