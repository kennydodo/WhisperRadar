import React from 'react';
import {Heading, Note, clamp01, lerp, prog, useMs, usePal} from '../motion.jsx';

// Tasks flow through stages. Each stage has N workers; a stage can wait for the whole
// previous stage to finish (a barrier). The run is simulated here, deterministically,
// so the picture shows what the numbers cause: eight workers busy, a later stage blocked.
const GAP = 130, TOP = 290, WR = 36, CW = 92;

function simulate(stages, tasks, runMs) {
  const unit = 1, travel = 0.18;
  const ready = Array.from({length: tasks}, (_, i) => i * 0.04);
  const plan = stages.map(() => []);
  const barriers = stages.map(() => 0);
  let prevEnd = 0;
  for (let s = 0; s < stages.length; s++) {
    const st = stages[s];
    const W = Math.max(1, st.workers);
    const free = Array(W).fill(0);
    const dur = (st.work || 1) * unit;
    const barrier = st.waits_for_all && s > 0 ? Math.max(...ready) : 0;
    const order = ready.map((r, i) => [r, i]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const ends = Array(tasks).fill(0);
    for (const [r, i] of order) {
      let w = 0;
      for (let k = 1; k < W; k++) if (free[k] < free[w]) w = k;
      const start = Math.max(r, free[w], barrier);
      free[w] = start + dur;
      plan[s][i] = {worker: w, start, end: start + dur, arrive: r};
      ends[i] = start + dur + travel;
    }
    barriers[s] = barrier;
    for (let i = 0; i < tasks; i++) ready[i] = ends[i];
    prevEnd = Math.max(...ends);
  }
  const k = runMs / prevEnd;
  return {plan, k, total: prevEnd, barriers};
}

const Chip = ({x, y, color, label, glow, scale = 1, opacity = 1, font}) => (
  <div style={{position: 'absolute', left: x - 24, top: y - 24, width: 48, height: 48, borderRadius: 12,
    background: color, opacity, transform: `scale(${scale})`, boxShadow: glow ? `0 0 18px ${color}` : 'none',
    display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: font, fontSize: 22,
    fontWeight: 700, color: '#0b0e13'}}>{label}</div>
);

export const Pipeline = ({title, sub, stages, tasks, start_ms, run_ms, note, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const sim = React.useMemo(() => simulate(stages.map((s) => ({...s})), tasks, run_ms),
    [JSON.stringify(stages), tasks, run_ms]);
  const t = Math.max(0, ms - start_ms) / sim.k;                    // simulation time
  const n = stages.length;
  const COLW = Math.min(620, Math.floor((1700 - (n - 1) * GAP) / n));
  const x0 = (1920 - (n * COLW + (n - 1) * GAP)) / 2;
  const colX = (s) => x0 + s * (COLW + GAP);
  const wpos = (s, w) => {
    const per = Math.min(stages[s].workers, Math.max(1, Math.floor((COLW - 40) / CW)), 4);
    const col = w % per, row = Math.floor(w / per);
    return [colX(s) + COLW / 2 - ((per - 1) * CW) / 2 + col * CW, TOP + 190 + row * 100];
  };
  const maxRows = Math.max(...stages.map((x) => Math.ceil(x.workers / 4)));
  const trayY = TOP + 190 + maxRows * 100 + 70;
  const done = [];
  const boxes = stages.map((st, s) => {
    const rows = Math.ceil(st.workers / Math.min(4, Math.max(1, Math.floor((COLW - 40) / CW))));
    const blocked = st.waits_for_all && s > 0 && t < sim.barriers[s];
    const release = st.waits_for_all && s > 0 ? clamp01((t - sim.barriers[s]) / 0.25) : 1;
    const appear = prog(ms, start_ms - 200 + s * 150, 500);
    const busyCount = sim.plan[s].filter((p) => t >= p.start && t < p.end).length;
    return (
      <div key={s} style={{position: 'absolute', left: colX(s), top: TOP, width: COLW, height: 150 + rows * 100 + 20,
        borderRadius: 20, background: pal.panel, border: `2px ${blocked ? 'dashed' : 'solid'} ${blocked ? pal.bad : pal.line}`,
        opacity: appear * (blocked ? 0.62 : 1), transform: `translateY(${(1 - appear) * 20}px)`,
        boxShadow: busyCount ? `0 0 ${14 + busyCount * 3}px ${pal.accent}55` : 'none'}}>
        <div style={{position: 'absolute', left: 26, top: 20, fontFamily: pal.sansF, fontWeight: 650, fontSize: 34, color: pal.text}}>{st.label}</div>
        <div style={{position: 'absolute', left: 26, top: 66, fontFamily: pal.monoF, fontSize: 24,
          color: blocked ? pal.bad : pal.accent}}>{blocked ? '🔒 waiting' : `${st.workers} workers · ${busyCount} busy`}</div>
      </div>
    );
  });
  const workers = stages.flatMap((st, s) => Array.from({length: st.workers}, (_, w) => {
    const busy = sim.plan[s].find((p) => p.worker === w && t >= p.start && t < p.end);
    const [x, y] = wpos(s, w);
    const pr = busy ? clamp01((t - busy.start) / (busy.end - busy.start)) : 0;
    const blocked = st.waits_for_all && s > 0 && t < sim.barriers[s];
    const a = prog(ms, start_ms + s * 150, 500);
    const R = WR, C = 2 * Math.PI * (R - 3);
    return (
      <svg key={`${s}-${w}`} style={{position: 'absolute', left: x - R, top: y - R, opacity: a}} width={R * 2} height={R * 2}>
        <circle cx={R} cy={R} r={R - 3} fill={busy ? `${pal.accent}22` : 'transparent'} stroke={blocked ? pal.bad : pal.line} strokeWidth={4}
          strokeDasharray={blocked ? '4 6' : undefined} />
        {busy ? <circle cx={R} cy={R} r={R - 3} fill="none" stroke={pal.accent} strokeWidth={5} strokeLinecap="round"
          strokeDasharray={C} strokeDashoffset={C * (1 - pr)} transform={`rotate(-90 ${R} ${R})`}
          style={{filter: `drop-shadow(0 0 6px ${pal.accent})`}} /> : null}
        <circle cx={R} cy={R} r={5} fill={busy ? pal.accent : blocked ? pal.bad : pal.dim} />
      </svg>
    );
  }));
  const chips = [];
  const waiting = stages.map(() => 0);
  for (let i = 0; i < tasks; i++) {
    const colr = pal[['accent', 'accent2', 'good'][i % 3]];
    let placed = false;
    for (let s = 0; s < n && !placed; s++) {
      const p = sim.plan[s][i];
      if (t < p.start) {                                           // queued before stage s
        if (s > 0 && t < sim.plan[s - 1][i].end) continue;
        const slot = waiting[s]++;
        const prev = s > 0 ? sim.plan[s - 1][i] : null;
        const [px, py] = prev ? wpos(s - 1, prev.worker) : [colX(0) + 40, trayY - 120];
        const perRow = Math.max(4, Math.floor((COLW - 40) / 56)); const tx = colX(s) + 40 + (slot % perRow) * 56, ty = trayY + Math.floor(slot / perRow) * 58;
        const mv = prev ? clamp01((t - prev.end) / 0.18) : 1;
        const e = mv * mv * (3 - 2 * mv);
        if (slot < 24) chips.push(<Chip font={pal.monoF} key={i} x={lerp(px, tx, e)} y={lerp(py, ty, e)} color={colr} label={i + 1} />);
        placed = true;
      } else if (t < p.end) {
        const [x, y] = wpos(s, p.worker);
        chips.push(<Chip font={pal.monoF} key={i} x={x} y={y} color={colr} label={i + 1} glow scale={0.8} />);
        placed = true;
      }
    }
    if (!placed) done.push(i);
  }
  const fin = done.length;
  const ao = prog(ms, start_ms, 600);
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    {stages.slice(1).map((_, k) => {
      const ax = colX(k) + COLW + 14, aw = GAP - 28, ay = TOP + 90 + 0;
      const flow = (ms / 600) % 1;
      return <svg key={k} style={{position: 'absolute', left: ax, top: ay - 20}} width={aw} height={40}>
        <line x1={0} y1={20} x2={aw - 12} y2={20} stroke={pal.line} strokeWidth={4} />
        <path d={`M ${aw - 18} 8 L ${aw} 20 L ${aw - 18} 32`} fill="none" stroke={pal.dim} strokeWidth={4} strokeLinecap="round" />
        <circle cx={flow * (aw - 12)} cy={20} r={5} fill={pal.accent} />
      </svg>;
    })}
    {boxes}{workers}{chips}
    <div style={{position: 'absolute', left: 110, right: 110, top: 880, display: 'flex', alignItems: 'center', gap: 24, opacity: ao}}>
      <div style={{fontFamily: pal.sansF, fontSize: 26, color: pal.dim, width: 120}}>Finished</div>
      <div style={{flex: 1, height: 18, borderRadius: 9, background: pal.panel, border: `1px solid ${pal.line}`, overflow: 'hidden'}}>
        <div style={{width: `${(fin / tasks) * 100}%`, height: '100%', background: `linear-gradient(90deg, ${pal.accent}, ${pal.good})`,
          boxShadow: `0 0 16px ${pal.good}`}} />
      </div>
      <div style={{fontFamily: pal.monoF, fontSize: 30, color: fin === tasks ? pal.good : pal.text, width: 120, textAlign: 'right'}}>{fin}/{tasks}</div>
    </div>
    {note ? <Note x={110} y={965} start={start_ms + run_ms * 0.5} pal={pal}>{note}</Note> : null}
  </>;
};
