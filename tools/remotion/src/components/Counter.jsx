import React from 'react';
import {Easing, interpolate} from 'remotion';
import {Footer, Heading, fmtNum, prog, useMs, usePal} from '../motion.jsx';

// One stated number counting from `from` to `to`, with its unit and a label under it. It sits on
// the same backdrop and heading as the other motion scenes, so the screen is never a lone number.
export const Counter = ({title, sub, label, unit = '', from, to, start_ms, count_ms, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const v = interpolate(ms, [start_ms, start_ms + count_ms], [from, to],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
  const done = ms >= start_ms + count_ms;
  const big = Math.abs(to - from) >= 100;
  const shown = big ? Math.round(v).toLocaleString('en-US') : fmtNum(v, '', '', Number.isInteger(to) && Number.isInteger(from) && done ? 0 : 2);
  const a = prog(ms, start_ms - 300, 500);
  const pulse = done ? 1 + 0.04 * Math.max(0, 1 - (ms - start_ms - count_ms) / 500) : 1;
  return <>
    {title ? <Heading title={title} sub={sub} pal={pal} /> : null}
    <div style={{position: 'absolute', left: 0, right: 0, top: 300, textAlign: 'center', opacity: a,
      transform: `scale(${pulse})`}}>
      <span style={{fontFamily: pal.headF, fontWeight: 700, fontSize: 240, color: done ? pal.good : pal.text,
        textShadow: done ? `0 0 30px ${pal.good}66` : 'none'}}>{shown}</span>
      {unit ? <span style={{fontFamily: pal.sansF, fontSize: 96, marginLeft: 24, color: pal.dim}}>{unit}</span> : null}
    </div>
    {label ? <div style={{position: 'absolute', left: 0, right: 0, top: 640, textAlign: 'center', opacity: prog(ms, start_ms + count_ms * 0.6, 500),
      fontFamily: pal.noteF, fontSize: 52, color: pal.accent2}}>{label}</div> : null}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
