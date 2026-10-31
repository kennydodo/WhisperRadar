import React from 'react';
import {Footer, Heading, Illustrative, clamp01, fmtNum, prog, roleColor, useMs, usePal} from '../motion.jsx';

// One whole and its parts (a budget, a portfolio, a memory card, an hour): a ring whose segments
// sweep in on the narrator's words, with a legend of values and shares. "total" larger than the
// parts leaves the rest as an empty, labelled gap.
const CX = 620, CY = 590, R = 250, SW = 92, SWEEP = 900;

const arc = (a0, a1) => {
  const p = (a) => [CX + R * Math.cos(a), CY + R * Math.sin(a)];
  const [x0, y0] = p(a0), [x1, y1] = p(a1);
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${R} ${R} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
};

export const Split = ({title, sub, items, total, total_label, rest_label, unit = '', prefix = '', center, illustrative, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const sum = items.reduce((a, x) => a + x.value, 0);
  const whole = Math.max(total || 0, sum) || 1;
  const ring = prog(ms, 0, 600);
  let acc = -Math.PI / 2;
  const segs = items.map((it, i) => {
    const span = (it.value / whole) * 2 * Math.PI;
    const a0 = acc; acc += span;
    const p = prog(ms, it.at_ms, SWEEP);
    return {it, i, a0, a1: a0 + Math.max(0.0001, span * p) - (p > 0.99 && items.length > 1 ? 0.02 : 0), p, col: roleColor(pal, it.color, i)};
  });
  const shown = items.reduce((a, it) => a + it.value * clamp01((ms - it.at_ms) / SWEEP), 0);
  const rest = whole - sum;
  const restP = rest > 0 ? prog(ms, Math.max(...items.map((x) => x.at_ms)) + SWEEP, 500) : 0;
  const rows = items.length + (rest > 0 && rest_label ? 1 : 0);
  const RH = Math.min(120, 560 / rows);
  const LY = Math.round(CY - (rows * RH) / 2);
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    <svg style={{position: 'absolute', left: 0, top: 0}} width={1920} height={1080}>
      <circle cx={CX} cy={CY} r={R} fill="none" stroke={pal.line} strokeWidth={SW} opacity={0.6 * ring} />
      {segs.map((s) => s.p > 0 ? <path key={s.i} d={arc(s.a0, s.a1)} fill="none" stroke={s.col} strokeWidth={SW}
        style={{filter: s.p < 1 ? `drop-shadow(0 0 10px ${s.col})` : 'none'}} /> : null)}
    </svg>
    <div style={{position: 'absolute', left: CX - 170, width: 340, top: CY - 70, textAlign: 'center', opacity: ring}}>
      <div style={{fontFamily: pal.monoF, fontWeight: 700, fontSize: 54, color: pal.text}}>{fmtNum(total ? whole : shown, prefix, unit)}</div>
      <div style={{fontFamily: pal.sansF, fontSize: 28, color: pal.dim, marginTop: 6}}>{center || total_label || ''}</div>
    </div>
    {segs.map((s) => <div key={s.i} style={{position: 'absolute', left: 1040, top: LY + s.i * RH, width: 760, opacity: prog(ms, s.it.at_ms - 100, 450),
      display: 'flex', alignItems: 'center', gap: 22}}>
      <div style={{width: 34, height: 34, borderRadius: 8, background: s.col, flex: 'none'}} />
      <div style={{fontFamily: pal.sansF, fontSize: 36, color: pal.text, flex: 1, lineHeight: 1.1}}>{s.it.label}</div>
      <div style={{fontFamily: pal.monoF, fontSize: 32, color: s.col, textAlign: 'right'}}>
        {fmtNum(s.it.value, prefix, unit, s.it.decimals)}<span style={{color: pal.dim, fontSize: 26, marginLeft: 14}}>{Math.round((100 * s.it.value) / whole)}%</span></div>
    </div>)}
    {rest > 0 && rest_label ? <div style={{position: 'absolute', left: 1040, top: LY + items.length * RH, width: 760, opacity: restP,
      display: 'flex', alignItems: 'center', gap: 22}}>
      <div style={{width: 30, height: 30, borderRadius: 8, border: `3px dashed ${pal.dim}`, flex: 'none'}} />
      <div style={{fontFamily: pal.sansF, fontSize: 36, color: pal.dim, flex: 1}}>{rest_label}</div>
      <div style={{fontFamily: pal.monoF, fontSize: 32, color: pal.dim}}>{fmtNum(rest, prefix, unit)}<span style={{fontSize: 26, marginLeft: 14}}>{Math.round((100 * rest) / whole)}%</span></div>
    </div> : null}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
