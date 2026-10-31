import React from 'react';
import {Footer, Heading, Illustrative, clamp01, fmtNum, lerp, prog, roleColor, useMs, usePal} from '../motion.jsx';

// A value over time (growth, a price, a scaling curve): the line draws from left to right over the
// animation window, a dot rides its head showing the current value, and marks pop up on their words.
const PX0 = 230, PX1 = 1640, PY0 = 290, PY1 = 850;

const niceStep = (span) => {
  const raw = span / 4, mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
};

export const Line = ({title, sub, x, series, unit = '', prefix = '', marks = [], start_ms, run_ms, illustrative, zero = true, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const all = series.flatMap((s) => s.values);
  let lo = zero ? Math.min(0, ...all) : Math.min(...all), hi = Math.max(...all);
  if (hi === lo) hi = lo + 1;
  const step = niceStep(hi - lo);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  const n = x.length;
  const X = (i) => PX0 + (i / Math.max(1, n - 1)) * (PX1 - PX0);
  const Y = (v) => PY1 - ((v - lo) / (hi - lo)) * (PY1 - PY0);
  const axes = prog(ms, 0, 600);
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(v);
  const labelEvery = Math.ceil(n / 8);
  // where each finished line puts its value + name: spread so two lines ending close never overlap
  const ends = series.map((s, k) => ({k, y: Y(s.values[n - 1])})).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 70);
  const endY = Object.fromEntries(ends.map((e) => [e.k, e.y]));
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    <svg style={{position: 'absolute', left: 0, top: 0}} width={1920} height={1080}>
      {ticks.map((v, i) => <g key={i} opacity={axes}>
        <line x1={PX0} x2={PX1} y1={Y(v)} y2={Y(v)} stroke={pal.line} strokeWidth={i === 0 ? 3 : 1.5} strokeDasharray={i === 0 ? undefined : '4 8'} />
        <text x={PX0 - 20} y={Y(v) + 9} textAnchor="end" fontFamily={pal.monoF} fontSize={24} fill={pal.dim}>{fmtNum(v, prefix, '', step < 1 ? 2 : 0)}</text>
      </g>)}
      {x.map((lab, i) => i % labelEvery === 0 || i === n - 1 ? <text key={i} x={X(i)} y={PY1 + 44} textAnchor="middle" opacity={axes}
        fontFamily={pal.sansF} fontSize={26} fill={pal.dim}>{lab}</text> : null)}
      {unit ? <text x={PX0 - 20} y={PY1 + 44} textAnchor="end" fontFamily={pal.sansF} fontSize={24} fill={pal.dim} opacity={axes}>{unit}</text> : null}
      {series.map((s, k) => {
        const col = roleColor(pal, s.color, k);
        const sStart = s.at_ms != null ? s.at_ms : start_ms;
        const sRun = s.at_ms != null ? Math.max(1500, run_ms - (s.at_ms - start_ms)) : run_ms;
        const p = clamp01((ms - sStart) / sRun);
        const f = p * (n - 1), i = Math.min(n - 2, Math.floor(f)), r = f - i;
        const pts = s.values.slice(0, i + 1).map((v, j) => [X(j), Y(v)]);
        const head = n > 1 ? [lerp(X(i), X(i + 1), r), lerp(Y(s.values[i]), Y(s.values[i + 1]), r)] : [X(0), Y(s.values[0])];
        const val = n > 1 ? lerp(s.values[i], s.values[i + 1], r) : s.values[0];
        const d = [...pts, head].map((q, j) => `${j ? 'L' : 'M'} ${q[0].toFixed(1)} ${q[1].toFixed(1)}`).join(' ');
        const area = `${d} L ${head[0].toFixed(1)} ${Y(Math.max(lo, 0))} L ${X(0)} ${Y(Math.max(lo, 0))} Z`;
        if (ms < sStart) return null;
        const done = p >= 1;
        return <g key={k}>
          {series.length === 1 ? <path d={area} fill={col} opacity={0.12} /> : null}
          <path d={d} fill="none" stroke={col} strokeWidth={6} strokeLinejoin="round" strokeLinecap="round"
            style={{filter: `drop-shadow(0 0 8px ${col})`}} />
          <circle cx={head[0]} cy={head[1]} r={11} fill={col} />
          {done ? <>
            <text x={head[0] + 26} y={endY[k] - 6} fontFamily={pal.monoF} fontWeight={700} fontSize={32} fill={col}>
              {fmtNum(val, prefix, unit === '%' ? '%' : '', s.decimals)}</text>
            {s.label ? <text x={head[0] + 26} y={endY[k] + 26} fontFamily={pal.sansF} fontSize={26} fill={pal.text}>{s.label}</text> : null}
          </> : <text x={head[0]} y={head[1] - 24} textAnchor="middle" fontFamily={pal.monoF}
            fontWeight={700} fontSize={32} fill={col}>{fmtNum(val, prefix, unit === '%' ? '%' : '', s.decimals)}</text>}
        </g>;
      })}
      {marks.map((m, k) => {
        const p = prog(ms, m.at_ms, 500);
        const s = series[(m.series || 1) - 1] || series[0];
        const xi = Math.max(0, Math.min(n - 1, m.x - 1));
        return <g key={`m${k}`} opacity={p}>
          <line x1={X(xi)} x2={X(xi)} y1={PY0 - 10} y2={PY1} stroke={pal.accent2} strokeWidth={2} strokeDasharray="6 8" />
          <circle cx={X(xi)} cy={Y(s.values[xi])} r={14 + (1 - p) * 20} fill="none" stroke={pal.accent2} strokeWidth={4} />
        </g>;
      })}
    </svg>
    {marks.map((m, k) => {
      const p = prog(ms, m.at_ms, 500);
      const xi = Math.max(0, Math.min(n - 1, m.x - 1));
      const left = Math.min(1500, Math.max(150, X(xi) - 160));
      return <div key={k} style={{position: 'absolute', left, width: 320, top: PY0 - 112, textAlign: 'center', opacity: p,
        transform: `translateY(${(1 - p) * 10}px)`, fontFamily: pal.noteF, fontSize: 28, color: pal.accent2}}>{m.text}</div>;
    })}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
