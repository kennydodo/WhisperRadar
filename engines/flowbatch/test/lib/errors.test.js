import test from 'node:test';
import assert from 'node:assert/strict';

import { ConfigError, GenerationError, SelectorError, TimeoutError } from '../../src/lib/errors.js';

test('SelectorError names the key, candidates, and the calibration path', () => {
  const error = new SelectorError('promptBox', ['a', 'b'], ['boom']);
  assert.equal(error.name, 'SelectorError');
  assert.equal(error.key, 'promptBox');
  assert.deepEqual(error.candidates, ['a', 'b']);
  assert.match(error.message, /promptBox/);
  assert.match(error.message, /- a/);
  assert.match(error.message, /npm run discover/);
  assert.ok(error instanceof Error);
});

test('GenerationError defaults to retryable', () => {
  assert.equal(new GenerationError('x').retryable, true);
  assert.equal(new GenerationError('x', { retryable: false }).retryable, false);
  assert.equal(new GenerationError('x').name, 'GenerationError');
});

test('TimeoutError and ConfigError carry their names', () => {
  assert.equal(new TimeoutError('t').name, 'TimeoutError');
  assert.equal(new ConfigError('c').name, 'ConfigError');
  assert.ok(new ConfigError('c') instanceof Error);
});
