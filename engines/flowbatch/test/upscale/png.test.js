import test from 'node:test';
import assert from 'node:assert/strict';

import {
  decodePng,
  encodePng,
  meanAbsoluteDeviation,
  pngDimensions,
  resizeLanczos,
  toRgb,
} from '../../src/upscale/png.js';

function solid(width, height, [r, g, b]) {
  const data = Buffer.alloc(width * height * 3);
  for (let i = 0; i < width * height; i += 1) {
    data[i * 3] = r;
    data[i * 3 + 1] = g;
    data[i * 3 + 2] = b;
  }
  return { width, height, channels: 3, data };
}

test('RGB PNGs survive an encode/decode round trip', () => {
  const data = Buffer.from(Array.from({ length: 3 * 2 * 3 }, (_, i) => (i * 10) % 256));
  const decoded = decodePng(encodePng({ width: 3, height: 2, channels: 3, data }));
  assert.equal(decoded.width, 3);
  assert.equal(decoded.height, 2);
  assert.equal(decoded.channels, 3);
  assert.deepEqual(decoded.data, data);
});

test('RGBA PNGs round trip with the alpha channel intact', () => {
  const data = Buffer.from(Array.from({ length: 3 * 2 * 4 }, (_, i) => (i * 7) % 256));
  const decoded = decodePng(encodePng({ width: 3, height: 2, channels: 4, data }));
  assert.equal(decoded.channels, 4);
  assert.deepEqual(decoded.data, data);
});

test('toRgb drops alpha, expands grey, and is identity for RGB', () => {
  const rgba = { width: 2, height: 1, channels: 4, data: Buffer.from([1, 2, 3, 255, 4, 5, 6, 128]) };
  const rgb = toRgb(rgba);
  assert.equal(rgb.channels, 3);
  assert.deepEqual(rgb.data, Buffer.from([1, 2, 3, 4, 5, 6]));

  const grey = toRgb({ width: 2, height: 1, channels: 1, data: Buffer.from([5, 200]) });
  assert.deepEqual(grey.data, Buffer.from([5, 5, 5, 200, 200, 200]));

  const greyAlpha = toRgb({ width: 1, height: 1, channels: 2, data: Buffer.from([77, 1]) });
  assert.deepEqual(greyAlpha.data, Buffer.from([77, 77, 77]));

  const plain = solid(1, 1, [9, 8, 7]);
  assert.equal(toRgb(plain), plain);
});

test('pngDimensions reads the IHDR and rejects malformed input', () => {
  const png = encodePng(solid(7, 5, [0, 0, 0]));
  assert.deepEqual(pngDimensions(png), { width: 7, height: 5 });
  assert.equal(pngDimensions(Buffer.from('nope')), null);
  assert.equal(pngDimensions(Buffer.alloc(30)), null);

  const wrong = Buffer.from(png);
  wrong[12] = 0x00;
  assert.equal(pngDimensions(wrong), null);
});

test('decodePng rejects a file that is not a PNG', () => {
  assert.throws(() => decodePng(Buffer.from('not a png')), /Not a PNG/);
});

test('resizeLanczos upscales a solid colour unchanged', () => {
  const out = resizeLanczos(solid(3, 3, [255, 0, 0]), 6, 6);
  assert.equal(out.width, 6);
  assert.equal(out.height, 6);
  assert.equal(out.channels, 3);
  for (let i = 0; i < out.data.length; i += 3) {
    assert.equal(out.data[i], 255);
    assert.equal(out.data[i + 1], 0);
    assert.equal(out.data[i + 2], 0);
  }
});

test('resizeLanczos downscales and always returns RGB', () => {
  const rgba = { width: 4, height: 4, channels: 4, data: Buffer.alloc(4 * 4 * 4, 255) };
  const out = resizeLanczos(rgba, 2, 2);
  assert.equal(out.channels, 3);
  assert.equal(out.data.length, 2 * 2 * 3);
  assert.deepEqual(out.data, Buffer.alloc(2 * 2 * 3, 255));
});

test('meanAbsoluteDeviation separates a match from garbage', () => {
  const black = solid(8, 8, [0, 0, 0]);
  const white = solid(8, 8, [255, 255, 255]);

  const same = meanAbsoluteDeviation(black, black);
  assert.equal(same.mad, 0);
  assert.equal(same.ok, true);

  const different = meanAbsoluteDeviation(black, white);
  assert.ok(different.mad > 200);
  assert.equal(different.ok, false);
  assert.equal(meanAbsoluteDeviation(black, white, 300).ok, true);
});
