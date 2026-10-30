import React from 'react';
import {Footer, Heading, Note, clamp01, prog, useMs, usePal} from '../motion.jsx';

// Two (or more) ways of producing the same text, side by side and on the same clock.
// "sequential": one word after another, left to right.
// "plan_then_fill": a rough outline of the whole thing first, then passes that fill the gaps.
function schedule(mode, n, runMs, beats, startMs) {
  const rel = (beats || []).map((b) => b - startMs);
  const at = Array(n).fill(0), pass = Array(n).fill(0);
  if (mode === 'sequential') {
    const a = rel.length ? Math.max(0, rel[0]) : 0, z = rel.length > 1 ? rel[1] : runMs;
    for (let i = 0; i < n; i++) { at[i] = a + ((i + 1) / n) * (z - a); pass[i] = i + 1; }
    return {at, pass, steps: n, end: z};
  }
  const strides = [4, 2, 1];
  const seen = new Set();
  let k = 0;
  for (const s of strides) {
    const idx = [];
    for (let i = 0; i < n; i += s) if (!seen.has(i)) idx.push(i);
    if (s === 1) for (let i = 0; i < n; i++) if (!seen.has(i) && !idx.includes(i)) idx.push(i);
    if (!idx.length) continue;
    k += 1;
    idx.forEach((i) => { seen.add(i); pass[i] = k; });
  }
  const per = Math.min(runMs * 0.34, runMs / k);
  const when = (p) => (rel[p - 1] != null ? Math.max(0, rel[p - 1]) : p * per);      // the narrator's words, else even
  for (let i = 0; i < n; i++) at[i] = when(pass[i]);
  return {at, pass, steps: k, end: when(k)};
}

export const Generation = ({title, sub, rows, start_ms, run_ms, note, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const rt = ms - start_ms;
  const passCol = [pal.accent, pal.accent2, pal.good, pal.accent];
  const out = rows.map((row, r) => {
    const sc = schedule(row.mode, row.words.length, run_ms, row.beats_ms, start_ms);
    const shown = row.words.filter((_, i) => rt >= sc.at[i]).length;
    const stepNow = row.mode === 'sequential' ? shown : Math.max(0, ...row.words.map((_, i) => (rt >= sc.at[i] ? sc.pass[i] : 0)));
    const finished = rt >= sc.end;
    const y = 270 + r * 330;
    const a = prog(ms, start_ms + r * 200, 600);
    const cells = row.words.map((w, i) => {
      const on = rt >= sc.at[i];
      const p = clamp01((rt - sc.at[i]) / 380);
      const col = row.mode === 'sequential' ? pal.accent : passCol[(sc.pass[i] - 1) % passCol.length];
      const x = 110 + (i % 7) * 240, yy = y + 92 + Math.floor(i / 7) * 82;
      return (
        <div key={i} style={{position: 'absolute', left: x, top: yy, width: 220, height: 62, borderRadius: 14,
          border: `2px ${on ? 'solid' : 'dashed'} ${on ? col : pal.line}`, background: on ? `${col}22` : 'transparent',
          display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: pal.monoF, fontSize: Math.min(26, Math.floor(200 / (Math.max(w.length, 1) * 0.62))),
          color: pal.text, boxShadow: on && p < 1 ? `0 0 ${30 * (1 - p)}px ${col}` : 'none'}}>
          <span style={{opacity: on ? p : 0, transform: `scale(${0.85 + 0.15 * p})`}}>{w}</span>
        </div>
      );
    });
    return (
      <div key={r} style={{opacity: a}}>
        <div style={{position: 'absolute', left: 110, top: y, fontFamily: pal.sansF, fontWeight: 650, fontSize: 30, color: pal.text}}>{row.label}</div>
        <div style={{position: 'absolute', right: 110, top: y, fontFamily: pal.monoF, fontSize: 28,
          color: finished ? pal.good : pal.dim}}>{finished ? '✓ done in ' : 'step '}{stepNow}{finished ? (stepNow === 1 ? ' step' : ' steps') : ''}</div>
        {cells}
      </div>
    );
  });
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    {out}
    {footer ? <Footer footer={footer} pal={pal} /> : note ? <Note x={110} y={950} start={start_ms + run_ms * 0.7} pal={pal}>{note}</Note> : null}
  </>;
};
