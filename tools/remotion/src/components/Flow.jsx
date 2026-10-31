import React from 'react';
import {Footer, Heading, Icon, clamp01, ease, lerp, prog, roleColor, useMs, usePal} from '../motion.jsx';

// Things that hand something on: boxes in a row, and on the narrator's words a labelled token
// travels from box to box (a request, a payment, a file, a token), optionally coming back
// ("response", "receipt"). The box the token passes lights up, so the viewer sees the path.
const HOP = 650, DWELL = 350, FADE = 450;

const sendPhases = (s, n) => {
  const from = (s.from || 1) - 1, to = (s.to || n) - 1;
  const hops = Math.max(1, Math.abs(to - from));
  const out = hops * HOP;
  return {from, to, out, back: s.back ? out : 0, end: s.at_ms + out + DWELL + (s.back ? out + DWELL : 0)};
};

export const Flow = ({title, sub, nodes, sends = [], footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const n = nodes.length;
  const gap = n <= 3 ? 170 : n <= 4 ? 120 : 74;
  const w = Math.min(330, Math.floor((1660 - (n - 1) * gap) / n));
  const hasIcon = nodes.some((x) => x.icon_paths);
  const h = hasIcon ? 210 : 150;
  const cy = sends.length ? 520 : 540;
  const x0 = (1920 - (n * w + (n - 1) * gap)) / 2;
  const cx = (i) => x0 + i * (w + gap) + w / 2;
  const laneOut = cy - h / 2 - 58, laneBack = cy + h / 2 + 58;
  // where every token is now: [{x, y, label, color, opacity}]
  const tokens = [];
  sends.forEach((s, k) => {
    const P = sendPhases(s, n);
    const t = ms - s.at_ms;
    if (t < 0 || ms > P.end + FADE) return;
    const col = roleColor(pal, s.color, k + 1);
    const along = (a, b, p) => {               // node index a -> b, eased per hop so it settles on each box
      const hops = Math.abs(b - a), dir = Math.sign(b - a) || 1;
      const f = clamp01(p) * hops, i = Math.min(hops - 1, Math.floor(f));
      return lerp(cx(a + dir * i), cx(a + dir * (i + 1)), ease(f - i));
    };
    if (t <= P.out + DWELL || !s.back) {
      const fade = s.back ? 1 : 1 - clamp01((ms - (s.at_ms + P.out + DWELL)) / FADE);
      tokens.push({x: along(P.from, P.to, t / P.out), y: laneOut, label: s.label, color: col,
        opacity: Math.min(clamp01(t / 200), fade)});
    } else {
      const tb = t - P.out - DWELL;
      const fade = 1 - clamp01((ms - P.end) / FADE);
      tokens.push({x: along(P.to, P.from, tb / P.back), y: laneBack, label: s.back, color: pal.good, opacity: fade, back: true});
    }
  });
  const anyBack = sends.some((s) => s.back);
  const firstSend = sends.length ? Math.min(...sends.map((s) => s.at_ms)) : Infinity;
  const lanes = prog(ms, firstSend - 300, 500);
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <svg style={{position: 'absolute', left: 0, top: 0}} width={1920} height={1080}>
      <defs>
        <marker id="fa" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0 0 L10 5 L0 10 z" fill={pal.line} /></marker>
      </defs>
      {nodes.slice(1).map((nd, i) => {
        const p = prog(ms, nd.at_ms, 600);
        const xa = cx(i) + w / 2 + 10, xb = cx(i + 1) - w / 2 - 12;
        return <line key={i} x1={xa} y1={cy} x2={lerp(xa, xb, p)} y2={cy} stroke={pal.line} strokeWidth={5}
          strokeLinecap="round" markerEnd={p > 0.95 ? 'url(#fa)' : undefined} opacity={p > 0 ? 1 : 0} />;
      })}
      {sends.length ? <line x1={cx(0)} y1={laneOut} x2={cx(n - 1)} y2={laneOut} stroke={pal.line} strokeWidth={2}
        strokeDasharray="6 10" opacity={0.7 * lanes} /> : null}
      {anyBack ? <line x1={cx(0)} y1={laneBack} x2={cx(n - 1)} y2={laneBack} stroke={pal.line} strokeWidth={2}
        strokeDasharray="6 10" opacity={0.7 * lanes} /> : null}
    </svg>
    {nodes.map((nd, i) => {
      const a = prog(ms, nd.at_ms, 500);
      const hot = tokens.reduce((m, tk) => Math.max(m, tk.opacity * clamp01(1 - Math.abs(tk.x - cx(i)) / (w * 0.6))), 0);
      const col = roleColor(pal, nd.color, i);
      return <div key={i} style={{position: 'absolute', left: cx(i) - w / 2, top: cy - h / 2, width: w, height: h,
        borderRadius: 22, background: hot > 0.05 ? `${col}${Math.round(20 + hot * 40).toString(16)}` : pal.panel,
        border: `3px solid ${hot > 0.05 ? col : pal.line}`, boxShadow: hot > 0.05 ? `0 0 ${18 + 30 * hot}px ${col}` : 'none',
        opacity: a, transform: `translateY(${(1 - a) * 22}px) scale(${1 + hot * 0.04})`,
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 10}}>
        {nd.icon_paths ? <Icon paths={nd.icon_paths} size={74} color={col} p={a} /> : null}
        <div style={{fontFamily: pal.sansF, fontWeight: 650, fontSize: n <= 4 ? 34 : 28, color: pal.text, textAlign: 'center',
          padding: '0 14px', lineHeight: 1.1}}>{nd.label}</div>
        {nd.sub ? <div style={{fontFamily: pal.noteF, fontSize: n <= 4 ? 24 : 21, color: pal.dim, textAlign: 'center',
          padding: '0 12px'}}>{nd.sub}</div> : null}
      </div>;
    })}
    {tokens.map((tk, i) => <div key={i} style={{position: 'absolute', left: tk.x, top: tk.y, transform: 'translate(-50%, -50%)',
      opacity: tk.opacity, padding: '10px 22px', borderRadius: 999, background: tk.color, color: '#0b0e13',
      fontFamily: pal.sansF, fontWeight: 700, fontSize: 28, whiteSpace: 'nowrap', boxShadow: `0 0 20px ${tk.color}`}}>
      {tk.back ? '← ' : ''}{tk.label}{tk.back ? '' : ' →'}</div>)}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
