import fs from 'node:fs';
import path from 'node:path';

import { fromRoot } from '../src/lib/paths.js';
import { log } from '../src/lib/log.js';
import { repairMojibake } from '../src/lib/text.js';

/** Match the file's existing indentation instead of imposing one. */
function detectIndent(text) {
  const match = text.match(/^([ \t]+)"/m);
  return match ? match[1] : '  ';
}

/** Repair every string in the document, recording what changed. */
function walk(value, pathParts, changes) {
  if (typeof value === 'string') {
    const fixed = repairMojibake(value);
    if (fixed !== value) {
      changes.push({ path: pathParts.join('.') || '(root)', before: value, after: fixed });
      return fixed;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((entry, index) => walk(entry, [...pathParts, String(index)], changes));
  }
  if (value && typeof value === 'object') {
    const result = {};
    for (const [key, entry] of Object.entries(value)) {
      result[key] = walk(entry, [...pathParts, key], changes);
    }
    return result;
  }
  return value;
}

function contextAround(text, needle) {
  const index = text.indexOf(needle);
  if (index < 0) return text.slice(0, 60);
  return text.slice(Math.max(0, index - 30), index + 30);
}

export async function repairCommand({ flags, positionals }) {
  const target = typeof flags.job === 'string' ? flags.job : positionals[0];
  if (!target) {
    throw new Error('Usage: node src/cli.js repair <job.json> [--dry-run] [--no-backup]');
  }

  const file = fromRoot(target);
  if (!file || !fs.existsSync(file)) throw new Error(`File not found: ${target}`);

  const buffer = fs.readFileSync(file);
  const hadBom = buffer.length >= 3 && buffer[0] === 0xef && buffer[1] === 0xbb && buffer[2] === 0xbf;
  const text = buffer.toString('utf8').replace(/^\uFEFF/, '');
  const indent = detectIndent(text);

  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new Error(`Invalid JSON in ${file}: ${error.message}`);
  }

  const changes = [];
  const repaired = walk(parsed, [], changes);

  log.heading(`Repair ${path.basename(file)}`);
  log.raw(`  file            : ${file}`);
  log.raw(`  UTF-8 BOM       : ${hadBom ? 'present — will be removed' : 'none'}`);
  log.raw(`  strings changed : ${changes.length}`);

  if (changes.length === 0 && !hadBom) {
    log.ok('Nothing to repair.');
    return 0;
  }

  for (const change of changes.slice(0, 5)) {
    log.raw(`  - ${change.path}`);
    log.raw(`      before: ${JSON.stringify(contextAround(change.before, change.before[change.before.search(/[^\x00-\x7F]/)]))}`);
    log.raw(`      after : ${JSON.stringify(contextAround(change.after, change.after[change.after.search(/[^\x00-\x7F]/)]))}`);
  }
  if (changes.length > 5) log.raw(`  … and ${changes.length - 5} more`);

  if (flags['dry-run'] === true) {
    log.info('Dry run: nothing was written.');
    return 0;
  }

  if (flags['no-backup'] !== true) {
    const backup = `${file}.bak`;
    fs.copyFileSync(file, backup);
    log.info(`Backup written: ${backup}`);
  }

  fs.writeFileSync(file, `${JSON.stringify(repaired, null, indent)}\n`, 'utf8');
  log.ok(`Wrote ${path.basename(file)} with ${changes.length} repaired string(s).`);
  return 0;
}
