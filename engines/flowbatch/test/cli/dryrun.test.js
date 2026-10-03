import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

import { ROOT } from '../../src/lib/paths.js';

const jobFiles = fs
  .readdirSync(path.join(ROOT, 'config'))
  .filter((file) => /^jobs.*\.json$/.test(file))
  .sort();

test('there is at least one example job to check', () => {
  assert.ok(jobFiles.length > 0, 'no config/jobs*.json files found');
});

test('every example job validates in a dry run', () => {
  for (const file of jobFiles) {
    const result = spawnSync(
      process.execPath,
      ['src/cli.js', 'generate', '--job', path.join('config', file), '--dry-run', '--no-color'],
      { cwd: ROOT, encoding: 'utf8', timeout: 60000 },
    );
    assert.equal(result.status, 0, `${file} exited ${result.status}: ${result.stderr}`);
    assert.match(result.stdout, /Dry run complete/, `${file} did not report a completed dry run`);
  }
});
