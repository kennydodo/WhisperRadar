import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

import { ROOT } from '../../src/lib/paths.js';

const SELECTORS_FILE = path.join(ROOT, 'config', 'selectors.json');
const selectors = JSON.parse(fs.readFileSync(SELECTORS_FILE, 'utf8').replace(/^\uFEFF/, ''));

function sourceFiles(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(full, out);
    else if (full.endsWith('.js')) out.push(full);
  }
  return out;
}

test('every selector key maps to non-empty string candidates', () => {
  for (const [key, value] of Object.entries(selectors)) {
    if (key.startsWith('_')) continue;
    const candidates = Array.isArray(value) ? value : [value];
    assert.ok(candidates.length > 0, `${key} has no candidates`);
    for (const candidate of candidates) {
      assert.equal(typeof candidate, 'string', `${key} has a non-string candidate`);
      assert.ok(candidate.trim().length > 0, `${key} has a blank candidate`);
    }
  }
});

test('every selector key referenced in the source exists in selectors.json', () => {
  const referenced = new Set();
  const pattern = /(?:find|exists|candidates|count)\([^)]*?'([A-Za-z][A-Za-z0-9]*)'/g;
  const files = [...sourceFiles(path.join(ROOT, 'src')), ...sourceFiles(path.join(ROOT, 'commands'))];

  for (const file of files) {
    const text = fs.readFileSync(file, 'utf8');
    for (const match of text.matchAll(pattern)) referenced.add(match[1]);
  }
  // `transient` is the synthetic key findByText builds, not a configured element.
  referenced.delete('transient');

  const missing = [...referenced].filter((key) => !(key in selectors)).sort();
  assert.deepEqual(missing, [], `selectors.json is missing: ${missing.join(', ')}`);
});
