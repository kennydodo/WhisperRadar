import test from 'node:test';
import assert from 'node:assert/strict';
import { FlowDriver } from '../../src/flow/driver.js';

function fixture({ fill = (text) => text, type = (text) => text } = {}) {
  let text = '';
  let typed = 0;
  let submissions = 0;
  const driver = Object.create(FlowDriver.prototype);
  const locator = {
    click: async () => {},
    fill: async (value) => { text = fill(value); },
    evaluate: async (fn) => fn({ innerText: text }),
  };
  driver.find = async (key) => ({ locator: key === 'generateButton'
    ? { click: async () => { submissions += 1; } } : locator });
  driver.page = { keyboard: {
    press: async (key) => { if (key === 'Delete') text = ''; },
    type: async (value) => { typed += 1; text += type(value); },
  } };
  driver.closeAssetLibrary = async () => {};
  driver.dismissOverlays = async () => {};
  driver.markStaleAlerts = async () => {};
  return { driver, mutate: (value) => { text = value; },
    state: () => ({ text, typed, submissions }) };
}

const prompt = 'Master style. '.repeat(150) + 'Martin and Chloe unfold the picnic blanket.';

test('full master and scene survive insertion and submission', async () => {
  const f = fixture();
  await f.driver.setPrompt(prompt);
  await f.driver.generate();
  assert.deepEqual(f.state(), { text: prompt, typed: 0, submissions: 1 });
});

test('silent truncation is replaced with a complete keyboard insertion', async () => {
  const f = fixture({ fill: (text) => text.slice(0, 1500) });
  await f.driver.setPrompt(prompt);
  assert.equal(f.state().text, prompt);
  assert.equal(f.state().typed, 1);
});

test('persistent truncation refuses submission', async () => {
  const f = fixture({ fill: (text) => text.slice(0, 1500), type: (text) => text.slice(0, 1500) });
  await assert.rejects(f.driver.setPrompt(prompt), /Generation was not submitted/);
  assert.equal(f.state().submissions, 0);
});

test('reference attachment that changes the prompt prevents Generate', async () => {
  const f = fixture();
  await f.driver.setPrompt(prompt);
  f.mutate(prompt.slice(0, 1500));
  await assert.rejects(f.driver.generate(), /incomplete or changed/);
  assert.equal(f.state().submissions, 0);
});

test('same-length corruption is detected but editor whitespace is accepted', async () => {
  const f = fixture();
  await f.driver.setPrompt('Master style. Scene action.');
  f.mutate('Master style. Other action.');
  await assert.rejects(f.driver.generate(), /incomplete or changed/);
  f.mutate('Master style.\nScene action.');
  await f.driver.generate();
  assert.equal(f.state().submissions, 1);
});
