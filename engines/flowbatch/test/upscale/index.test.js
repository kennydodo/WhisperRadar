import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

import {
  TIERS,
  TIER_NAMES,
  describeUpscaler,
  engineLabel,
  loadUpscaleSettings,
  normalizeTier,
  targetSizeFor,
  upscaleImage,
} from '../../src/upscale/index.js';
import { encodePng } from '../../src/upscale/png.js';
import { makeTempDir, removeDir } from '../../test-support/tmp.js';

test('normalizeTier accepts tiers, legacy multipliers, and off', () => {
  assert.equal(normalizeTier('off'), 'off');
  assert.equal(normalizeTier('none'), 'off');
  assert.equal(normalizeTier('0'), 'off');
  assert.equal(normalizeTier('false'), 'off');
  assert.equal(normalizeTier('1k'), '1k');
  assert.equal(normalizeTier('4K'), '4k');
  assert.equal(normalizeTier(1), '1k');
  assert.equal(normalizeTier(2), '2k');
  assert.equal(normalizeTier(4), '4k');
  assert.equal(normalizeTier(undefined), '2k');
  assert.deepEqual(TIER_NAMES, ['1k', '2k', '4k']);
  assert.throws(() => normalizeTier('9k'), /Upscale tier must be/);
  assert.throws(() => normalizeTier('3'), /Upscale tier must be/);
});

test('targetSizeFor snaps known ratios to the tier size', () => {
  assert.deepEqual(targetSizeFor(1376, 768, '1k'), { width: 1920, height: 1080 });
  assert.deepEqual(targetSizeFor(1376, 768, '2k'), { width: 2560, height: 1440 });
  assert.deepEqual(targetSizeFor(1376, 768, '4k'), { width: 3840, height: 2160 });
  assert.deepEqual(targetSizeFor(768, 1376, '1k'), { width: 1080, height: 1920 });
  assert.deepEqual(targetSizeFor(1024, 768, '2k'), { width: 2560, height: 1920 });
  assert.deepEqual(targetSizeFor(1024, 1024, '2k'), { width: 2560, height: 2560 });
  assert.deepEqual(targetSizeFor(1200, 1000, '2k'), { width: 2560, height: 2133 });
  assert.throws(() => targetSizeFor(100, 100, '9k'), /Unknown tier/);
});

test('targetSizeFor aspect keeps the master ratio and the long side', () => {
  assert.deepEqual(targetSizeFor(768, 1376, '1k', 'aspect'), { width: 1072, height: 1920 });
  assert.deepEqual(targetSizeFor(1376, 768, '1k', 'aspect'), { width: 1920, height: 1072 });
});

test('engineLabel describes the method, device and tier', () => {
  assert.equal(engineLabel(null), 'unknown');
  assert.equal(engineLabel({ method: 'none', tier: '2k' }), 'original (upscale off)');
  assert.equal(engineLabel({ method: 'lanczos', tier: '1k' }), 'Lanczos CPU (1920x1080 (Full HD))');
  assert.equal(engineLabel({ method: 'realesrgan', device: 'RTX', tier: '4k' }), 'Real-ESRGAN (RTX, 3840x2160 (4K))');
});

test('loadUpscaleSettings strips comment keys and yields a valid tier', () => {
  const settings = loadUpscaleSettings();
  assert.equal('_readme' in settings, false);
  assert.ok('tier' in settings && 'model' in settings && 'supersample' in settings && 'fit' in settings);
  assert.doesNotThrow(() => normalizeTier(settings.tier));
});

test('describeUpscaler exposes the three tiers and a matching target', () => {
  const info = describeUpscaler();
  assert.deepEqual(info.tiers.map((tier) => tier.id), ['1k', '2k', '4k']);
  assert.equal(typeof info.engineAvailable, 'boolean');
  for (const tier of info.tiers) {
    assert.match(tier.sixteenNine, /^\d+x\d+$/);
    assert.match(tier.aspect, /long side/);
  }
  const spec = TIERS[info.tier];
  assert.equal(info.target16x9, spec ? spec.exact16x9.join('\u00d7') : null);
});

test('upscaleImage with tier off copies the master byte for byte', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const source = path.join(dir, 'in.png');
  const destination = path.join(dir, 'out.png');
  fs.writeFileSync(source, encodePng({ width: 3, height: 2, channels: 3, data: Buffer.alloc(18, 128) }));

  const result = upscaleImage(source, destination, { tier: 'off' });
  assert.equal(result.method, 'none');
  assert.equal(result.width, 3);
  assert.equal(result.height, 2);
  assert.deepEqual(fs.readFileSync(destination), fs.readFileSync(source));
});

test('upscaleImage rejects a missing source', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  assert.throws(
    () => upscaleImage(path.join(dir, 'nope.png'), path.join(dir, 'o.png'), { tier: 'off' }),
    /Image not found/,
  );
});
