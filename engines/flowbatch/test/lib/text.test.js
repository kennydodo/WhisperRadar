import test from 'node:test';
import assert from 'node:assert/strict';

import { countMojibake, looksLikeMojibake, repairMojibake } from '../../src/lib/text.js';

test('looksLikeMojibake detects CP1252-read-as-UTF-8', () => {
  assert.equal(looksLikeMojibake('\u00e2\u20ac\u201d'), true);
  assert.equal(looksLikeMojibake('\u00c3\u00a9'), true);
  assert.equal(looksLikeMojibake('an em dash \u2014 is fine'), false);
  assert.equal(looksLikeMojibake(''), false);
  assert.equal(looksLikeMojibake(null), false);
});

test('repairMojibake reverses the damage', () => {
  assert.equal(repairMojibake('\u00e2\u20ac\u201d'), '\u2014');
  assert.equal(repairMojibake('\u00c3\u00a9'), '\u00e9');
  assert.equal(repairMojibake('caf\u00c3\u00a9 ok'), 'caf\u00e9 ok');
});

test('repairMojibake leaves clean text unchanged', () => {
  assert.equal(repairMojibake('\u2014'), '\u2014');
  assert.equal(repairMojibake('plain ascii'), 'plain ascii');
});

test('repairMojibake returns the input when a character cannot be mapped', () => {
  assert.equal(repairMojibake('\u4e2d\u6587'), '\u4e2d\u6587');
});

test('countMojibake counts only corrupted string entries', () => {
  assert.equal(countMojibake(['clean', '\u00e2\u20ac\u201d', '\u00c3\u00a9', 7]), 2);
});
