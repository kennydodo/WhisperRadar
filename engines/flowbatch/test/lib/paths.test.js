import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';

import { ROOT, buildDirs, ensureParent, fromRoot, slugify } from '../../src/lib/paths.js';
import { makeTempDir, removeDir } from '../../test-support/tmp.js';

test('fromRoot resolves relative paths against the project root', () => {
  assert.equal(fromRoot('output/x'), path.resolve(ROOT, 'output/x'));
});

test('fromRoot normalizes absolute paths and passes falsy through', () => {
  const absolute = path.resolve(ROOT, 'some', 'file.json');
  assert.equal(fromRoot(absolute), path.normalize(absolute));
  assert.equal(fromRoot(null), null);
  assert.equal(fromRoot(''), null);
});

test('buildDirs applies defaults and derives root/config', () => {
  const dirs = buildDirs({ outputDir: 'out' });
  assert.equal(dirs.outputDir, path.resolve(ROOT, 'out'));
  assert.equal(dirs.profileDir, path.resolve(ROOT, 'profile'));
  assert.equal(dirs.root, ROOT);
  assert.equal(dirs.config, path.join(ROOT, 'config'));
});

test('slugify produces filesystem-safe ids', () => {
  assert.equal(slugify('Hello World'), 'Hello-World');
  assert.equal(slugify('a\u{1F600}b'), 'a-b');
  assert.equal(slugify(''), 'item');
  assert.equal(slugify('   ', 'fallback'), 'fallback');
  assert.equal(slugify('x'.repeat(200)).length, 80);
});

test('ensureParent creates the containing directory', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const target = path.join(dir, 'nested', 'deep', 'file.json');
  ensureParent(target);
  assert.equal(fs.existsSync(path.dirname(target)), true);
});
