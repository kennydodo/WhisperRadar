import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { deepMerge, isPlainObject, loadSelectors, loadSettings } from '../../src/lib/config.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

test('isPlainObject distinguishes objects from arrays and scalars', () => {
  assert.equal(isPlainObject({}), true);
  assert.equal(isPlainObject([]), false);
  assert.equal(isPlainObject(null), false);
  assert.equal(isPlainObject('x'), false);
});

test('deepMerge merges nested objects and replaces arrays and scalars', () => {
  assert.deepEqual(deepMerge({ a: { b: 1, c: 2 } }, { a: { b: 9 } }), { a: { b: 9, c: 2 } });
  assert.deepEqual(deepMerge({ a: [1, 2] }, { a: [3] }), { a: [3] });
  assert.deepEqual(deepMerge({ a: 1 }, { a: 2 }), { a: 2 });
  assert.deepEqual(deepMerge({ a: 1 }, undefined), { a: 1 });
  assert.equal(deepMerge(1, 2), 2);
});

test('loadSettings merges settings.local.json over settings.json', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  writeFile(dir, 'settings.json', JSON.stringify({ browser: { headless: true }, paths: { outputDir: 'o' } }));
  writeFile(dir, 'settings.local.json', JSON.stringify({ browser: { headless: false } }));
  const settings = loadSettings(dir);
  assert.equal(settings.browser.headless, false);
  assert.equal(path.basename(settings.dirs.outputDir), 'o');
});

test('loadSettings throws when the base settings file is missing', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  assert.throws(() => loadSettings(dir), /File not found/);
});

test('loadSelectors strips private keys and lets local entries win', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  writeFile(dir, 'selectors.json', JSON.stringify({ _note: 'x', promptBox: ['a'], other: ['b'] }));
  writeFile(dir, 'selectors.local.json', JSON.stringify({ promptBox: ['c'] }));
  const selectors = loadSelectors(dir);
  assert.equal('_note' in selectors, false);
  assert.deepEqual(selectors.promptBox, ['c']);
  assert.deepEqual(selectors.other, ['b']);
});
