import React from 'react';
import {Footer, Heading, Illustrative, prog, roleColor, useMs, usePal} from '../motion.jsx';

// A board of cells (a unit chart, a matrix, a puzzle board, "7 in 10 people"). The empty board
// is drawn first; each step fills the next cells on its words, in reading order, with its colour.
// A step may carry "symbols" (short text per filled cell: digits, letters, tokens).
export const Grid = ({title, sub, rows, cols, steps, start_ms, show_share, illustrative, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const total = rows * cols;
  const AW = 960, AH = 620, AX = 150, AY = 270;
  const gap = total > 64 ? 6 : 10;
  const size = Math.floor(Math.min((AW - (cols - 1) * gap) / cols, (AH - (rows - 1) * gap) / rows));
  const gw = cols * size + (cols - 1) * gap, gh = rows * size + (rows - 1) * gap;
  const gx = AX + (AW - gw) / 2, gy = AY + (AH - gh) / 2;
  const owner = new Array(total).fill(null);       // cell -> {step, k (index within the step)}
  let next = 0;
  steps.forEach((s, si) => {
    for (let k = 0; k < s.cells && next < total; k++, next++) owner[next] = {si, k};
  });
  const board = prog(ms, Math.max(0, start_ms - 400), 600);
  const LX = 1210;
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    {owner.map((o, c) => {
      const r = Math.floor(c / cols), q = c % cols;
      let fill = 0, col = pal.line, sym = null;
      if (o) {
        const s = steps[o.si];
        const stagger = Math.min(1400, s.cells * 35) / Math.max(1, s.cells);
        fill = prog(ms, s.at_ms + o.k * stagger, 350);
        col = roleColor(pal, s.color, o.si);
        sym = s.symbols ? s.symbols[o.k % s.symbols.length] : null;
      }
      return <div key={c} style={{position: 'absolute', left: gx + q * (size + gap), top: gy + r * (size + gap), width: size, height: size,
        borderRadius: Math.max(4, size * 0.16), border: `2px solid ${fill > 0 ? col : pal.line}`, opacity: board,
        background: fill > 0 ? col : 'transparent', transform: `scale(${fill > 0 ? 0.7 + 0.3 * fill : 1})`,
        boxShadow: fill > 0 && fill < 1 ? `0 0 16px ${col}` : 'none',
        display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: pal.monoF, fontWeight: 700,
        fontSize: Math.round(size * 0.5), color: '#0b0e13'}}>{fill > 0.5 && sym != null ? sym : ''}</div>;
    })}
    {steps.map((s, si) => {
      const p = prog(ms, s.at_ms, 500);
      const col = roleColor(pal, s.color, si);
      const y = Math.round(AY + 60 + si * Math.min(130, 520 / steps.length));
      return <div key={si} style={{position: 'absolute', left: LX, top: y, width: 600, opacity: p,
        transform: `translateX(${(1 - p) * 20}px)`, display: 'flex', alignItems: 'center', gap: 20}}>
        <div style={{width: 34, height: 34, borderRadius: 8, background: col, flex: 'none'}} />
        <div>
          <div style={{fontFamily: pal.sansF, fontSize: 36, color: pal.text, lineHeight: 1.1}}>{s.label}</div>
          <div style={{fontFamily: pal.monoF, fontSize: 26, color: col, marginTop: 4}}>
            {s.cells}{show_share ? ` of ${total} · ${Math.round((100 * s.cells) / total)}%` : ''}</div>
        </div>
      </div>;
    })}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
