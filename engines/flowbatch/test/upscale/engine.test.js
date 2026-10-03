import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

import { usableIcdFiles } from '../../src/upscale/engine.js';
import { makeTempDir, removeDir, writeFile } from '../../test-support/tmp.js';

test('usableIcdFiles keeps only ICDs whose driver exists on this machine', () => {
  const dir = makeTempDir('flowbatch-icd-');
  try {
    const driver = writeFile(dir, 'driver.dll', '');
    writeFile(dir, 'nv-vk64.json', JSON.stringify({ ICD: { library_path: driver } }));
    writeFile(dir, 'igvk64.json', JSON.stringify({ ICD: { library_path: path.join(dir, 'absent.dll') } }));
    writeFile(dir, 'device_cache.json', JSON.stringify({ device: 0 }));

    assert.deepEqual(
      usableIcdFiles(dir).map((file) => path.basename(file)),
      ['nv-vk64.json'],
    );
  } finally {
    removeDir(dir);
  }
});

test('usableIcdFiles ignores unreadable ICDs and names no files when none install', () => {
  const dir = makeTempDir('flowbatch-icd-');
  try {
    writeFile(dir, 'nv-vk64.json', JSON.stringify({ ICD: { library_path: path.join(dir, 'nope.dll') } }));
    writeFile(dir, 'broken.json', '{ not json');
    assert.deepEqual(usableIcdFiles(dir), []);
  } finally {
    removeDir(dir);
  }
});

test('usableIcdFiles treats a missing tools directory as no overrides', () => {
  assert.deepEqual(usableIcdFiles(path.join(makeTempDir('flowbatch-icd-'), 'absent')), []);
});

test('the shipped ICD files are skipped when this machine lacks their driver', () => {
  const shipped = usableIcdFiles();
  for (const file of shipped) {
    const icd = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(fs.existsSync(icd.ICD.library_path), true, file);
  }
});
