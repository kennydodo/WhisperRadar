// Pure, deterministic schedule. Narration anchors are absolute scene milliseconds;
// changing the simulation rate must never move those anchors or exceed the window.
export function pipelineSchedule(stages, tasks, runMs, startMs) {
  const deadline = startMs + runMs;
  const make = (unit) => {
    let ready = Array.from({length: tasks}, (_, i) => startMs + i * unit * 0.04);
    const plan = [], barriers = [];
    for (const stage of stages) {
      const free = Array(stage.workers).fill(startMs);
      const barrier = Math.max(startMs, stage.at_ms ?? startMs,
        stage.waits_for_all ? Math.max(...ready) : startMs);
      const jobs = Array(tasks);
      const next = Array(tasks);
      const order = ready.map((at, i) => [at, i]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      for (const [arrive, i] of order) {
        let worker = 0;
        for (let w = 1; w < free.length; w++) if (free[w] < free[worker]) worker = w;
        const start = Math.max(arrive, barrier, free[worker]);
        const end = start + (stage.work ?? 1) * unit;
        jobs[i] = {worker, start, end, arrive};
        free[worker] = end;
        next[i] = end + unit * 0.18;
      }
      plan.push(jobs);
      barriers.push(barrier);
      ready = next;
    }
    return {plan, barriers, end: Math.max(...ready), travelMs: unit * 0.18};
  };
  if (make(0).end >= deadline) throw new Error('Pipeline anchors leave no time to finish');
  let lo = 0, hi = runMs;
  for (let i = 0; i < 50; i++) {
    const mid = (lo + hi) / 2;
    if (make(mid).end <= deadline) lo = mid;
    else hi = mid;
  }
  return make(lo);
}
