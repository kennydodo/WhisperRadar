import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { loadJob } from '../../src/jobs/load.js';
import { ROOT } from '../../src/lib/paths.js';
import { log, setLevel } from '../../src/lib/log.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

setLevel('silent');

function writeJob(dir, name, value, { bom = false } = {}) {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return writeFile(dir, name, `${bom ? '\uFEFF' : ''}${text}`);
}

function captureWarnings(t) {
  const warnings = [];
  const original = log.warn;
  log.warn = (message) => warnings.push(String(message));
  t.after(() => {
    log.warn = original;
  });
  return warnings;
}

test('loads the images shape with style and resolved references', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  writeFile(dir, 'refs/Maya.png', 'x');
  writeFile(dir, 'refs/BG.png', 'x');
  const jobPath = writeJob(dir, 'job.json', {
    name: 'My Job',
    style: 'STYLE',
    refs: { Maya: 'refs/Maya.png', BG: 'refs/BG.png' },
    images: [
      { file: 'S01_01.png', prompt: 'hello', refs: ['Maya'] },
      { file: 'S01_02.png', prompt: 'world' },
    ],
  });

  const job = loadJob(jobPath);
  assert.equal(job.name, 'My-Job');
  assert.equal(job.style, 'STYLE');
  assert.equal(job.outputsDir, path.resolve(ROOT, 'output', 'My-Job'));
  assert.equal(job.items.length, 2);

  const first = job.items[0];
  assert.equal(first.id, 'S01_01');
  assert.equal(first.outputFile, 'S01_01.png');
  assert.equal(first.outputName, 'S01_01');
  assert.equal(first.prompt, 'STYLE hello');
  assert.deepEqual(first.refNames, ['Maya']);
  assert.equal(path.basename(first.refs[0].path), 'Maya.png');
  assert.equal(path.isAbsolute(first.refs[0].path), true);

  assert.deepEqual(job.items[1].refs, []);
});

test('stylePosition suffix appends, and an invalid value is rejected', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const jobPath = writeJob(dir, 'job.json', {
    style: 'STYLE',
    stylePosition: 'suffix',
    images: [{ file: 'a.png', prompt: 'hi' }],
  });
  assert.equal(loadJob(jobPath).items[0].prompt, 'hi STYLE');

  const badPath = writeJob(dir, 'bad.json', {
    stylePosition: 'middle',
    images: [{ file: 'a.png', prompt: 'hi' }],
  });
  assert.throws(() => loadJob(badPath), /stylePosition must be/);
});

test('matrix expands to prompts x refSets with stable ids', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  writeFile(dir, 'refs/Maya.png', 'x');
  writeFile(dir, 'refs/BG.png', 'x');
  const jobPath = writeJob(dir, 'job.json', {
    name: 'm',
    matrix: {
      prompts: ['p1', 'p2'],
      refSets: { alice: ['refs/Maya.png'], bob: ['refs/BG.png'] },
    },
  });

  const job = loadJob(jobPath);
  assert.deepEqual(job.items.map((item) => item.id), ['p1-alice', 'p1-bob', 'p2-alice', 'p2-bob']);
  assert.deepEqual(job.items[0].refNames, ['Maya']);
});

test('matrix validates prompts and refSets', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const noPrompts = writeJob(dir, 'a.json', { matrix: { prompts: [], refSets: { x: [] } } });
  assert.throws(() => loadJob(noPrompts), /matrix\.prompts must contain/);

  const noSets = writeJob(dir, 'b.json', { matrix: { prompts: ['p'], refSets: {} } });
  assert.throws(() => loadJob(noSets), /matrix\.refSets must contain/);
});

test('unrecognised keys warn and private keys do not', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const warnings = captureWarnings(t);
  const jobPath = writeJob(dir, 'job.json', {
    name: 'j',
    _note: 'private',
    bogus: 1,
    defaults: { nope: 2 },
    images: [{ file: 'a.png', prompt: 'p' }],
  });

  loadJob(jobPath);
  assert.ok(warnings.some((w) => w.includes('unrecognised key "bogus"')));
  assert.ok(warnings.some((w) => w.includes('unrecognised key "nope"')));
  assert.equal(warnings.some((w) => w.includes('_note')), false);
});

test('extra arrays and multiple containers warn, images wins', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const warnings = captureWarnings(t);
  const jobPath = writeJob(dir, 'job.json', {
    name: 'j',
    shots: [{}, {}],
    images: [{ file: 'a.png', prompt: 'p' }],
    items: [{ file: 'b.png', prompt: 'q' }],
  });

  const job = loadJob(jobPath);
  assert.equal(job.items.length, 1);
  assert.equal(job.items[0].outputFile, 'a.png');
  assert.ok(warnings.some((w) => w.includes('top-level "shots"')));
  assert.ok(warnings.some((w) => w.includes('"images"') && w.includes('"items"')));
});

test('duplicate item ids and duplicate references are rejected', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  writeFile(dir, 'refs/Maya.png', 'x');

  const dupIds = writeJob(dir, 'ids.json', {
    images: [{ id: 'x', prompt: 'a' }, { id: 'x', prompt: 'b' }],
  });
  assert.throws(() => loadJob(dupIds), /Duplicate item id "x"/);

  const dupRefs = writeJob(dir, 'refs.json', {
    refs: { Maya: 'refs/Maya.png' },
    images: [{ file: 'a.png', prompt: 'a', refs: ['Maya', 'Maya'] }],
  });
  assert.throws(() => loadJob(dupRefs), /Duplicate reference "Maya"/);
});

test('invalid item fields are rejected', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));

  const empty = writeJob(dir, 'empty.json', { images: [{ file: 'a.png', prompt: '   ' }] });
  assert.throws(() => loadJob(empty), /empty prompt/);

  const mode = writeJob(dir, 'mode.json', { images: [{ file: 'a.png', prompt: 'p', mode: 'audio' }] });
  assert.throws(() => loadJob(mode), /unsupported mode "audio"/);

  const outputs = writeJob(dir, 'out.json', { images: [{ file: 'a.png', prompt: 'p', outputs: 0 }] });
  assert.throws(() => loadJob(outputs), /invalid outputs/);

  const refMode = writeJob(dir, 'refmode.json', { images: [{ file: 'a.png', prompt: 'p', refMode: 'nope' }] });
  assert.throws(() => loadJob(refMode), /unsupported refMode "nope"/);
});

test('a missing local reference warns and stays attachable by name', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const warnings = captureWarnings(t);
  const jobPath = writeJob(dir, 'job.json', {
    refs: { Maya: 'refs/missing.png' },
    images: [{ file: 'a.png', prompt: 'p', refs: ['Maya'] }],
  });

  const job = loadJob(jobPath);
  assert.equal(job.items[0].refs[0].name, 'Maya');
  assert.equal(job.items[0].refs[0].path, null);
  assert.ok(warnings.some((w) => w.includes('local file not found')));
});

test('a legacy refs array maps by filename stem', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  writeFile(dir, 'refs/Maya.png', 'x');
  const warnings = captureWarnings(t);
  const jobPath = writeJob(dir, 'job.json', {
    refs: ['refs/Maya.png', 'refs/gone.png'],
    images: [{ file: 'a.png', prompt: 'p', refs: ['Maya'] }],
  });

  const job = loadJob(jobPath);
  assert.deepEqual(job.items[0].refNames, ['Maya']);
  assert.equal(path.basename(job.items[0].refs[0].path), 'Maya.png');
  assert.ok(warnings.some((w) => w.includes('Reference file not found')));
});

test('a ref entry may carry its own name and path', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  writeFile(dir, 'refs/Maya.png', 'x');

  const jobPath = writeJob(dir, 'job.json', {
    images: [{ file: 'a.png', prompt: 'p', refs: [{ name: 'Maya', path: 'refs/Maya.png' }] }],
  });
  assert.equal(path.basename(loadJob(jobPath).items[0].refs[0].path), 'Maya.png');

  const badPath = writeJob(dir, 'bad.json', {
    images: [{ file: 'a.png', prompt: 'p', refs: [{ name: 'Maya', path: 'refs/nope.png' }] }],
  });
  assert.throws(() => loadJob(badPath), /Reference image not found/);
});

test('unknown asset names resolve to attach-by-name and path-shaped misses warn', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const warnings = captureWarnings(t);

  const plain = writeJob(dir, 'plain.json', {
    images: [{ file: 'a.png', prompt: 'p', refs: ['SomeAsset'] }],
  });
  const job = loadJob(plain);
  assert.equal(job.items[0].refs[0].name, 'SomeAsset');
  assert.equal(job.items[0].refs[0].path, null);

  const pathLike = writeJob(dir, 'path.json', {
    images: [{ file: 'a.png', prompt: 'p', refs: ['refs/nope.png'] }],
  });
  const job2 = loadJob(pathLike);
  assert.equal(job2.items[0].refs[0].name, 'nope');
  assert.ok(warnings.some((w) => w.includes('is not on disk')));
});

test('over-long prompts warn against maxPromptChars', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const warnings = captureWarnings(t);
  const jobPath = writeJob(dir, 'job.json', {
    name: 'j',
    defaults: { maxPromptChars: 5 },
    images: [{ file: 'a.png', prompt: '0123456789' }],
  });

  loadJob(jobPath);
  assert.ok(warnings.some((w) => w.includes('exceed 5 characters')));
});

test('mojibake is warned about and repaired on request', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const jobPath = writeJob(dir, 'job.json', {
    images: [{ file: 'a.png', prompt: 'caf\u00c3\u00a9' }],
  });

  const warnings = captureWarnings(t);
  const plain = loadJob(jobPath);
  assert.equal(plain.items[0].prompt, 'caf\u00c3\u00a9');
  assert.ok(warnings.some((w) => w.includes('mojibake')));

  const repaired = loadJob(jobPath, { repairEncoding: true });
  assert.equal(repaired.items[0].prompt, 'caf\u00e9');
});

test('a UTF-8 BOM on the job file is tolerated', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const jobPath = writeJob(dir, 'job.json', { name: 'j', images: [{ file: 'a.png', prompt: 'p' }] }, { bom: true });
  assert.equal(loadJob(jobPath).items.length, 1);
});

test('outputsDir, projectUrl and project resolve from raw or settings', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));

  const explicit = writeJob(dir, 'explicit.json', {
    name: 'j',
    outputsDir: 'custom/dir',
    images: [{ file: 'a.png', prompt: 'p' }],
  });
  assert.equal(loadJob(explicit).outputsDir, path.resolve(ROOT, 'custom/dir'));

  const fromSettings = writeJob(dir, 'settings.json', { name: 'j', images: [{ file: 'a.png', prompt: 'p' }] });
  const job = loadJob(fromSettings, {
    settings: { projectUrl: 'https://settings', generation: { project: 'P' } },
  });
  assert.equal(job.projectUrl, 'https://settings');
  assert.equal(job.project, 'P');

  const overridden = writeJob(dir, 'raw.json', {
    name: 'j',
    projectUrl: 'https://raw',
    images: [{ file: 'a.png', prompt: 'p' }],
  });
  assert.equal(loadJob(overridden, { settings: { projectUrl: 'https://settings' } }).projectUrl, 'https://raw');
});

test('a missing job file and a non-object payload are rejected', (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  assert.throws(() => loadJob(path.join(dir, 'nope.json')), /Job file not found/);

  const array = writeJob(dir, 'array.json', '[1,2]');
  assert.throws(() => loadJob(array), /must contain a JSON object/);
});
