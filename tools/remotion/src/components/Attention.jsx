import React from 'react';
import {Footer, Heading, Illustrative, mix, prog, useMs, usePal} from '../motion.jsx';

// Which words does the model look at while it reads one word? Arcs from the focus word to the words
// it attends to; a thicker arc is a bigger weight. Weights are illustrative unless the scene says so.
const WH = 150, GAP = 22, CY = 800;

export const Attention = ({title, sub, words, focus, links, illustrative = true, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const n = words.length;
  const raw = words.map((w) => Math.max(90, w.length * 30 + 44));
  const k = Math.min(1, 1700 / (raw.reduce((a, b) => a + b, 0) + (n - 1) * GAP));   // long sentences shrink to fit
  const widths = raw.map((w) => w * k);
  const total = widths.reduce((a, b) => a + b, 0) + (n - 1) * GAP * k;
  const x0 = (1920 - total) / 2;
  const xs = []; let acc = x0;
  widths.forEach((w) => { xs.push(acc + w / 2); acc += w + GAP * k; });
  const a = prog(ms, 0, 600);
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    <svg style={{position: 'absolute', left: 0, top: 0}} width={1920} height={1080}>
      {links.map((l, k) => {
        const p = prog(ms, l.at_ms, 700);
        const x1 = xs[focus], x2 = xs[l.to], top = CY - 44;
        const h = Math.min(430, 120 + Math.abs(x2 - x1) * 0.42);
        const d = `M ${x1} ${top} C ${x1} ${top - h}, ${x2} ${top - h}, ${x2} ${top}`;
        const w = 3 + l.weight * 16;
        return <g key={k} opacity={p > 0 ? 1 : 0}>
          <path d={d} fill="none" stroke={pal.accent2} strokeWidth={w} strokeLinecap="round" pathLength={1} strokeDasharray={1}
            strokeDashoffset={1 - p} opacity={0.35 + 0.65 * l.weight} style={{filter: `drop-shadow(0 0 8px ${pal.accent2}66)`}} />
          <text x={(x1 + x2) / 2} y={top - h * 0.75 - 14} textAnchor="middle" fontFamily={pal.monoF} fontSize={30} fill={pal.accent2}
            opacity={p}>{Math.round(l.weight * 100)}%</text>
        </g>;
      })}
    </svg>
    {words.map((w, i) => {
      const isFocus = i === focus;
      const lk = links.find((l) => l.to === i);
      const glow = lk ? prog(ms, lk.at_ms + 300, 500) * lk.weight : 0;
      return <div key={i} style={{position: 'absolute', left: xs[i] - widths[i] / 2, top: CY - 44, width: widths[i], height: 88, display: 'flex',
        alignItems: 'center', justifyContent: 'center', opacity: a, borderRadius: 14, fontFamily: pal.sansF, fontSize: Math.round(44 * k),
        color: isFocus ? pal.bg : pal.text, background: isFocus ? pal.accent2 : mix(pal.bg, pal.accent2, 0.1 + 0.4 * glow),
        border: `2px solid ${isFocus ? pal.accent2 : mix(pal.line, pal.accent2, glow)}`,
        boxShadow: glow ? `0 0 ${Math.round(40 * glow)}px ${pal.accent2}88` : 'none'}}>{w}</div>;
    })}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
