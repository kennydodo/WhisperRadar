import test from 'node:test';
import assert from 'node:assert/strict';

import { contentHash, decodeFlowVideoUrl, extractFlowVideoUrls, mediaId, pickClipByPrompt } from '../../commands/video.js';
import { setLevel } from '../../src/lib/log.js';

setLevel('silent');

const UUID = '274a87b1-83bf-4f30-852b-0abea3930791';
const BASE = `https://flow-content.google/video/${UUID}`;

test('decodeFlowVideoUrl leaves a plain signed URL untouched', () => {
  const url = `${BASE}?Expires=1790000000&Signature=abc123`;
  assert.equal(decodeFlowVideoUrl(url), url);
});

test('decodeFlowVideoUrl undoes lowercase and uppercase \u003d/\u0026 escapes', () => {
  assert.equal(
    decodeFlowVideoUrl(`${BASE}?Expires\\u003d1790000000\\u0026Signature\\u003dabc123`),
    `${BASE}?Expires=1790000000&Signature=abc123`,
  );
  assert.equal(
    decodeFlowVideoUrl(`${BASE}?Expires\\u003D1790000000\\u0026Signature\\u003Dabc123`),
    `${BASE}?Expires=1790000000&Signature=abc123`,
  );
});

test('decodeFlowVideoUrl undoes double-escaped and backslash-escaped slashes', () => {
  assert.equal(
    decodeFlowVideoUrl(`${BASE}?Expires\\\\u003d1790000000`),
    `${BASE}?Expires=1790000000`,
  );
  assert.equal(decodeFlowVideoUrl(`${BASE}\\/?Expires\\u003d1`), `${BASE}/?Expires=1`);
});

test('extractFlowVideoUrls keeps the whole signed query from raw JSON text', () => {
  const body = `{"media":{"videoUrl":"${BASE}?Expires\\u003D1790000000\\u0026Signature\\u003Dabc123\\u0026Key\\u003Dxyz","state":"DONE"}}`;
  const urls = extractFlowVideoUrls(body);
  assert.equal(urls.length, 1);
  assert.equal(urls[0], `${BASE}?Expires=1790000000&Signature=abc123&Key=xyz`);
  assert.ok(urls[0].includes('Expires='), 'signed query must survive (regression: truncated at ?Expires)');
});

test('extractFlowVideoUrls finds nothing in unrelated text', () => {
  assert.deepEqual(extractFlowVideoUrls('{"state":"RUNNING","image":"https://flow-content.google/image/abc"}'), []);
});

test('mediaId normalizes googlevideo mirrors of the same video to one identity', () => {
  const a = 'https://rr1---sn-4g5ednz7.googlevideo.com/videoplayback?expire=1&ei=x&id=da2e2056a4d94cb9&itag=22&sig=AAA';
  const b = 'https://rr3---sn-aj4g55-52.googlevideo.com/videoplayback?expire=2&ei=y&id=da2e2056a4d94cb9&itag=22&sig=BBB';
  const c = 'https://rr5---sn-4g5lznly.googlevideo.com/videoplayback?expire=3&ei=z&id=da2e2056a4d94cb9&itag=22&sig=CCC';
  assert.equal(mediaId(a), 'gv:da2e2056a4d94cb9');
  assert.equal(mediaId(a), mediaId(b));
  assert.equal(mediaId(b), mediaId(c));
  // a different video keeps a different identity
  const other = 'https://rr3---sn-aj4g55-52.googlevideo.com/videoplayback?expire=1&ei=y&id=609dcac093ef49f8&itag=22';
  assert.notEqual(mediaId(other), mediaId(a));
  // flow-content uuids keep their path identity
  assert.equal(mediaId(`${BASE}?Expires=1`), UUID);
  // unknown URLs fall back to the full string
  assert.equal(mediaId('https://example.com/x.mp4'), 'https://example.com/x.mp4');
});

test('contentHash links one clip across identities', () => {
  const a = Buffer.from('same video bytes');
  assert.equal(contentHash(a), contentHash(Buffer.from('same video bytes')));
  assert.notEqual(contentHash(a), contentHash(Buffer.from('other')));
  assert.match(contentHash(a), /^sha1:[0-9a-f]{40}$/);
});

test('pickClipByPrompt takes the clip whose tile prompt matches, never a guess', () => {
  const fresh = [
    { id: 'a', url: 'u1' },
    { id: 'b', url: 'u2' },
  ];
  const tiles = [
    { id: 'a', prompt: 'Slow push in on the lighthouse at dusk' },
    { id: 'b', prompt: 'Camera drifts left across the harbour at night' },
  ];
  assert.equal(pickClipByPrompt(fresh, tiles, 'slow push in on the lighthouse at dusk, gentle')?.id, 'a');
  assert.equal(pickClipByPrompt(fresh, tiles, 'something else entirely, unrelated words'), null);
  // two tiles matching the same prompt is ambiguous -> null
  const dup = [...tiles, { id: 'a', prompt: 'Slow push in on the lighthouse at dusk' }];
  assert.equal(pickClipByPrompt(fresh, dup, 'Slow push in on the lighthouse at dusk'), null);
  // a tile for a clip that is not fresh is ignored
  assert.equal(pickClipByPrompt([fresh[1]], tiles, 'Slow push in on the lighthouse at dusk'), null);
});
