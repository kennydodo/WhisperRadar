import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { mimeForExtension, sniffImageExtension } from '../../src/lib/image.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00]);
const JPG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);
const WEBP = Buffer.concat([Buffer.from('RIFF'), Buffer.from([0, 0, 0, 0]), Buffer.from('WEBP')]);
const GIF = Buffer.from('GIF89a');

test('sniffImageExtension detects the real format from magic bytes', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const cases = [
    ['a.png', PNG, '.png'],
    ['b.jpg', JPG, '.jpg'],
    ['c.webp', WEBP, '.webp'],
    ['d.gif', GIF, '.gif'],
    ['e.bin', Buffer.from('just text here'), '.bin'],
  ];
  for (const [name, bytes, expected] of cases) {
    assert.equal(sniffImageExtension(writeFile(dir, name, bytes)), expected);
  }
});

test('sniffImageExtension falls back when the file cannot be read', () => {
  assert.equal(sniffImageExtension(path.join('does', 'not', 'exist')), '.bin');
});

test('mimeForExtension maps known extensions case-insensitively', () => {
  assert.equal(mimeForExtension('.PNG'), 'image/png');
  assert.equal(mimeForExtension('.jpeg'), 'image/jpeg');
  assert.equal(mimeForExtension('.webp'), 'image/webp');
  assert.equal(mimeForExtension('.xyz'), 'application/octet-stream');
});
