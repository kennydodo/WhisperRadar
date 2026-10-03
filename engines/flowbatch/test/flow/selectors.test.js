import test from 'node:test';
import assert from 'node:assert/strict';

import { SelectorSet } from '../../src/flow/selectors.js';
import { SelectorError } from '../../src/lib/errors.js';

function makeRoot(map = {}) {
  return {
    locator(selector) {
      const spec = map[selector] ?? { count: 0 };
      const locator = {
        first: () => locator,
        count: async () => spec.count ?? 0,
        isVisible: async () => spec.visible ?? true,
        isEnabled: async () => spec.enabled ?? true,
      };
      return locator;
    },
  };
}

test('candidates filters empties and normalises single values', () => {
  const set = new SelectorSet({ a: ['x', '', '  ', 5, 'y'], b: 'z', c: [] });
  assert.deepEqual(set.candidates('a'), ['x', 'y']);
  assert.deepEqual(set.candidates('b'), ['z']);
  assert.deepEqual(set.candidates('c'), []);
  assert.deepEqual(set.candidates('missing'), []);
  assert.equal(set.has('a'), true);
  assert.equal(set.has('c'), false);
  assert.deepEqual(set.keys().sort(), ['a', 'b', 'c']);
});

test('find returns the first candidate that resolves', async () => {
  const set = new SelectorSet({ a: ['first', 'second'] });
  const root = makeRoot({ first: { count: 0 }, second: { count: 1 } });
  const found = await set.find(root, 'a', { timeout: 0 });
  assert.equal(found.selector, 'second');
  assert.ok(found.locator);
});

test('find skips invisible candidates unless visibility is waived', async () => {
  const set = new SelectorSet({ a: ['first', 'second'] });
  const root = makeRoot({ first: { count: 1, visible: false }, second: { count: 1 } });
  assert.equal((await set.find(root, 'a', { timeout: 0 })).selector, 'second');

  const onlyInvisible = makeRoot({ first: { count: 1, visible: false } });
  assert.equal((await set.find(onlyInvisible, 'a', { timeout: 0, requireVisible: false })).selector, 'first');
});

test('find skips disabled candidates when enabled is required', async () => {
  const set = new SelectorSet({ a: ['first', 'second'] });
  const root = makeRoot({ first: { count: 1, enabled: false }, second: { count: 1 } });
  assert.equal((await set.find(root, 'a', { timeout: 0, requireEnabled: true })).selector, 'second');
});

test('find raises an actionable SelectorError when nothing resolves', async () => {
  const set = new SelectorSet({ a: ['first', 'second'] });
  const root = makeRoot({});
  await assert.rejects(set.find(root, 'a', { timeout: 0 }), (error) => {
    assert.ok(error instanceof SelectorError);
    assert.equal(error.key, 'a');
    assert.match(error.message, /first/);
    return true;
  });
});

test('find returns null for an optional element and throws when no candidates exist', async () => {
  const set = new SelectorSet({ a: ['first'] });
  assert.equal(await set.find(makeRoot({}), 'a', { timeout: 0, required: false }), null);

  const empty = new SelectorSet({ a: [] });
  assert.equal(await empty.find(makeRoot({}), 'a', { required: false }), null);
  await assert.rejects(empty.find(makeRoot({}), 'a'), SelectorError);
});

test('exists and count report presence', async () => {
  const set = new SelectorSet({ a: ['first', 'second'] });
  assert.equal(await set.exists(makeRoot({ first: { count: 1 } }), 'a'), true);
  assert.equal(await set.exists(makeRoot({}), 'a'), false);

  assert.equal(await set.count(makeRoot({ first: { count: 0 }, second: { count: 3 } }), 'a'), 3);
  assert.equal(await set.count(makeRoot({}), 'a'), 0);
});
