import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

import { readJson, writeJson } from '../../src/lib/json.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

test('readJson strips a UTF-8 BOM', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const file = writeFile(dir, 'bom.json', `\uFEFF${JSON.stringify({ a: 1 })}`);
  assert.deepEqual(readJson(file), { a: 1 });
});

test('readJson returns null for a missing optional file and throws for a required one', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const missing = path.join(dir, 'nope.json');
  assert.equal(readJson(missing, { required: false }), null);
  assert.throws(() => readJson(missing), /File not found/);
});

test('readJson reports malformed JSON', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const file = writeFile(dir, 'bad.json', '{ not json');
  assert.throws(() => readJson(file), /Invalid JSON/);
});

test('writeJson round-trips, creates parents, and leaves no temp file', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const file = path.join(dir, 'nested', 'state.json');
  writeJson(file, { x: [1, 2], y: 'z' });
  assert.deepEqual(readJson(file), { x: [1, 2], y: 'z' });
  assert.equal(fs.existsSync(`${file}.tmp`), false);
  assert.match(fs.readFileSync(file, 'utf8'), /\n$/);
});
