import React from 'react';
import {Footer, Heading, prog, useMs, usePal} from '../motion.jsx';

// Coarse to fine: a short broad level first, then longer levels that split it into more and
// more cells, then a decoder that turns the result into text. Each level appears on its words.
const X0 = 210, W = 1500, Y0 = 330, DY = 150, CH = 76;

export const Levels = ({title, sub, levels, decoder, result, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const cols = [pal.accent, pal.accent2, pal.good, pal.bad];
  const n = levels.length;
  const cellW = (c) => W / c - 12;
  const cx = (c, i) => X0 + (i + 0.5) * (W / c);
  const lines = [];
  const rowsEl = levels.map((lv, k) => {
    const a = prog(ms, lv.at_ms, 500);
    const col = cols[k % cols.length];
    const y = Y0 + k * DY;
    if (k > 0) {
      const par = levels[k - 1];
      for (let i = 0; i < lv.cells; i++) {
        const pi = Math.min(par.cells - 1, Math.floor((i * par.cells) / lv.cells));
        lines.push(<line key={`${k}-${i}`} x1={cx(par.cells, pi)} y1={y - DY + CH} x2={cx(lv.cells, i)} y2={y}
          stroke={col} strokeOpacity={0.5 * a} strokeWidth={2} />);
      }
    }
    return <React.Fragment key={k}>
      <div style={{position: 'absolute', left: X0, top: y - 34, fontFamily: pal.sansF, fontSize: 24, color: pal.dim, opacity: a}}>{lv.label}</div>
      {Array.from({length: lv.cells}, (_, i) => {
        const p = prog(ms, lv.at_ms + (i / Math.max(lv.cells, 1)) * 500, 450);
        return <div key={i} style={{position: 'absolute', left: cx(lv.cells, i) - cellW(lv.cells) / 2, top: y,
          width: cellW(lv.cells), height: CH, borderRadius: 14, border: `2px solid ${col}`, background: `${col}22`,
          opacity: p, transform: `scale(${0.85 + 0.15 * p})`, boxShadow: p < 1 ? `0 0 ${24 * (1 - p)}px ${col}` : 'none'}} />;
      })}
    </React.Fragment>;
  });
  const dy = Y0 + n * DY - 20;
  const da = decoder ? prog(ms, decoder.at_ms, 500) : 0;
  const ra = result ? prog(ms, result.at_ms, 600) : 0;
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <svg style={{position: 'absolute', left: 0, top: 0}} width={1920} height={1080}>{lines}</svg>
    {rowsEl}
    {decoder ? <div style={{position: 'absolute', left: 710, top: dy, width: 500, height: 70, borderRadius: 16,
      border: `2px solid ${pal.line}`, background: pal.panel, opacity: da, display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontFamily: pal.sansF, fontSize: 30, color: pal.text}}>↓ {decoder.label}</div> : null}
    {result ? <div style={{position: 'absolute', left: 210, width: 1500, top: dy + 100, textAlign: 'center', opacity: ra,
      fontFamily: pal.monoF, fontSize: 34, color: pal.good}}>{result.text}</div> : null}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
