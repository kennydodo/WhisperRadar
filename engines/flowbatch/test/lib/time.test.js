import test from 'node:test';
import assert from 'node:assert/strict';

import { formatDuration, nowIso, sleep, timestampSlug } from '../../src/lib/time.js';

test('formatDuration renders minutes and seconds', () => {
  assert.equal(formatDuration(0), '0s');
  assert.equal(formatDuration(1000), '1s');
  assert.equal(formatDuration(65000), '1m 05s');
  assert.equal(formatDuration(3600000), '60m 00s');
  assert.equal(formatDuration(-5), '0s');
});

test('timestampSlug is filesystem-safe', () => {
  const slug = timestampSlug(new Date('2026-09-24T20:11:39.123Z'));
  assert.equal(slug, '2026-09-24T20-11-39');
  assert.equal(slug.length, 19);
});

test('nowIso returns a parseable ISO timestamp', () => {
  const value = nowIso();
  assert.equal(typeof value, 'string');
  assert.match(value, /Z$/);
  assert.equal(Number.isNaN(Date.parse(value)), false);
});

test('sleep resolves after the delay', async () => {
  const started = Date.now();
  await sleep(5);
  assert.ok(Date.now() - started >= 0);
});
