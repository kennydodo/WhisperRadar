import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';

import { startServer, listJobs } from '../../src/server.js';
import { setLevel } from '../../src/lib/log.js';

setLevel('silent');

let server;
let base;

before(async () => {
  server = startServer({ port: 0, host: '127.0.0.1' });
  await new Promise((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  await new Promise((resolve) => server.close(resolve));
});

test('listJobs lists job files and skips configuration files', () => {
  const jobs = listJobs();
  const files = jobs.map((job) => job.file);
  assert.ok(files.includes('jobs.example.json'));
  assert.equal(files.some((file) => ['settings.json', 'selectors.json'].includes(file)), false);
  const example = jobs.find((job) => job.file === 'jobs.example.json');
  assert.equal(typeof example.label, 'string');
});

test('GET /api/status reports an idle server', async () => {
  const response = await fetch(`${base}/api/status`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.running, false);
});

test('GET /api/jobs serves the job list', async () => {
  const response = await fetch(`${base}/api/jobs`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.ok(Array.isArray(body.jobs));
  assert.ok(body.jobs.some((job) => job.file === 'jobs.example.json'));
});

test('GET /api/upscale describes the tiers', async () => {
  const response = await fetch(`${base}/api/upscale`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(body.tiers.map((tier) => tier.id), ['1k', '2k', '4k']);
});

test('GET /api/settings describes the profile and generation settings', async () => {
  const response = await fetch(`${base}/api/settings`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(typeof body.profileDir, 'string');
  assert.equal(typeof body.cooldownSeconds, 'number');
  assert.ok(Array.isArray(body.profiles));
});

test('POST /api/stop is a no-op when nothing is running', async () => {
  const response = await fetch(`${base}/api/stop`, { method: 'POST' });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).stopped, false);
});

test('GET / serves the UI shell', async () => {
  const response = await fetch(`${base}/`);
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /text\/html/);
});

test('an unknown route is a 404', async () => {
  const response = await fetch(`${base}/nope`);
  assert.equal(response.status, 404);
});

test('POST /api/settings rejects a profile outside the project', async () => {
  const response = await fetch(`${base}/api/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profileDir: '../evil' }),
  });
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /Unknown profile/);
});

test('POST /api/upscale rejects an invalid tier', async () => {
  const response = await fetch(`${base}/api/upscale`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tier: '9k' }),
  });
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /Upscale tier/);
});

test('POST /api/start rejects a missing job file', async () => {
  const response = await fetch(`${base}/api/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  assert.equal(response.status, 400);
  assert.match((await response.json()).error, /Job file not found/);
});
