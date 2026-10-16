import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveReferenceStatuses, uniqueRefs, referenceRecordFor } from '../../commands/prepare.js';
import { setLevel } from '../../src/lib/log.js';

setLevel('silent');

function refDriver({ has = () => false, onUpload = () => {} } = {}) {
  return {
    galleryHasAsset: async (name) => has(name),
    attachUploadedFiles: async (files) => {
      onUpload(files);
    },
  };
}

test('uniqueRefs keeps first-seen order and drops duplicates', () => {
  const job = {
    items: [
      { refs: [{ name: 'A', path: 'a.png' }, { name: 'B', path: 'b.png' }] },
      { refs: [{ name: 'A', path: 'other.png' }, { name: 'C', path: null }] },
    ],
  };
  const refs = uniqueRefs(job);
  assert.deepEqual(refs.map((ref) => ref.name), ['A', 'B', 'C']);
  assert.equal(refs[0].path, 'a.png');
});

test('a reference already in the project is reported reused', async () => {
  const driver = refDriver({
    has: () => true,
    onUpload: () => {
      throw new Error('should not upload a reused reference');
    },
  });
  const out = await resolveReferenceStatuses(driver, [{ name: 'A', path: 'a.png' }], {
    fileExists: () => true,
  });
  assert.deepEqual(out, [{ name: 'A', kind: 'image', status: 'reused', path: 'a.png', source: 'gallery' }]);
});

test('a ref the record vouches for is reused without probing or uploading', async () => {
  const driver = refDriver({
    has: (name) => {
      if (name === 'CH_A') throw new Error('should not probe a recorded reference');
      return true;
    },
    onUpload: () => {
      throw new Error('should not upload a recorded reference');
    },
  });
  const out = await resolveReferenceStatuses(
    driver,
    [{ name: 'CH_A', path: 'refs/CH_A.png' }, { name: 'BG_B', path: 'refs/BG_B.png' }],
    { fileExists: () => true, record: new Set(['CH_A']) },
  );
  assert.equal(out[0].status, 'reused');
  assert.equal(out[0].source, 'record');
  // an unrecorded name still takes the normal gallery probe path
  assert.equal(out[1].source, 'gallery');
});

test('the record only applies to the project it was written for', () => {
  const url = 'https://flow.google.com/project/aaaa';
  assert.deepEqual(
    [...referenceRecordFor({ projectUrl: url, refsGenerated: { projectUrl: url, names: ['CH_A'] } })],
    ['CH_A'],
  );
  assert.equal(referenceRecordFor({ projectUrl: 'https://flow.google.com/project/bbbb', refsGenerated: { projectUrl: url, names: ['CH_A'] } }), null);
  assert.equal(referenceRecordFor({ projectUrl: null, refsGenerated: { projectUrl: url, names: ['CH_A'] } }), null);
  assert.equal(referenceRecordFor({ projectUrl: url, refsGenerated: { projectUrl: url, names: [] } }), null);
  assert.equal(referenceRecordFor({ projectUrl: url }), null);
});

test('a missing reference with a local file is uploaded', async () => {
  const uploads = [];
  const driver = refDriver({ has: () => false, onUpload: (files) => uploads.push(files) });
  const out = await resolveReferenceStatuses(driver, [{ name: 'B', path: 'b.png' }], {
    fileExists: () => true,
  });
  assert.equal(out[0].status, 'uploaded');
  assert.deepEqual(uploads, [['b.png']]);
});

test('a missing reference without a path or file stays missing', async () => {
  const uploads = [];
  const driver = refDriver({ has: () => false, onUpload: (files) => uploads.push(files) });

  const noPath = await resolveReferenceStatuses(driver, [{ name: 'C', path: null }], {
    fileExists: () => true,
  });
  assert.equal(noPath[0].status, 'missing');

  const noFile = await resolveReferenceStatuses(driver, [{ name: 'D', path: 'gone.png' }], {
    fileExists: () => false,
  });
  assert.equal(noFile[0].status, 'missing');
  assert.deepEqual(uploads, []);
});

test('a reference that fails to attach is reported missing and does not abort the rest', async () => {
  const driver = refDriver({
    has: (name) => {
      if (name === 'A') throw new Error('element is not enabled');
      return true;
    },
  });
  const out = await resolveReferenceStatuses(
    driver,
    [{ name: 'A', path: 'a.png' }, { name: 'B', path: 'b.png' }],
    { fileExists: () => true },
  );
  assert.equal(out[0].status, 'missing');
  assert.equal(out[1].status, 'reused');
});

test('an upload click that times out is reported missing and does not abort the rest', async () => {
  const driver = refDriver({
    has: () => false,
    onUpload: () => {
      throw new Error('waiting for element to be visible, enabled and stable');
    },
  });
  const out = await resolveReferenceStatuses(
    driver,
    [{ name: 'A', path: 'a.png' }, { name: 'B', path: 'b.png' }],
    { fileExists: () => true },
  );
  assert.deepEqual(out.map((entry) => entry.status), ['missing', 'missing']);
});

test('a reference status entry matches the report contract shape', async () => {
  const out = await resolveReferenceStatuses(refDriver({ has: () => true }), [{ name: 'A', path: 'a.png' }], {
    fileExists: () => true,
  });
  assert.deepEqual(Object.keys(out[0]).sort(), ['kind', 'name', 'path', 'source', 'status']);
});

test('a generated ref is checked by its Flow caption and never uploaded', async () => {
  const probed = [];
  const driver = refDriver({
    has: (name) => {
      probed.push(name);
      return name === 'Two apes sitting hunched';
    },
    onUpload: () => {
      throw new Error('a generated reference must never be uploaded');
    },
  });
  const out = await resolveReferenceStatuses(
    driver,
    [
      { name: 'CH_APES', path: 'apes.png', assetName: 'Two apes sitting hunched' },
      { name: 'CH_TRIBE', path: 'tribe.png', assetName: 'Four stick figures standing toge' },
    ],
    { fileExists: () => true },
  );
  assert.deepEqual(probed, ['Two apes sitting hunched', 'Four stick figures standing toge']);
  assert.equal(out[0].status, 'reused');
  assert.equal(out[0].source, 'gallery');
  assert.equal(out[1].status, 'missing');
  assert.equal(out[1].generated, true);
});

test('uniqueRefs carries the recorded caption', () => {
  const refs = uniqueRefs({ items: [{ refs: [{ name: 'A', path: 'a.png', assetName: 'Two apes' }] }] });
  assert.equal(refs[0].assetName, 'Two apes');
});
