import test from 'node:test';
import assert from 'node:assert/strict';

import { newTitles, normalizeTitle, titleSearchTerm, titlesMatch, uniqueNewTitle } from '../../src/flow/asset-titles.js';

test('normalizeTitle drops whitespace noise and a trailing ellipsis', () => {
  assert.equal(normalizeTitle('  Four stick figures   standing toge… '), 'Four stick figures standing toge');
  assert.equal(normalizeTitle('Mammoth standing on plain backgr...'), 'Mammoth standing on plain backgr');
  assert.equal(titleSearchTerm('Two apes sitting hunched'), 'Two apes sitting hunched');
});

test('a truncated library title matches the full caption, and only that one', () => {
  assert.equal(titlesMatch('Four stick figures standing toge…', 'Four stick figures standing together'), true);
  assert.equal(titlesMatch('Four stick figures standing together', 'Four stick figures standing toge…'), true);
  assert.equal(titlesMatch('Two apes sitting hunched', 'Two apes sitting hunched'), true);
  assert.equal(titlesMatch('two apes sitting hunched', 'Two apes sitting hunched'), true);
  // no ellipsis on either side: a longer title is a different asset
  assert.equal(titlesMatch('Clay mug on plain background', 'Clay mug'), false);
  // a short truncated prefix is not evidence
  assert.equal(titlesMatch('Character…', 'Character waving goodbye in scene'), false);
  assert.equal(titlesMatch('Mammoth standing on plain backgr…', 'Four stick figures standing together'), false);
  assert.equal(titlesMatch('', 'x'), false);
});

test('newTitles is a multiset difference', () => {
  assert.deepEqual(newTitles(['a', 'b'], ['a', 'b', 'c']), ['c']);
  assert.deepEqual(newTitles(['a', 'a'], ['a', 'a', 'a']), ['a']);
  assert.deepEqual(newTitles(['a'], ['a']), []);
});

test('uniqueNewTitle names the one new image, never a guess', () => {
  const before = ['OBJ_CLAY_MUG.png', 'Early human holding stone spear'];
  assert.equal(uniqueNewTitle(before, [...before, 'Two apes sitting hunched']), 'Two apes sitting hunched');
  assert.equal(uniqueNewTitle(before, before), null);
  assert.equal(uniqueNewTitle(before, [...before, 'a one', 'a two']), null);
  // the new caption is shared with another asset: a lookup could attach the wrong one
  const dup = ['Character waving goodbye in scene'];
  assert.equal(uniqueNewTitle(dup, [...dup, 'Character waving goodbye in scene']), null);
});
