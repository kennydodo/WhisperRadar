import fs from 'node:fs';
import { ensureParent } from './paths.js';

export function readJson(filePath, { required = true } = {}) {
  if (!fs.existsSync(filePath)) {
    if (required) throw new Error(`File not found: ${filePath}`);
    return null;
  }
  // Editors on Windows commonly save JSON with a UTF-8 BOM, which JSON.parse
  // rejects outright. Strip it before parsing.
  const raw = fs.readFileSync(filePath, 'utf8').replace(/^\uFEFF/, '');
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`Invalid JSON in ${filePath}: ${error.message}`);
  }
}

/**
 * Write JSON atomically so an interrupted run cannot leave a truncated state file.
 */
export function writeJson(filePath, value) {
  ensureParent(filePath);
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  fs.renameSync(tmp, filePath);
}
