import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

import { FlowDriver } from '../../src/flow/driver.js';
import { setLevel } from '../../src/lib/log.js';
import { makePage, makeContext } from '../../test-support/fake-page.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

setLevel('silent');

const FINAL = 'https://flow-content.google/image/result';
const FINAL_2 = 'https://flow-content.google/image/result-2';
const PLACEHOLDER = 'https://flow.google.com/asb/placeholder';

const SELECTORS = {
  assetTile: ['tile'],
  tileRedoButton: ['redo'],
  promptBox: ['prompt'],
  generatingIndicator: ['generating'],
  generationRefusal: ['refusal'],
  errorBanner: ['banner'],
  assetPickerDialog: ['picker'],
  overlayPane: ['overlay'],
  promptReferenceChip: ['chip'],
  promptReferenceRemoveButton: ['chip-remove'],
  newProjectButton: ['newproject'],
};

function makeDriver(page) {
  return new FlowDriver({
    page,
    context: makeContext(page.state),
    selectors: SELECTORS,
    settings: {
      flowUrl: 'https://flow.google.com',
      timeouts: { selectorMs: 0, readyMs: 0, navigationMs: 0, generationMs: 0, downloadMs: 0 },
    },
  });
}

test('snapshotAssets maps tiles to result metadata', async () => {
  const page = makePage({ requestHandler: () => Buffer.from('x') });
  page.set('tile', [
    { src: FINAL, text: 'A generated still', redo: true },
    { src: PLACEHOLDER, text: 'Maya.png', width: 512, height: 512 },
    { text: 'Failed to generate' },
  ]);
  const snap = await makeDriver(page).snapshotAssets();

  assert.equal(snap.selector, 'tile');
  assert.equal(snap.entries.length, 3);
  const [generated, reference, failed] = snap.entries;

  assert.equal(generated.canRedo, true);
  assert.equal(generated.uploaded, false);
  assert.equal(generated.failed, false);
  assert.equal(generated.hasImage, true);
  assert.equal(generated.key, FINAL);

  assert.equal(reference.uploaded, true);
  assert.equal(reference.canRedo, false);

  assert.equal(failed.failed, true);
  assert.equal(failed.hasImage, false);
  assert.equal(failed.key, '#2');
});

test('snapshotAssets returns nothing when no tile selector resolves', async () => {
  const page = makePage();
  page.set('tile', []);
  assert.deepEqual(await makeDriver(page).snapshotAssets(), { selector: null, entries: [] });
});

test('waitForNewAssets claims a new finished result', async () => {
  const page = makePage({ requestHandler: () => Buffer.from('result-bytes') });
  page.set('tile', [{ src: FINAL, text: 'Maya holding perfume bottles', redo: true }]);
  const out = await makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0 });

  assert.equal(out.added.length, 1);
  assert.equal(out.added[0].src, FINAL);
});

test('waitForNewAssets ignores a reference tile labelled with its filename', async () => {
  const page = makePage({ requestHandler: () => Buffer.from('ref-bytes') });
  page.set('tile', [{ src: FINAL, text: 'Maya.png', redo: false }]);
  await assert.rejects(
    makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0, excludeNames: ['Maya'] }),
    /No new result tile/,
  );
});

test('waitForNewAssets ignores an uploaded tile', async () => {
  const page = makePage({ requestHandler: () => Buffer.from('ref-bytes') });
  page.set('tile', [{ src: FINAL, text: 'BG_BACKYARD_01.png reference', redo: false }]);
  await assert.rejects(makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0 }), /No new result tile/);
});

test('waitForNewAssets refuses bytes already seen on the page', async () => {
  const page = makePage({ requestHandler: () => Buffer.from('shared-bytes') });
  page.set('tile', [{ src: FINAL, text: 'new result', redo: true }]);
  const before = {
    entries: [{ key: 'base', src: PLACEHOLDER, hasImage: true, uploaded: false, canRedo: false, text: '' }],
  };
  await assert.rejects(makeDriver(page).waitForNewAssets(before, 1, { timeout: 0 }), /No new result tile/);
});

test('waitForNewAssets reports a throttled refusal as non-retryable', async () => {
  const page = makePage();
  page.set('tile', [{ text: 'Failed. We noticed some unusual activity. You have not been charged.' }]);
  await assert.rejects(makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0 }), (error) => {
    assert.equal(error.name, 'GenerationError');
    assert.equal(error.retryable, false);
    return true;
  });
});

test('waitForNewAssets reports an ordinary failure as retryable', async () => {
  const page = makePage();
  page.set('tile', [{ text: 'Failed to generate' }]);
  await assert.rejects(makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0 }), (error) => {
    assert.equal(error.retryable, true);
    return /failed generation/.test(error.message);
  });
});

test('waitForNewAssets surfaces a fresh error banner when no tile appears', async () => {
  const page = makePage();
  page.set('banner', [{ text: 'You have not been charged for this generation' }]);
  await assert.rejects(makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0 }), /error while generating/);
});

test('a stale refusal banner is not attributed to the new generation', async () => {
  const page = makePage();
  page.set('refusal', [{ text: 'unusual activity' }]);
  const driver = makeDriver(page);
  await driver.markStaleAlerts();

  assert.equal(await driver.freshAlert(), null);
  await assert.rejects(driver.waitForNewAssets({ entries: [] }, 1, { timeout: 0 }), /No new result tile/);
});

test('waitForNewAssets salvages a result whose bytes failed once', async () => {
  let calls = 0;
  const page = makePage({
    requestHandler: () => {
      calls += 1;
      return calls === 1 ? null : Buffer.from('salvaged');
    },
  });
  page.set('tile', [{ src: FINAL, text: 'a result', redo: true }]);
  const out = await makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0 });

  assert.equal(out.salvaged, true);
  assert.equal(out.added.length, 1);
});

test('waitForNewAssets picks the newest candidate when several are present', async () => {
  const page = makePage({ requestHandler: (url) => Buffer.from(`bytes:${url}`) });
  page.set('tile', [
    { src: FINAL, text: 'first result', redo: true },
    { src: FINAL_2, text: 'second result', redo: true },
  ]);
  const out = await makeDriver(page).waitForNewAssets({ entries: [] }, 1, { timeout: 0 });

  assert.equal(out.added.length, 1);
  assert.equal(out.added[0].src, FINAL);
});

test('waitForNewAssets returns every candidate when several are expected', async () => {
  const page = makePage({ requestHandler: (url) => Buffer.from(`bytes:${url}`) });
  page.set('tile', [
    { src: FINAL, text: 'first result', redo: true },
    { src: FINAL_2, text: 'second result', redo: true },
  ]);
  const out = await makeDriver(page).waitForNewAssets({ entries: [] }, 2, { timeout: 0 });

  assert.equal(out.added.length, 2);
});

test('ownNewAssets owns each result only once', async () => {
  const page = makePage({ requestHandler: () => Buffer.from('once') });
  const driver = makeDriver(page);
  const entry = { src: FINAL, hasImage: true, canRedo: true, text: '' };

  assert.equal((await driver.ownNewAssets([entry])).length, 1);
  assert.equal((await driver.ownNewAssets([entry])).length, 0);
});

test('fetchBytes only returns bytes for a fetchable http url', async () => {
  const page = makePage({
    requestHandler: (url) => {
      if (url === 'https://cdn/ok') return Buffer.from('x');
      if (url === 'https://cdn/empty') return Buffer.alloc(0);
      return null;
    },
  });
  const driver = makeDriver(page);

  assert.deepEqual(await driver.fetchBytes('https://cdn/ok'), Buffer.from('x'));
  assert.equal(await driver.fetchBytes('https://cdn/empty'), null);
  assert.equal(await driver.fetchBytes('https://cdn/miss'), null);
  assert.equal(await driver.fetchBytes('/relative'), null);
  assert.equal(await driver.fetchBytes(''), null);
});

test('convertToPng writes a real PNG from the canvas result', async (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const input = writeFile(dir, 'in.jpg', Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]));
  const pngBytes = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 9, 9, 9]);

  const page = makePage({ pageEvaluate: async () => pngBytes.toString('base64') });
  const out = path.join(dir, 'out.png');
  assert.equal(await makeDriver(page).convertToPng(input, out), true);
  assert.deepEqual(fs.readFileSync(out), pngBytes);
});

test('convertToPng returns false when the canvas result is unusable', async (t) => {
  const dir = makeTempDir();
  t.after(() => removeDir(dir));
  const input = writeFile(dir, 'in.jpg', Buffer.from([0xff, 0xd8, 0xff]));

  const page = makePage({ pageEvaluate: async () => Buffer.from('not a png').toString('base64') });
  const driver = makeDriver(page);
  assert.equal(await driver.convertToPng(input, path.join(dir, 'a.png')), false);

  page.state.pageEvaluate = async () => '';
  assert.equal(await driver.convertToPng(input, path.join(dir, 'b.png')), false);
});

function stubAttachments(driver) {
  const existing = [];
  const uploads = [];
  let confirm = true;
  driver.attachExistingAsset = async (name) => {
    existing.push(name);
    return false;
  };
  driver.attachUploadedFiles = async (files) => {
    uploads.push(...files);
  };
  driver.waitForReferences = async () => confirm;
  return { existing, uploads, setConfirm: (value) => { confirm = value; } };
}

test('addReferences reuses an existing project asset without uploading', async () => {
  const page = makePage();
  const driver = makeDriver(page);
  const spy = stubAttachments(driver);
  driver.attachExistingAsset = async (name) => {
    spy.existing.push(name);
    return true;
  };

  await driver.addReferences([{ name: 'Maya', path: 'C:/m.png' }], { mode: 'reuse' });
  assert.deepEqual(spy.existing, ['Maya']);
  assert.deepEqual(spy.uploads, []);
});

test('addReferences uploads a missing reference in reuse mode', async () => {
  const page = makePage();
  const driver = makeDriver(page);
  const spy = stubAttachments(driver);

  await driver.addReferences([{ name: 'Maya', path: 'C:/m.png' }], { mode: 'reuse' });
  assert.deepEqual(spy.uploads, ['C:/m.png']);
});

test('addReferences never uploads in assets mode', async () => {
  const page = makePage();
  const driver = makeDriver(page);
  const spy = stubAttachments(driver);

  await driver.addReferences([{ name: 'Maya', path: 'C:/m.png' }], { mode: 'assets' });
  assert.deepEqual(spy.existing, ['Maya']);
  assert.deepEqual(spy.uploads, []);
});

test('addReferences skips a missing reference with no local path', async () => {
  const page = makePage();
  const driver = makeDriver(page);
  const spy = stubAttachments(driver);

  await driver.addReferences([{ name: 'Maya', path: null }], { mode: 'reuse' });
  assert.deepEqual(spy.uploads, []);
});

test('addReferences always uploads in upload mode', async () => {
  const page = makePage();
  const driver = makeDriver(page);
  const spy = stubAttachments(driver);

  await driver.addReferences([{ name: 'Maya', path: 'C:/m.png' }], { mode: 'upload' });
  assert.deepEqual(spy.existing, []);
  assert.deepEqual(spy.uploads, ['C:/m.png']);
});

test('addReferences fails the item when the references cannot be confirmed', async () => {
  const page = makePage();
  const driver = makeDriver(page);
  const spy = stubAttachments(driver);
  spy.setConfirm(false);
  driver.attachExistingAsset = async () => true;

  await assert.rejects(driver.addReferences([{ name: 'Maya', path: null }], { mode: 'reuse' }), (error) => {
    assert.equal(error.retryable, true);
    return /could not confirm/.test(error.message);
  });
});

test('overlay and picker presence drive the dismissal helpers', async () => {
  const page = makePage();
  const driver = makeDriver(page);

  page.set('overlay', [{ visible: true }]);
  assert.equal(await driver.overlayIsOpen(), true);
  page.set('overlay', []);
  assert.equal(await driver.overlayIsOpen(), false);

  page.set('picker', [{}]);
  assert.equal(await driver.pickerIsOpen(), true);
  page.set('picker', []);
  assert.equal(await driver.closeAssetLibrary(), true);
});

test('dismissOverlays escapes until the overlay is gone', async () => {
  const page = makePage();
  page.set('overlay', [{ visible: true }]);
  page.state.onKey = (key) => {
    if (key === 'Escape') page.set('overlay', []);
  };
  assert.equal(await makeDriver(page).dismissOverlays(), true);
});

test('isProjectUnavailable detects a deleted-project 404', () => {
  const gone = makePage({ url: 'https://flow.google.com/404?reason=project' });
  assert.equal(makeDriver(gone).isProjectUnavailable(), true);

  const fine = makePage({ url: 'https://flow.google.com/project/abc' });
  assert.equal(makeDriver(fine).isProjectUnavailable(), false);
});

test('openProject reports an unavailable project instead of a selector error', async () => {
  const page = makePage();
  page.set('consentDismiss', [{}]);
  await assert.rejects(
    makeDriver(page).openProject('https://flow.google.com/404?reason=project'),
    /unavailable/,
  );
});

test('readTilePrompt clicks the tile redo control, reads the composer, and clears it', async () => {
  const page = makePage({ requestHandler: () => null });
  page.set('tile', [{ src: FINAL, text: 'a result', redo: true, children: { redo: [{}] } }]);
  page.set('prompt', [{ text: 'the stored prompt for this tile' }]);
  const driver = makeDriver(page);

  const text = await driver.readTilePrompt(0, { selector: 'tile' });
  assert.equal(text, 'the stored prompt for this tile');
  const keys = page.state.calls.filter((call) => call.method === 'press').map((call) => call.key);
  assert.ok(keys.includes('Control+A'), 'the composer must be cleared after the read');
});

test('readTilePrompt returns null when the tile carries no redo control', async () => {
  const page = makePage();
  page.set('tile', [{ src: FINAL, text: 'an upload' }]);
  assert.equal(await makeDriver(page).readTilePrompt(0, { selector: 'tile' }), null);
});

test('listGeneratedResults keeps finished redo tiles and drops uploads, placeholders and failures', async () => {
  const page = makePage({ pageEvaluate: async () => false });
  page.set('tile', [
    { src: FINAL, text: 'a generated still', redo: true },
    { src: PLACEHOLDER, text: 'Maya.png', redo: false },
    { src: FINAL_2, text: 'Maya.png', redo: false },
    { src: FINAL, text: 'Failed. unusual activity', redo: false },
  ]);
  const results = await makeDriver(page).listGeneratedResults({ initialSettle: false, settleMs: 0 });
  assert.equal(results.length, 1);
  assert.equal(results[0].src, FINAL);
});

test('listGeneratedResults accepts reloaded gallery tiles served through the asb proxy', async () => {
  const page = makePage({ pageEvaluate: async () => false });
  const asb = 'https://flow.google.com/asb/ANqvLOZklFx=s1600-rw';
  page.set('tile', [
    { src: asb, redo: true, attrs: { 'aria-label': 'Woman auctioning vintage camera' } },
    { src: 'https://flow.google.com/asb/ANqvLOother=s1600-rw', redo: true, attrs: { 'aria-label': 'Failed. unusual activity' } },
    { src: FINAL, redo: true },
  ]);
  const results = await makeDriver(page).listGeneratedResults({ initialSettle: false, settleMs: 0 });
  // The proxy tile is a real result; the one Flow captioned as failed is not.
  assert.deepEqual(results.map((entry) => entry.src), [asb, FINAL]);
});

test('snapshotAssets captures the tile caption as label', async () => {
  const page = makePage();
  page.set('tile', [{ src: FINAL, redo: true, attrs: { 'aria-label': 'Maya examining camera details' } }]);
  const snapshot = await makeDriver(page).snapshotAssets();
  assert.equal(snapshot.entries[0].label, 'Maya examining camera details');
});

test('scanAssets unions tiles across scrolls until the gallery stops moving', async () => {
  const page = makePage({ requestHandler: () => null });
  page.set('tile', [{ src: `${FINAL}-0`, text: 'still 0', redo: true }]);
  let moved = 0;
  page.state.pageEvaluate = async (fn, arg) => {
    if (arg === true) return false;
    moved += 1;
    page.set('tile', [{ src: `${FINAL}-${moved}`, text: `still ${moved}`, redo: true }]);
    return moved < 3;
  };
  const scanned = await makeDriver(page).scanAssets({ settleMs: 0, initialSettle: false });
  assert.ok(scanned.entries.length >= 2, `expected the union of mounted tiles, got ${scanned.entries.length}`);
});


test('ensureProject without forceNew adopts an open project', async () => {
  const page = makePage({ url: 'https://flow.google.com/project/existing-id-1234' });
  const result = await makeDriver(page).ensureProject(null);
  assert.equal(result.created, false);
});

test('ensureProject with forceNew creates a fresh project', async () => {
  const page = makePage({ url: 'https://flow.google.com' });
  page.set('prompt', [{ text: '' }]);
  page.set('newproject', [{ text: 'New project' }]);
  const result = await makeDriver(page).ensureProject(null, { forceNew: true });
  assert.equal(result.created, true);
});

test('ensureProject with forceNew refuses to reuse a project it cannot leave', async () => {
  const page = makePage({ url: 'https://flow.google.com/project/existing-id-1234' });
  await assert.rejects(
    () => makeDriver(page).ensureProject(null, { forceNew: true }),
    /Refusing to reuse it/,
  );
});
