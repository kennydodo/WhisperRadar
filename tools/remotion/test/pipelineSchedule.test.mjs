import {test} from 'node:test';
import assert from 'node:assert/strict';
import {pipelineSchedule} from '../src/pipelineSchedule.mjs';

test('late narration anchor stays exact and every task finishes inside the window', () => {
  const stages = [{workers: 8, work: 1}, {workers: 8, work: 1, waits_for_all: true, at_ms: 16500}];
  const s = pipelineSchedule(stages, 8, 18000, 400);
  assert.ok(s.plan[1].every((p) => p.start >= 16500));
  assert.ok(s.end <= 18400.001);
  assert.ok(s.plan[1].every((p) => p.start >= Math.max(...s.plan[0].map((a) => a.end))));
});

test('one to eight workers, eight stages and queued tasks keep their dependencies', () => {
  for (const workers of [1, 4, 8]) {
    const stages = Array.from({length: 8}, (_, i) => ({workers, waits_for_all: true, at_ms: i * 1000}));
    const s = pipelineSchedule(stages, 24, 12000, 0);
    assert.equal(s.plan.length, 8);
    assert.ok(s.end <= 12000.001);
    for (let i = 1; i < 8; i++) {
      const previous = Math.max(...s.plan[i - 1].map((p) => p.end));
      assert.ok(s.plan[i].every((p) => p.start >= previous));
    }
  }
});

test('an impossible anchor is refused rather than showing an unfinished process', () => {
  assert.throws(() => pipelineSchedule([{workers: 1}, {workers: 1, at_ms: 13000}], 4, 12000, 0));
});
