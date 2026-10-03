import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { RunState, STATUS } from '../../src/runner/state.js';
import { readJson } from '../../src/lib/json.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

function items(...ids) {
  return ids.map((id) => ({ id }));
}

function meta(overrides = {}) {
  return { jobName: 'job', jobPath: 'job.json', items: items('a', 'b'), projectUrl: null, ...overrides };
}

test('a new state starts every item pending', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const state = new RunState(path.join(dir, 'state.json'), meta());

  assert.equal(state.get('a').status, STATUS.pending);
  assert.equal(state.get('a').attempts, 0);
  assert.deepEqual(state.get('a').files, []);
  assert.equal(state.get('a').error, null);
  assert.equal(state.isDone('a'), false);
});

test('open restores matching progress and keeps new items pending', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const file = path.join(dir, 'state.json');

  const first = new RunState(file, meta());
  first.update('a', { status: STATUS.done, files: ['a.png'] });
  first.save();

  const reopened = RunState.open(file, meta());
  assert.equal(reopened.isDone('a'), true);
  assert.deepEqual(reopened.get('a').files, ['a.png']);
  assert.equal(reopened.get('b').status, STATUS.pending);
});

test('open ignores state recorded for a different job', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const file = path.join(dir, 'state.json');

  const first = new RunState(file, meta());
  first.update('a', { status: STATUS.done });
  first.save();

  const reopened = RunState.open(file, meta({ jobName: 'other-job' }));
  assert.equal(reopened.isDone('a'), false);
});

test('open reports a project change and refuses to reuse the progress', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const file = path.join(dir, 'state.json');

  const first = new RunState(file, meta({ projectUrl: 'p1' }));
  first.update('a', { status: STATUS.done });
  first.save();

  const reopened = RunState.open(file, meta({ projectUrl: 'p2' }));
  assert.deepEqual(reopened.projectChanged, { from: 'p1', to: 'p2' });
  assert.equal(reopened.isDone('a'), false);
});

test('clearStaleRunning reopens items left mid-run', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const state = new RunState(path.join(dir, 'state.json'), meta());
  state.update('a', { status: STATUS.running, error: 'boom' });

  const stale = state.clearStaleRunning();
  assert.deepEqual(stale, ['a']);
  assert.equal(state.get('a').status, STATUS.pending);
  assert.equal(state.get('a').error, null);
});

test('clearMissingFiles reopens a done item whose output is gone', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const present = writeFile(dir, 'b.png', 'x');
  const state = new RunState(path.join(dir, 'state.json'), meta({ items: items('a', 'b', 'c') }));

  state.update('a', { status: STATUS.done, files: [path.join(dir, 'gone.png')] });
  state.update('b', { status: STATUS.done, files: [present] });
  state.update('c', { status: STATUS.done, files: [] });

  const reopened = state.clearMissingFiles();
  assert.deepEqual(reopened.sort(), ['a', 'c']);
  assert.equal(state.get('a').status, STATUS.pending);
  assert.equal(state.get('a').attempts, 0);
  assert.deepEqual(state.get('a').files, []);
  assert.equal(state.get('b').status, STATUS.done);
  assert.equal(state.get('c').status, STATUS.pending);
});

test('counts, reset, and update behave', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const state = new RunState(path.join(dir, 'state.json'), meta({ items: items('a', 'b', 'c') }));

  state.update('a', { status: STATUS.done });
  state.update('b', { status: STATUS.failed });
  assert.deepEqual(state.counts(), { pending: 1, running: 0, done: 1, failed: 1, skipped: 0 });

  state.update('c', { status: STATUS.running, attempts: 3, files: ['c.png'], error: 'x' });
  state.reset(['c']);
  assert.equal(state.get('c').status, STATUS.pending);
  assert.equal(state.get('c').attempts, 0);
  assert.deepEqual(state.get('c').files, []);
  assert.equal(state.get('c').error, null);

  state.update('missing', { status: STATUS.done });
  assert.equal(state.get('missing'), undefined);
});

test('save writes the state file and pathFor/exists locate it', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const file = path.join(dir, 'job.json');
  assert.equal(RunState.pathFor(dir, 'job'), file);
  assert.equal(RunState.exists(dir, 'job'), false);

  const state = new RunState(file, meta());
  state.update('a', { status: STATUS.done, files: ['a.png'] });
  state.save();

  assert.equal(RunState.exists(dir, 'job'), true);
  assert.equal(readJson(file).items.a.status, STATUS.done);
});
