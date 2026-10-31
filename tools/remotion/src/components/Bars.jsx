import React from 'react';
import {Footer, Heading, Illustrative, Icon, fmtNum, lerp, prog, roleColor, useMs, usePal} from '../motion.jsx';

// Stated numbers side by side: each bar grows on the narrator's words and its value counts up.
// Optional: a limit line ("budget", "VRAM"), and "ratio_to" (1-based) to show "×N" against one bar.
const GROW = 900;

export const Bars = ({title, sub, items, unit = '', prefix = '', limit, ratio_to, illustrative, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const n = items.length;
  const max = Math.max(...items.map((x) => x.value), limit ? limit.value : 0) * 1.08 || 1;
  const RH = n <= 3 ? 150 : n <= 4 ? 128 : 104, BH = Math.round(RH * 0.46);
  const top = Math.round(250 + (640 - n * RH) / 2);
  const LX = 150, LW = 470, BX = LX + LW + 30, BW = 1770 - BX - 210;
  const ref = ratio_to ? items[ratio_to - 1] : null;
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    {items.map((it, i) => {
      const y = top + i * RH;
      const a = prog(ms, it.at_ms - 150, 450);
      const g = prog(ms, it.at_ms, GROW);
      const col = roleColor(pal, it.color, i);
      const w = Math.max(6, (it.value / max) * BW * g);
      const ratio = ref && it !== ref && ref.value > 0 ? it.value / ref.value : null;
      const rp = prog(ms, it.at_ms + GROW, 400);
      return <React.Fragment key={i}>
        <div style={{position: 'absolute', left: LX, top: y, width: LW, height: BH, display: 'flex', alignItems: 'center',
          justifyContent: 'flex-end', gap: 16, opacity: a, transform: `translateX(${(1 - a) * -16}px)`}}>
          {it.icon_paths ? <Icon paths={it.icon_paths} size={Math.round(BH * 0.9)} color={col} p={a} /> : null}
          <div style={{fontFamily: pal.sansF, fontSize: n <= 4 ? 36 : 30, color: pal.text, textAlign: 'right', lineHeight: 1.1}}>{it.label}</div>
        </div>
        <div style={{position: 'absolute', left: BX, top: y, width: w, height: BH, borderRadius: 10, opacity: a,
          background: `linear-gradient(90deg, ${col}cc, ${col})`, boxShadow: g < 1 && g > 0 ? `0 0 22px ${col}` : `0 0 8px ${col}55`}} />
        <div style={{position: 'absolute', left: BX + w + 18, top: y, height: BH, display: 'flex', alignItems: 'center',
          fontFamily: pal.monoF, fontSize: n <= 4 ? 36 : 30, fontWeight: 650, color: col, opacity: a, whiteSpace: 'nowrap'}}>
          {fmtNum(lerp(0, it.value, g), prefix, unit, it.decimals)}
          {ratio ? <span style={{marginLeft: 18, fontSize: 28, color: pal.accent2, opacity: rp}}>
            {ratio >= 1 ? fmtNum(ratio, '×', '', ratio >= 10 ? 0 : 1) : `1/${fmtNum(1 / ratio, '', '', 1 / ratio >= 10 ? 0 : 1)}`}</span> : null}
        </div>
        {it.note ? <div style={{position: 'absolute', left: BX, top: y + BH + 8, fontFamily: pal.noteF, fontSize: 24,
          color: pal.dim, opacity: rp}}>{it.note}</div> : null}
      </React.Fragment>;
    })}
    {limit ? (() => {
      const x = BX + (limit.value / max) * BW;
      const p = prog(ms, limit.at_ms, 600);
      return <>
        <div style={{position: 'absolute', left: x - 2, top: top - 40, width: 0, height: (n * RH + 30) * p,
          borderLeft: `4px dashed ${pal.bad}`}} />
        <div style={{position: 'absolute', left: x - 200, width: 400, textAlign: 'center', top: top - 84, opacity: p,
          fontFamily: pal.sansF, fontSize: 28, color: pal.bad}}>{limit.label} · {fmtNum(limit.value, prefix, unit)}</div>
      </>;
    })() : null}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
