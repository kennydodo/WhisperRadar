import test from 'node:test';
import assert from 'node:assert/strict';

import { VideoTrace, diagnose } from '../../src/lib/videoTrace.js';

function trace(build) {
  let clock = 0;
  const t = new VideoTrace({ now: () => clock });
  build(t, (s) => {
    clock = s * 1000;
  });
  return t;
}

test('no video ever exposed', () => {
  const t = trace((tr, at) => {
    for (let s = 4; s <= 40; s += 4) {
      at(s);
      tr.note('poll', { candidates: 0, fresh: 0, videos: 0, tile: 'Generating 22%' });
    }
  });
  assert.match(t.diagnose().verdict, /NEVER EXPOSED/);
});

test('download refused after the URL was seen', () => {
  const t = trace((tr, at) => {
    at(30);
    tr.note('poll', { candidates: 1, fresh: 1 });
    tr.note('fetch', { id: 'abc', ok: false, status: 403 });
  });
  assert.match(t.diagnose().verdict, /REFUSED/);
});

test('only known clips visible', () => {
  const t = trace((tr, at) => {
    at(8);
    tr.note('poll', { candidates: 2, fresh: 0, knownOrSaved: 2 });
  });
  assert.match(t.diagnose().verdict, /KNOWN\/SAVED/);
});

test('alert wins, and a saved clip wins over everything', () => {
  assert.match(diagnose([{ t: 5, kind: 'alert', text: 'Failed' }]).verdict, /REFUSED \/ FAILED/);
  assert.match(
    diagnose([{ t: 5, kind: 'alert', text: 'x' }, { t: 9, kind: 'fetch', ok: true }]).verdict,
    /SAVED/,
  );
});

test('status changes are noted once', () => {
  const t = trace((tr) => {
    tr.status('mediaGenerationStatus', 'PENDING');
    tr.status('mediaGenerationStatus', 'PENDING');
    tr.status('mediaGenerationStatus', 'SUCCESS');
  });
  assert.equal(t.events.filter((e) => e.kind === 'status').length, 2);
});
