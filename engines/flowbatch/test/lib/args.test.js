import test from 'node:test';
import assert from 'node:assert/strict';

import { flagValue, intFlag, listFlag, parseArgs, repeatFlag } from '../../src/lib/args.js';

test('parseArgs reads --key=value', () => {
  assert.deepEqual(parseArgs(['--limit=3']).flags, { limit: '3' });
});

test('parseArgs reads --key value for declared value flags', () => {
  const { flags, positionals } = parseArgs(['--job', 'a.json', 'extra'], { valueFlags: ['job'] });
  assert.deepEqual(flags, { job: 'a.json' });
  assert.deepEqual(positionals, ['extra']);
});

test('parseArgs treats an undeclared flag as boolean and keeps its value positional', () => {
  const { flags, positionals } = parseArgs(['--flag', 'value']);
  assert.equal(flags.flag, true);
  assert.deepEqual(positionals, ['value']);
});

test('parseArgs supports --no-key', () => {
  assert.equal(parseArgs(['--no-color']).flags.color, false);
});

test('parseArgs stops flag parsing at --', () => {
  const { flags, positionals } = parseArgs(['--', '--not-a-flag', 'z']);
  assert.deepEqual(flags, {});
  assert.deepEqual(positionals, ['--not-a-flag', 'z']);
});

test('parseArgs collects a repeated value flag into an array', () => {
  const { flags } = parseArgs(['--only', 'a', '--only', 'b'], { valueFlags: ['only'] });
  assert.deepEqual(flags.only, ['a', 'b']);
});

test('parseArgs throws when a value flag lacks a value', () => {
  assert.throws(() => parseArgs(['--job'], { valueFlags: ['job'] }), /requires a value/);
  assert.throws(() => parseArgs(['--job', '--other'], { valueFlags: ['job'] }), /requires a value/);
});

test('flagValue returns the fallback only when absent', () => {
  assert.equal(flagValue({}, 'x', 'd'), 'd');
  assert.equal(flagValue({ x: true }, 'x', 'd'), true);
  assert.equal(flagValue({ x: false }, 'x', 'd'), false);
  assert.equal(flagValue({ x: 'v' }, 'x', 'd'), 'v');
});

test('intFlag parses, defaults, and rejects non-integers', () => {
  assert.equal(intFlag({ n: '5' }, 'n', 0), 5);
  assert.equal(intFlag({}, 'n', 9), 9);
  assert.throws(() => intFlag({ n: 'abc' }, 'n'), /must be an integer/);
});

test('listFlag splits, trims, and tolerates arrays', () => {
  assert.deepEqual(listFlag({ only: 'a, b ,c' }, 'only'), ['a', 'b', 'c']);
  assert.deepEqual(listFlag({ only: ['a', 'b'] }, 'only'), ['a', 'b']);
  assert.equal(listFlag({ only: true }, 'only'), null);
  assert.equal(listFlag({}, 'only'), null);
});

test('repeatFlag always returns an array', () => {
  assert.deepEqual(repeatFlag({ x: ['a', 'b'] }, 'x'), ['a', 'b']);
  assert.deepEqual(repeatFlag({ x: 'a' }, 'x'), ['a']);
  assert.deepEqual(repeatFlag({ x: true }, 'x'), []);
  assert.deepEqual(repeatFlag({}, 'x'), []);
});
