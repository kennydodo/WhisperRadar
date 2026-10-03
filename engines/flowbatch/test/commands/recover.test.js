import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

import { labelMatches, missingItems, normalizePrompt, planMatches, recoverJob } from '../../commands/recover.js';
import { RunState } from '../../src/runner/state.js';
import { ROOT } from '../../src/lib/paths.js';
import { setLevel } from '../../src/lib/log.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

setLevel('silent');

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4]);
const CDN = 'https://flow-content.google/image/';

function item(id, prompt, index) {
  return {
    id,
    index,
    outputFile: `${id}.png`,
    outputName: id,
    prompt,
    refs: [],
    refNames: [],
  };
}

function tile(key, text, index) {
  return {
    index,
    key,
    src: `${CDN}${key}`,
    text,
    label: '',
    uploaded: false,
    failed: false,
    canRedo: true,
    hasImage: true,
  };
}

const PROMPT_A = 'A cinematic wide shot of a red fox trotting through a misty pine forest at dawn';
const PROMPT_B = 'Close-up of an owl turning its head over one shoulder, soft studio light';

function fakeDriver({ tiles, prompts = {} } = {}) {
  return {
    calls: [],
    async listGeneratedResults() {
      return tiles ?? [];
    },
    async snapshotAssets() {
      return { selector: 'tile', entries: (tiles ?? []).map((entry, i) => ({ ...entry, index: i })) };
    },
    async readTilePrompt(index) {
      const entry = (tiles ?? [])[index];
      this.calls.push(`read:${entry ? entry.key : '?'}`);
      return entry ? (prompts[entry.key] ?? null) : null;
    },
    async fetchBytes() {
      return Buffer.from(PNG);
    },
    async downloadAsset() {
      return { method: null };
    },
    async convertToPng() {
      return false;
    },
  };
}

function makeState(dir, items, name = 'recover-test') {
  return RunState.open(RunState.pathFor(dir, name), {
    jobName: name,
    jobPath: path.join(dir, 'job.json'),
    items,
    projectUrl: null,
  });
}

test('normalizePrompt collapses whitespace and case', () => {
  assert.equal(normalizePrompt('  A Red\t\nfox '), 'a red fox');
});

test('labelMatches requires a 20+ char full-prefix or an exact match', () => {
  assert.equal(labelMatches(PROMPT_A, 'A cinematic wide shot of a red fox trot'), true);
  assert.equal(labelMatches('A cat', 'A cat'), true);
  assert.equal(labelMatches('A cat', 'A cat sleeping on the warm windowsill by noon'), false);
  assert.equal(labelMatches(PROMPT_A, 'Close-up of an owl turning'), false);
});

test('planMatches claims unique label hits and defers ambiguous ones', () => {
  const twinA = 'A cinematic wide shot of a red fox trotting through a misty pine forest at dawn';
  const twinB = 'A cinematic wide shot of a red fox trotting along a gravel road at dusk';
  const items = [item('A01', twinA, 0), item('A02', twinB, 1)];
  const ambiguous = tile('t0', twinA.slice(0, 30), 0);
  const { matches, restItems, restTiles } = planMatches(items, [ambiguous, tile('t1', twinB, 1)]);
  assert.deepEqual(matches.map((m) => m.tile.key), ['t1']);
  assert.deepEqual(restItems.map((i) => i.id), ['A01']);
  assert.deepEqual(restTiles.map((t) => t.key), ['t0']);
});

test('missingItems ignores outputs already on disk (any extension, by stem)', () => {
  const dir = makeTempDir();
  try {
    fs.mkdirSync(path.join(dir, 'out'));
    writeFile(dir, 'out/A01.jpg', PNG);
    const job = { outputsDir: path.join(dir, 'out'), items: [item('A01', PROMPT_A, 0), item('A02', PROMPT_B, 1)] };
    assert.deepEqual(missingItems(job).map((i) => i.id), ['A02']);
  } finally {
    removeDir(dir);
  }
});

test('recoverJob saves the label-matched tile under the exact job name', async () => {
  const dir = makeTempDir();
  try {
    const job = {
      name: 'recover-test',
      outputsDir: path.join(dir, 'out'),
      projectUrl: null,
      items: [item('A01', PROMPT_A, 0), item('A02', PROMPT_B, 1)],
    };
    const driver = fakeDriver({ tiles: [tile('t1', PROMPT_B, 0)] });
    const state = makeState(dir, job.items);
    const report = await recoverJob({ job, driver, state, upscale: { tier: 'off' } });

    assert.deepEqual(report.recovered.map((entry) => entry.id), ['A02']);
    assert.deepEqual(report.stillMissing, ['A01']);
    assert.ok(fs.existsSync(path.join(job.outputsDir, 'A02.png')));
    assert.equal(state.isDone('A02'), true);
    assert.equal(state.get('A01').status, 'pending');
  } finally {
    removeDir(dir);
  }
});

test('recoverJob identifies an unlabeled tile via the redo-control prompt read', async () => {
  const dir = makeTempDir();
  try {
    const job = {
      name: 'recover-test',
      outputsDir: path.join(dir, 'out'),
      projectUrl: null,
      items: [item('B01', PROMPT_A, 0), item('B02', PROMPT_B, 1)],
    };
    const driver = fakeDriver({
      tiles: [tile('u0', '', 0)],
      prompts: { u0: PROMPT_B },
    });
    const report = await recoverJob({ job, driver, state: makeState(dir, job.items), upscale: { tier: 'off' } });

    assert.deepEqual(report.recovered.map((e) => `${e.id}:${e.how}`), ['B02:prompt-read']);
  } finally {
    removeDir(dir);
  }
});

test('recoverJob identifies a caption-labelled reloaded-gallery tile via the redo read', async () => {
  const dir = makeTempDir();
  try {
    const job = {
      name: 'recover-test',
      outputsDir: path.join(dir, 'out'),
      projectUrl: null,
      items: [item('B01', PROMPT_A, 0), item('B02', PROMPT_B, 1)],
    };
    // What a reloaded project actually serves: a signed same-origin proxy URL
    // and a Flow caption instead of the prompt. Only the redo read can say
    // which item it is.
    const reloaded = {
      index: 0,
      key: 'https://flow.google.com/asb/ANqvLOZkl=s1600-rw',
      src: 'https://flow.google.com/asb/ANqvLOZkl=s1600-rw',
      text: '',
      label: 'Woman auctioning vintage camera',
      uploaded: false,
      failed: false,
      canRedo: true,
      hasImage: true,
    };
    const driver = fakeDriver({ tiles: [reloaded], prompts: { [reloaded.key]: PROMPT_B } });
    const report = await recoverJob({
      job,
      driver,
      state: makeState(dir, job.items),
      upscale: { tier: 'off' },
    });
    assert.deepEqual(report.recovered.map((entry) => `${entry.id}:${entry.how}`), ['B02:prompt-read']);
  } finally {
    removeDir(dir);
  }
});

test('recoverJob falls back to submission order only when counts agree and prompts are distinct', async () => {
  const dir = makeTempDir();
  try {
    const job = {
      name: 'recover-test',
      outputsDir: path.join(dir, 'out'),
      projectUrl: null,
      items: [item('C01', PROMPT_A, 0), item('C02', PROMPT_B, 1)],
    };
    const tiles = [tile('t0', '', 0), tile('t1', '', 1)];
    const report = await recoverJob({
      job,
      driver: fakeDriver({ tiles }),
      state: makeState(dir, job.items),
      upscale: { tier: 'off' },
    });
    // Newest tile first in the grid: C02 (submitted last) gets t1... the tiles
    // are ordered newest-first, so byAge pairs C01 with the LATER key.
    assert.deepEqual(report.recovered.map((e) => `${e.id}:${e.how}`).sort(), ['C01:order', 'C02:order']);

    const off = await recoverJob({
      job,
      driver: fakeDriver({ tiles: [tile('t0', '', 0)] }),
      state: makeState(dir, job.items),
      upscale: { tier: 'off' },
    });
    assert.deepEqual(off.recovered, []);
  } finally {
    removeDir(dir);
  }
});

test('recoverJob with nothing missing reports everything already present', async () => {
  const dir = makeTempDir();
  try {
    fs.mkdirSync(path.join(dir, 'out'));
    writeFile(dir, 'out/A01.png', PNG);
    const job = { name: 'recover-test', outputsDir: path.join(dir, 'out'), projectUrl: null, items: [item('A01', PROMPT_A, 0)] };
    const reportPath = path.join(dir, 'report.json');
    const report = await recoverJob({
      job,
      driver: fakeDriver({}),
      state: makeState(dir, job.items),
      reportPath,
      upscale: { tier: 'off' },
    });
    assert.deepEqual(report.alreadyPresent, ['A01']);
    assert.deepEqual(report.stillMissing, []);
    assert.deepEqual(JSON.parse(fs.readFileSync(reportPath, 'utf8')).schemaVersion, 1);
  } finally {
    removeDir(dir);
  }
});

test('recover --dry-run plans without launching a browser', () => {
  const dir = makeTempDir();
  try {
    const jobPath = writeFile(
      dir,
      'job.json',
      JSON.stringify({
        name: 'dry-run-test',
        outputsDir: path.join(dir, 'out'),
        images: [{ file: 'A01.png', prompt: PROMPT_A }],
      })
    );
    const out = spawnSync(process.execPath, ['src/cli.js', 'recover', '--job', jobPath, '--dry-run', '--no-color'], {
      cwd: ROOT,
      encoding: 'utf8',
    });
    assert.equal(out.status, 0, out.stdout + out.stderr);
    assert.match(out.stdout, /Recover plan for job "dry-run-test"/);
    assert.match(out.stdout, /missing : 1 of 1/);
  } finally {
    removeDir(dir);
  }
});
