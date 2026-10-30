import React from 'react';
import {COL, Text, fmt, useP} from './common.jsx';

// Bars of model size (params x bits / 8, computed here from the same numbers
// the Python checker validated) against a memory limit.
export const FitChart = ({title, limit, unit, bits, items, theme}) => {
  const vals = items.map((i) => i.params_b * bits / 8);
  const mx = Math.max(...vals, limit) * 1.1, base = 800, ph = 520, left = 240, bw = 240, gap = 120;
  const limY = base - ph * limit / mx;
  const lp = useP(600, 800), tp = useP(0, 500);
  return <>
    {title && <>
      <Text ms={0} x={134} y={64} size={72} color={theme.heading}>{title}</Text>
      <div style={{position: 'absolute', left: 134, top: 150, height: 5, background: COL.orange,
        width: Math.min(1650, 12 * title.length * 5) * tp}} />
    </>}
    <Text ms={0} x={left} y={200} size={40} color="grey">{bits}-bit weights, in {unit}</Text>
    <div style={{position: 'absolute', left: left - 20, top: limY, width: (left + items.length * (bw + gap) - 300) * lp,
      height: 0, borderTop: `5px dashed ${COL.orange}`}} />
    <Text ms={600} x={1560} y={limY - 56} size={40} color="orange">{fmt(limit)} {unit} card</Text>
    {items.map((it, k) => {
      const p = useP(it.start_ms, 1100), v = vals[k] * p, h = ph * v / mx, over = vals[k] > limit;
      const col = over ? 'red' : 'green', x = left + k * (bw + gap);
      return <React.Fragment key={k}>
        <div style={{position: 'absolute', left: x, top: base - h, width: bw, height: h, background: COL[col],
          opacity: 0.85, border: `5px solid ${COL[col]}`, borderRadius: '14px 14px 0 0', boxSizing: 'border-box'}} />
        <Text ms={it.start_ms} x={x} y={base + 20} size={56} color={theme.text}>{it.label}</Text>
        <Text ms={it.start_ms + 700} x={x} y={base - h - 62} size={50} color={col}>{fmt(v)} {unit}</Text>
        {over && <Text ms={it.start_ms + 1100} x={x - 10} y={base + 85} size={36} color="red">does not fit</Text>}
      </React.Fragment>;
    })}
  </>;
};
