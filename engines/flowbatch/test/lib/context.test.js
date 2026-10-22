import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { buildContext } from '../../src/lib/context.js';
import { loadUpscaleSettings } from '../../src/upscale/index.js';

test('runtime and profile overrides isolate account data without changing config', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'flow-account-'));
  const previous = process.env.FLOWBATCH_UPSCALE_TIER;
  try {
    const profile = path.join(temp, 'profile');
    const runtime = path.join(temp, 'runtime');
    const context = buildContext({ profile, 'runtime-dir': runtime, 'upscale-tier': '4k' });
    assert.equal(context.dirs.profileDir, profile);
    for (const key of ['stateDir', 'debugDir', 'discoverDir', 'downloadsDir', 'outputDir', 'refsDir']) {
      assert.equal(context.dirs[key], path.join(runtime, key));
      assert.equal(fs.existsSync(context.dirs[key]), true);
    }
    assert.equal(loadUpscaleSettings().tier, '4k');
    buildContext({ profile, 'runtime-dir': runtime, 'upscale-tier': 'off' });
    assert.equal(loadUpscaleSettings().tier, 'off');
    assert.throws(() => buildContext({ 'upscale-tier': 'wrong' }), /Invalid upscale tier/);
  } finally {
    if (previous === undefined) delete process.env.FLOWBATCH_UPSCALE_TIER;
    else process.env.FLOWBATCH_UPSCALE_TIER = previous;
    fs.rmSync(temp, { recursive: true, force: true });
  }
});
