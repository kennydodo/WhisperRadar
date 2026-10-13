import test from 'node:test';
import assert from 'node:assert/strict';

import { decodeFlowVideoUrl, extractFlowVideoUrls } from '../../commands/video.js';
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
