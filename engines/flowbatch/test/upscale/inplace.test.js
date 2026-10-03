import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { pngDimensions, upscalePlanForFile } from '../../commands/upscale.js';
import { encodePng } from '../../src/upscale/png.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

test('pngDimensions reads the IHDR size and rejects anything that is not a PNG', () => {
  const dir = makeTempDir('flowbatch-inplace-');
  try {
    const png = encodePng({ width: 4, height: 3, channels: 3, data: Buffer.alloc(4 * 3 * 3) });
    const file = writeFile(dir, 'shot.png', png);
    assert.deepEqual(pngDimensions(file), { width: 4, height: 3 });

    const text = writeFile(dir, 'notes.txt', 'definitely not a png');
    assert.equal(pngDimensions(text), null);
    assert.equal(pngDimensions(path.join(dir, 'missing.png')), null);
  } finally {
    removeDir(dir);
  }
});

test('in-place skips files that already meet the tier (the pass is idempotent)', () => {
  const at2k = upscalePlanForFile('a.png', { tier: '2k', inPlace: true, dimensions: { width: 2560, height: 1440 } });
  assert.equal(at2k.skip, true);
  assert.match(at2k.reason, /already 2560x1440/);

  const above = upscalePlanForFile('a.png', { tier: '2k', inPlace: true, dimensions: { width: 3840, height: 2160 } });
  assert.equal(above.skip, true);

  const master = upscalePlanForFile('a.png', { tier: '2k', inPlace: true, dimensions: { width: 1376, height: 768 } });
  assert.equal(master.skip, false);
  assert.equal(master.mode, 'in-place');
  assert.match(master.destination, /a\.png\.upscale-tmp\.png$/);
  assert.equal(master.destination.endsWith('_2k.png'), false);
});

test('in-place with the tier off is a no-op and never writes a tier copy', () => {
  const off = upscalePlanForFile('a.png', { tier: 'off', inPlace: true, dimensions: null });
  assert.equal(off.skip, true);
  assert.match(off.reason, /off/);

  // A non-PNG (no readable dimensions) is still processed, never skipped.
  const unknown = upscalePlanForFile('a.webp', { tier: '2k', inPlace: true, dimensions: null });
  assert.equal(unknown.skip, false);
});

test('without --in-place the existing destination rules are unchanged', () => {
  const beside = upscalePlanForFile(path.join('x', 'a.png'), { tier: '2k' });
  assert.equal(beside.destination, path.join('x', 'a_2k.png'));
  assert.equal(beside.skip, false);

  const out = upscalePlanForFile(path.join('x', 'a.png'), { tier: '2k', outDir: path.join('y') });
  assert.equal(out.destination, path.join('y', 'a.png'));
  assert.equal(out.mode, 'out');
});
