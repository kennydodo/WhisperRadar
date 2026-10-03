import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveReferenceStatuses, uniqueRefs } from '../../commands/prepare.js';
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
  assert.deepEqual(out, [{ name: 'A', kind: 'image', status: 'reused', path: 'a.png' }]);
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
  assert.deepEqual(Object.keys(out[0]).sort(), ['kind', 'name', 'path', 'status']);
});
