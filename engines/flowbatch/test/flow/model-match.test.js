import { test } from 'node:test';
import assert from 'node:assert/strict';
import { modelMatches } from '../../src/flow/driver.js';

test('"Nano Banana 2" matches itself and point releases', () => {
  assert.ok(modelMatches('Nano Banana 2', 'Nano Banana 2'));
  assert.ok(modelMatches('🍌 Nano Banana 2', 'Nano Banana 2'));
  assert.ok(modelMatches('Nano Banana 2.1', 'Nano Banana 2'));
  assert.ok(modelMatches('Nano Banana 2.1.3', 'Nano Banana 2'));
});

test('"Nano Banana 2" never matches Lite, Pro or another generation', () => {
  assert.ok(!modelMatches('Nano Banana 2 Lite', 'Nano Banana 2'));
  assert.ok(!modelMatches('Nano Banana Pro', 'Nano Banana 2'));
  assert.ok(!modelMatches('Nano Banana 2.1 Pro', 'Nano Banana 2'));
  assert.ok(!modelMatches('Nano Banana 3', 'Nano Banana 2'));
  assert.ok(!modelMatches('Nano Banana', 'Nano Banana 2'));
});
