import React from 'react';
import {Footer, Heading, Illustrative, mix, prog, roleColor, useMs, usePal} from '../motion.jsx';

// An LLM writes one token at a time: the text so far, the candidates for the next token with their
// probabilities, and the one picked, which is appended to the text. Probabilities are illustrative
// unless the scene says otherwise.
export const NextToken = ({title, sub, lead, options, pick, illustrative = true, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const a = prog(ms, 0, 600);
  const chosen = pick ? options[pick.option - 1] : null;
  const pp = pick ? prog(ms, pick.at_ms, 600) : 0;
  const RH = Math.min(96, 560 / options.length);
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    <div style={{position: 'absolute', left: 110, right: 110, top: 250, opacity: a, fontFamily: pal.sansF, fontSize: 62, color: pal.text,
      whiteSpace: 'pre-wrap', lineHeight: 1.15}}>
      {lead}
      <span style={{color: pal.accent2, fontWeight: 700, opacity: pp, display: 'inline-block', transform: `translateY(${(1 - pp) * 16}px)`}}>
        {chosen ? ' ' + chosen.word : ''}</span>
      {pp < 1 ? <span style={{display: 'inline-block', width: 8, height: 60, marginLeft: 6, verticalAlign: 'middle', background: pal.accent,
        opacity: Math.floor(ms / 400) % 2 === 0 ? 0.9 : 0.2}} /> : null}
    </div>
    <div style={{position: 'absolute', left: 110, top: 410, opacity: a, fontFamily: pal.monoF, fontSize: 24, letterSpacing: 1.5,
      textTransform: 'uppercase', color: pal.dim}}>Next token: chance of each candidate</div>
    {options.map((o, i) => {
      const p = prog(ms, o.at_ms, 600);
      const isPick = pick && pick.option === i + 1;
      const c = isPick ? pal.accent2 : roleColor(pal, null, i);
      const dim = pick && !isPick ? 1 - 0.55 * pp : 1;
      return <div key={i} style={{position: 'absolute', left: 110, top: 465 + i * RH, width: 1700, height: RH - 14, display: 'flex', alignItems: 'center',
        gap: 24, opacity: p * dim}}>
        <div style={{width: 260, fontFamily: pal.monoF, fontSize: 38, color: isPick && pp > 0.5 ? pal.accent2 : pal.text, textAlign: 'right'}}>{o.word}</div>
        <div style={{flex: 1, height: RH - 30, background: mix(pal.bg, pal.line, 0.6), borderRadius: 8, overflow: 'hidden'}}>
          <div style={{width: `${o.p * 100 * p}%`, height: '100%', background: `linear-gradient(90deg, ${mix(c, pal.bg, 0.35)}, ${c})`, borderRadius: 8,
            boxShadow: isPick && pp > 0.3 ? `0 0 24px ${c}88` : 'none'}} />
        </div>
        <div style={{width: 120, fontFamily: pal.monoF, fontSize: 36, color: c}}>{Math.round(o.p * 100 * p)}%</div>
      </div>;
    })}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
