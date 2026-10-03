import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

import { runJob, selectItems } from '../../src/runner/run.js';
import { RunState } from '../../src/runner/state.js';
import { GenerationError } from '../../src/lib/errors.js';
import { setLevel } from '../../src/lib/log.js';
import { makeDriver, callsNamed } from '../../test-support/fake-driver.js';
import { makeTempDir, removeDir } from '../../test-support/tmp.js';

setLevel('silent');

const JPG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46]);
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00]);

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

function makeSettings({ timeouts = {}, generation = {} } = {}) {
  return {
    timeouts: { generationMs: 500, ...timeouts },
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

async function runFixture(t, { items, driver, settings = makeSettings(), options = {}, presetDone = [] }) {
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
  for (const id of presetDone) state.update(id, { status: 'done' });

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
  return { result, job, state, outputsDir };
}

test('selectItems filters by --only and rejects unknown ids', () => {
  const items = [{ id: 'a' }, { id: 'b' }];
  assert.deepEqual(selectItems(items, { only: ['b'] }).map((i) => i.id), ['b']);
  assert.throws(() => selectItems(items, { only: ['z'] }), /unknown item id/);
});

test('selectItems skips done items before applying the limit', () => {
  const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
  const state = { isDone: (id) => id === 'a' };
  assert.deepEqual(selectItems(items, { resume: true, limit: 2, state }).map((i) => i.id), ['b', 'c']);
  assert.deepEqual(selectItems(items, { resume: false, limit: 2, state }).map((i) => i.id), ['a', 'b']);
});

test('a dry run plans without launching the browser', async (t) => {
  const driver = makeDriver();
  const { result } = await runFixture(t, {
    items: [item('A')],
    driver,
    options: { dryRun: true },
  });
  assert.deepEqual(result, { ok: 0, failed: 0, skipped: 0, results: [] });
  assert.equal(driver.calls.length, 0);
});

test('runJob does nothing when every selected item is already done', async (t) => {
  const driver = makeDriver();
  const { result } = await runFixture(t, { items: [item('A')], driver, presetDone: ['A'] });
  assert.deepEqual(result, { ok: 0, failed: 0, skipped: 0, results: [] });
  assert.equal(driver.calls.length, 0);
});

test('a happy path generates, downloads by CDN, and records the item done', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k1', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  const { result, state, outputsDir } = await runFixture(t, { items: [item('S01_01')], driver });

  assert.equal(result.ok, 1);
  assert.equal(result.failed, 0);
  assert.equal(state.isDone('S01_01'), true);
  assert.equal(callsNamed(driver, 'openProject').length, 0);
  assert.equal(callsNamed(driver, 'goto').length, 1);
  assert.equal(callsNamed(driver, 'ensureProject').length, 1);

  const applied = callsNamed(driver, 'applyGenerationSettings');
  assert.equal(applied.length, 1);
  assert.deepEqual(applied[0].args[0], { mode: 'image', model: null, aspectRatio: null, outputs: 1, agent: false });

  // JPEG bytes with a .png requested: the failed conversion keeps the true extension,
  // and the failed upscale never adds a second file.
  assert.deepEqual(fs.readdirSync(outputsDir), ['S01_01.jpg']);
});

test('runJob re-encodes to PNG when the job asked for .png', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
    convertToPng: async (source, destination) => {
      fs.writeFileSync(destination, PNG);
      return true;
    },
  });
  const { outputsDir } = await runFixture(t, { items: [item('A', { outputFile: 'A.png' })], driver });
  assert.deepEqual(fs.readdirSync(outputsDir), ['A.png']);
});

test('multiple results get numeric suffixes', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k1', index: 0 }, { key: 'k2', index: 1 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  const { outputsDir } = await runFixture(t, { items: [item('shot', { outputFile: 'shot.png' })], driver });
  assert.deepEqual(fs.readdirSync(outputsDir).sort(), ['shot-1.jpg', 'shot-2.jpg']);
});

test('the --delay option replaces the configured gap between items', async (t) => {
  const makeOk = () =>
    makeDriver({
      waitForNewAssets: async () => ({ added: [{ key: 'k1', index: 0 }], selector: 'grid' }),
      fetchAssetBytes: async () => JPG,
    });
  const timed = async (settings, options) => {
    const start = Date.now();
    await runFixture(t, { items: [item('A1'), item('A2')], driver: makeOk(), settings, options });
    return Date.now() - start;
  };
  const configured = await timed(makeSettings({ generation: { delayBetweenItemsMs: 0 } }), {});
  const overridden = await timed(makeSettings({ generation: { delayBetweenItemsMs: 0 } }), {
    delayBetweenItemsMs: 150,
  });
  const beaten = await timed(makeSettings({ generation: { delayBetweenItemsMs: 400 } }), {
    delayBetweenItemsMs: 0,
  });
  assert.ok(overridden >= configured + 250, `two items, 150 ms each: ${overridden} vs ${configured}`);
  assert.ok(beaten < 300, `the option 0 wins over the 400 ms setting: ${beaten}`);
});

test('references are attached with the configured mode', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  const refs = [{ name: 'Maya', path: 'x' }];
  await runFixture(t, {
    items: [item('A', { refs, refNames: ['Maya'], refMode: 'assets' })],
    driver,
  });
  const attached = callsNamed(driver, 'addReferences');
  assert.equal(attached.length, 1);
  assert.deepEqual(attached[0].args[1], { mode: 'assets' });
});

test('a mention refMode types @names instead of attaching', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  await runFixture(t, {
    items: [item('A', { refMode: 'mention', refNames: ['Maya'] })],
    driver,
  });
  assert.equal(callsNamed(driver, 'mentionReferences').length, 1);
  assert.equal(callsNamed(driver, 'setPrompt').length, 0);
  assert.equal(callsNamed(driver, 'addReferences').length, 0);
});

test('a non-retryable refusal aborts the batch and leaves the rest pending', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => {
      throw new GenerationError('Flow refused the generation: "We noticed some unusual activity"', {
        retryable: false,
      });
    },
  });
  const { result, state } = await runFixture(t, { items: [item('A'), item('B')], driver });

  assert.equal(result.failed, 1);
  assert.equal(state.get('A').status, 'failed');
  assert.equal(state.get('B').status, 'pending');
  assert.equal(callsNamed(driver, 'setPrompt').length, 1);
});

test('an over-long prompt is skipped so the batch continues', async (t) => {
  let calls = 0;
  const driver = makeDriver({
    waitForNewAssets: async () => {
      calls += 1;
      if (calls === 1) throw new GenerationError('unusual activity', { retryable: false });
      return { added: [{ key: 'k', index: 0 }], selector: 'grid' };
    },
    fetchAssetBytes: async () => JPG,
  });
  const settings = makeSettings({ generation: { maxPromptChars: 3, maxCooldowns: 5 } });
  const { result, state } = await runFixture(t, {
    items: [item('LONG', { prompt: 'toolong' }), item('SHORT', { prompt: 'ok' })],
    driver,
    settings,
  });

  assert.equal(state.get('LONG').status, 'failed');
  assert.equal(state.get('SHORT').status, 'done');
  assert.equal(result.ok, 1);
  assert.equal(result.failed, 1);
});

test('a retryable error is retried until it succeeds', async (t) => {
  let attempts = 0;
  const driver = makeDriver({
    setPrompt: async () => {
      attempts += 1;
    },
    waitForNewAssets: async () => {
      if (attempts === 1) throw new GenerationError('transient');
      return { added: [{ key: 'k', index: 0 }], selector: 'grid' };
    },
    fetchAssetBytes: async () => JPG,
  });
  const { result, state } = await runFixture(t, { items: [item('R', { retries: 1 })], driver });

  assert.equal(result.ok, 1);
  assert.equal(state.get('R').attempts, 2);
});

test('a retryable error that keeps failing marks the item failed', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => {
      throw new GenerationError('still broken');
    },
  });
  const { result, state } = await runFixture(t, { items: [item('R', { retries: 1 })], driver });

  assert.equal(result.failed, 1);
  assert.equal(state.get('R').status, 'failed');
  assert.equal(state.get('R').attempts, 2);
  assert.match(state.get('R').error, /still broken/);
});

test('a non-retryable, non-rate-limit error stops the batch', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => {
      throw new GenerationError('cannot save', { retryable: false });
    },
  });
  const { result, state } = await runFixture(t, { items: [item('A'), item('B')], driver });

  assert.equal(result.failed, 1);
  assert.equal(state.get('B').status, 'pending');
  assert.equal(callsNamed(driver, 'setPrompt').length, 1);
});

test('a lost download is retried three times before the item fails', async (t) => {
  let downloads = 0;
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    snapshotAssets: async () => ({ entries: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => null,
    downloadAsset: async () => {
      downloads += 1;
      return { method: null };
    },
  });
  const { result } = await runFixture(t, { items: [item('A')], driver });

  assert.equal(downloads, 3);
  assert.equal(result.failed, 1);
});

test('a download that recovers on the third attempt is saved', async (t) => {
  let fetches = 0;
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    snapshotAssets: async () => ({ entries: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => {
      fetches += 1;
      return fetches === 3 ? JPG : null;
    },
    downloadAsset: async () => ({ method: null }),
  });
  const { result, outputsDir } = await runFixture(t, { items: [item('A')], driver });

  assert.equal(fetches, 3);
  assert.equal(result.ok, 1);
  assert.deepEqual(fs.readdirSync(outputsDir), ['A.jpg']);
});

test('an item that cannot be cleared in place falls back to a reload', async (t) => {
  let reloads = 0;
  const driver = makeDriver({
    clearComposerForNextItem: async () => false,
    reload: async () => {
      reloads += 1;
    },
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  const { result } = await runFixture(t, { items: [item('A'), item('B')], driver });

  assert.equal(result.ok, 2);
  assert.equal(reloads, 1);
  assert.equal(callsNamed(driver, 'clearComposerForNextItem').length, 1);
  assert.equal(callsNamed(driver, 'applyGenerationSettings').length, 2);
});

test('a changed per-item aspect ratio is re-applied in the clear path', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  const settings = makeSettings({ generation: { reapplySettingsAfterReset: false } });
  const { result } = await runFixture(t, {
    items: [item('A', { aspectRatio: '16:9' }), item('B', { aspectRatio: '1:1' })],
    driver,
    settings,
  });

  assert.equal(result.ok, 2);
  const applied = callsNamed(driver, 'applyGenerationSettings');
  assert.deepEqual(applied.map((call) => call.args[0].aspectRatio), ['16:9', '1:1']);
});

test('an unchanged aspect ratio is not re-applied when reapply is off', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  const settings = makeSettings({ generation: { reapplySettingsAfterReset: false } });
  const { result } = await runFixture(t, {
    items: [item('A', { aspectRatio: '16:9' }), item('B', { aspectRatio: '16:9' })],
    driver,
    settings,
  });

  assert.equal(result.ok, 2);
  assert.equal(callsNamed(driver, 'applyGenerationSettings').length, 1);
});

test('a signed-out page stops before any item runs', async (t) => {
  const driver = makeDriver({ looksSignedIn: async () => 'out' });
  await assert.rejects(
    runFixture(t, { items: [item('A')], driver }),
    /signed-out page/,
  );
  assert.equal(callsNamed(driver, 'setPrompt').length, 0);
});

test('a Google challenge stops before any item runs', async (t) => {
  const driver = makeDriver({ looksSignedIn: async () => 'challenge' });
  await assert.rejects(runFixture(t, { items: [item('A')], driver }), /extra verification/);
});

test('a known project opens by URL instead of navigating', async (t) => {
  const driver = makeDriver({
    waitForNewAssets: async () => ({ added: [{ key: 'k', index: 0 }], selector: 'grid' }),
    fetchAssetBytes: async () => JPG,
  });
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const items = [item('A')];
  const job = { name: 'j', items, outputsDir: path.join(dir, 'out'), projectUrl: 'https://flow/project/x', refMode: null };
  const state = RunState.open(path.join(dir, 'state.json'), {
    jobName: 'j',
    jobPath: path.join(dir, 'job.json'),
    items,
    projectUrl: job.projectUrl,
  });
  await runJob({
    job,
    driver,
    state,
    settings: makeSettings(),
    options: { only: null, limit: 0, resume: true, dryRun: false, dumpOnError: false },
  });

  assert.deepEqual(callsNamed(driver, 'openProject')[0].args[0], 'https://flow/project/x');
  assert.equal(callsNamed(driver, 'goto').length, 0);
  assert.equal(callsNamed(driver, 'ensureProject').length, 0);
});
