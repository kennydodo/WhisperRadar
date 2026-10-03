// Coverage for the consecutive-failure stop added in 7b0d469 ("Stop the
// batch after N consecutive failures"). That commit shipped with no test
// file - these pin the behaviour it describes: stop the whole batch once
// `maxConsecutiveFailures` items in a row end up failed, reset the counter
// on any success, treat 0 as "disabled", and never trip on `--fail-fast`'s
// own separate single-failure stop.
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { runJob } from '../../src/runner/run.js';
import { RunState } from '../../src/runner/state.js';
import { GenerationError } from '../../src/lib/errors.js';
import { setLevel } from '../../src/lib/log.js';
import { makeDriver } from '../../test-support/fake-driver.js';
import { makeTempDir, removeDir } from '../../test-support/tmp.js';

setLevel('silent');

const JPG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);

function item(id, extra = {}) {
  return {
    id,
    index: 0,
    outputName: id,
    outputFile: `${id}.png`,
    prompt: 'a prompt',
    refs: [],
    refNames: [],
    refPaths: [],
    mode: 'image',
    model: null,
    aspectRatio: null,
    outputs: 1,
    timeoutMs: null,
    retries: null,
    refMode: null,
    ...extra,
  };
}

function makeSettings({ generation = {} } = {}) {
  return {
    timeouts: { generationMs: 500 },
    generation: {
      retryDelayMs: 0,
      delayBetweenItemsMs: 0,
      cooldownSeconds: 0,
      maxCooldowns: 0,
      resetBetweenItems: 'clear',
      ...generation,
    },
    dirs: {},
  };
}

async function runFixture(t, { items, driver, settings = makeSettings(), options = {} }) {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const outputsDir = path.join(dir, 'out');
  const job = { name: 'test-job', items, outputsDir, projectUrl: null, refMode: null };
  const state = RunState.open(path.join(dir, 'state.json'), {
    jobName: job.name,
    jobPath: path.join(dir, 'job.json'),
    items,
    projectUrl: null,
  });

  const result = await runJob({
    job,
    driver,
    state,
    settings,
    options: {
      only: null,
      limit: 0,
      resume: true,
      dryRun: false,
      failFast: false,
      pauseOnError: false,
      dumpOnError: false,
      cooldownSeconds: undefined,
      maxCooldowns: undefined,
      ...options,
    },
  });
  return { result, state };
}

// Every item fails the SAME way: a retryable GenerationError with retries:0
// (the item default), so each one fails after exactly one attempt and the
// batch moves on to the next item rather than aborting outright (that only
// happens for a NON-retryable error, covered by the existing "a non-retryable
// refusal aborts the batch" test in run.test.js).
function failingDriver() {
  return makeDriver({
    waitForNewAssets: async () => {
      throw new GenerationError('transient grid timeout');
    },
  });
}

test('stops the batch once maxConsecutiveFailures items in a row have failed', async (t) => {
  const driver = failingDriver();
  const settings = makeSettings({ generation: { maxConsecutiveFailures: 2 } });
  const { result, state } = await runFixture(t, {
    items: [item('A'), item('B'), item('C'), item('D')],
    driver,
    settings,
  });

  assert.equal(result.failed, 2);
  assert.equal(state.get('A').status, 'failed');
  assert.equal(state.get('B').status, 'failed');
  // C and D were never attempted - the batch stopped after B made it 2 in a row.
  assert.equal(state.get('C').status, 'pending');
  assert.equal(state.get('D').status, 'pending');
});

test('a success in between resets the consecutive-failure count', async (t) => {
  let calls = 0;
  const driver = makeDriver({
    waitForNewAssets: async () => {
      calls += 1;
      // B (the 2nd item) succeeds; everything else fails.
      if (calls === 2) return { added: [{ key: 'k', index: 0 }], selector: 'grid' };
      throw new GenerationError('transient grid timeout');
    },
    fetchAssetBytes: async () => JPG,
  });
  const settings = makeSettings({ generation: { maxConsecutiveFailures: 2 } });
  const { result, state } = await runFixture(t, {
    items: [item('A'), item('B'), item('C'), item('D')],
    driver,
    settings,
  });

  // A fails (1), B succeeds (resets to 0), C fails (1), D fails (2) - the
  // batch only stops once 2 in a row actually happen, so all 4 ran.
  assert.equal(result.ok, 1);
  assert.equal(result.failed, 3);
  assert.equal(state.get('A').status, 'failed');
  assert.equal(state.get('B').status, 'done');
  assert.equal(state.get('C').status, 'failed');
  assert.equal(state.get('D').status, 'failed');
});

test('maxConsecutiveFailures: 0 disables the guard entirely', async (t) => {
  const driver = failingDriver();
  const settings = makeSettings({ generation: { maxConsecutiveFailures: 0 } });
  const { result, state } = await runFixture(t, {
    items: [item('A'), item('B'), item('C')],
    driver,
    settings,
  });

  assert.equal(result.failed, 3);
  assert.equal(state.get('A').status, 'failed');
  assert.equal(state.get('B').status, 'failed');
  assert.equal(state.get('C').status, 'failed');
});

test('defaults to 3 when maxConsecutiveFailures is not set', async (t) => {
  const driver = failingDriver();
  const { result, state } = await runFixture(t, {
    items: [item('A'), item('B'), item('C'), item('D')],
    driver,
    // default settings: no generation.maxConsecutiveFailures override
  });

  assert.equal(result.failed, 3);
  assert.equal(state.get('A').status, 'failed');
  assert.equal(state.get('B').status, 'failed');
  assert.equal(state.get('C').status, 'failed');
  assert.equal(state.get('D').status, 'pending');
});

test('--fail-fast stops after the first failure regardless of maxConsecutiveFailures', async (t) => {
  const driver = failingDriver();
  const settings = makeSettings({ generation: { maxConsecutiveFailures: 5 } });
  const { result, state } = await runFixture(t, {
    items: [item('A'), item('B')],
    driver,
    settings,
    options: { failFast: true },
  });

  assert.equal(result.failed, 1);
  assert.equal(state.get('A').status, 'failed');
  assert.equal(state.get('B').status, 'pending');
});

test('the CLI --max-consecutive-failures flag overrides the config default', async (t) => {
  const driver = failingDriver();
  // options.maxConsecutiveFailures (what the CLI flag threads through) must
  // win over settings.generation.maxConsecutiveFailures (config default).
  const settings = makeSettings({ generation: { maxConsecutiveFailures: 10 } });
  const { result, state } = await runFixture(t, {
    items: [item('A'), item('B'), item('C')],
    driver,
    settings,
    options: { maxConsecutiveFailures: 1 },
  });

  assert.equal(result.failed, 1);
  assert.equal(state.get('A').status, 'failed');
  assert.equal(state.get('B').status, 'pending');
  assert.equal(state.get('C').status, 'pending');
});
