import React from 'react';
import {Footer, Heading, Illustrative, mix, prog, roleColor, useMs, usePal} from '../motion.jsx';

// Text becomes tokens, tokens become numbers. The sentence is on screen first; each token is cut
// out of it on the narrator's words and its ID appears underneath. IDs are illustrative unless the
// scene says otherwise.
export const Tokens = ({title, sub, text, tokens, illustrative, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const a = prog(ms, 0, 600);
  const row = (label, top, p) => <div style={{position: 'absolute', left: 110, top, opacity: p, fontFamily: pal.monoF, fontSize: 24,
    letterSpacing: 1.5, textTransform: 'uppercase', color: pal.dim}}>{label}</div>;
  const firstAt = tokens.length ? Math.min(...tokens.map((t) => t.at_ms)) : 0;
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    {row('Text', 260, a)}
    <div style={{position: 'absolute', left: 110, right: 110, top: 300, opacity: a, fontFamily: pal.sansF, fontSize: 64, color: pal.text,
      transform: `translateY(${(1 - a) * 14}px)`}}>{text}</div>
    {row('Tokens', 470, prog(ms, firstAt - 200, 400))}
    <div style={{position: 'absolute', left: 110, right: 110, top: 520, display: 'flex', flexWrap: 'wrap', gap: 14}}>
      {tokens.map((t, i) => {
        const p = prog(ms, t.at_ms, 450);
        const c = roleColor(pal, null, i);
        return <div key={i} style={{opacity: p, transform: `translateY(${(1 - p) * -22}px) scale(${0.9 + 0.1 * p})`}}>
          <div style={{padding: '10px 22px', borderRadius: 10, fontFamily: pal.monoF, fontSize: 40, color: pal.text,
            background: mix(pal.bg, c, 0.22), border: `2px solid ${c}`}}>{t.text.trim()}</div>
        </div>;
      })}
    </div>
    {row('Token IDs', 690, prog(ms, firstAt + 300, 400))}
    <div style={{position: 'absolute', left: 110, right: 110, top: 740, display: 'flex', flexWrap: 'wrap', gap: 14}}>
      {tokens.map((t, i) => {
        const p = prog(ms, t.at_ms + 400, 450);
        const c = roleColor(pal, null, i);
        return <div key={i} style={{opacity: p, padding: '8px 22px', minWidth: 60, textAlign: 'center', fontFamily: pal.monoF, fontSize: 36,
          color: c, border: `2px dashed ${mix(pal.bg, c, 0.5)}`, borderRadius: 10}}>{t.id == null ? '·' : t.id}</div>;
      })}
    </div>
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
