import React from 'react';
import {Footer, Heading, Illustrative, clamp01, lerp, mix, prog, roleColor, useMs, usePal} from '../motion.jsx';

// Meaning as position: similar texts sit close together. A query lands among them and its nearest
// neighbours light up. A flat picture of what is really hundreds of dimensions (illustrative).
const X0 = 150, X1 = 1770, Y0 = 250, Y1 = 900;

export const Embeddings = ({title, sub, dots, query, nearest, illustrative = true, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const groups = [...new Set(dots.map((d) => d.group || ''))];
  const pos = (o) => [lerp(X0, X1, o.x), lerp(Y0, Y1, o.y)];
  const near = query && nearest ? [...dots.keys()].sort((i, j) => {
    const d = (k) => Math.hypot(dots[k].x - query.x, dots[k].y - query.y);
    return d(i) - d(j);
  }).slice(0, nearest.count || 2) : [];
  const nearP = nearest ? prog(ms, nearest.at_ms, 700) : 0;
  const qP = query ? prog(ms, query.at_ms, 600) : 0;
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <Illustrative pal={pal} show={illustrative} />
    <svg style={{position: 'absolute', left: 0, top: 0}} width={1920} height={1080}>
      <rect x={X0 - 40} y={Y0 - 30} width={X1 - X0 + 80} height={Y1 - Y0 + 60} rx={18} fill="none" stroke={pal.line} strokeWidth={1.5} opacity={0.8} />
      {query && nearP > 0 ? near.map((k) => {
        const [qx, qy] = pos(query), [dx, dy] = pos(dots[k]);
        return <line key={k} x1={qx} y1={qy} x2={lerp(qx, dx, nearP)} y2={lerp(qy, dy, nearP)} stroke={pal.accent2} strokeWidth={4}
          strokeLinecap="round" strokeDasharray="2 10" style={{filter: `drop-shadow(0 0 6px ${pal.accent2})`}} />;
      }) : null}
      {dots.map((d, i) => {
        const [x, y] = pos(d);
        const p = prog(ms, d.at_ms, 450);
        const gi = groups.indexOf(d.group || '');
        const c = roleColor(pal, d.color, gi);
        const isNear = near.includes(i) && nearP > 0.6;
        const dim = nearP > 0 && near.length && !near.includes(i) ? 1 - 0.55 * nearP : 1;
        return <g key={i} opacity={p * dim}>
          {isNear ? <circle cx={x} cy={y} r={34} fill="none" stroke={pal.accent2} strokeWidth={3} opacity={0.9} /> : null}
          <circle cx={x} cy={y} r={15 * (0.6 + 0.4 * p)} fill={c} />
          <text x={x + (isNear ? 46 : 26)} y={y + 9} fontFamily={pal.sansF} fontSize={28} fill={pal.text}>{d.label}</text>
        </g>;
      })}
      {query ? (() => {
        const [x, y] = pos(query);
        return <g opacity={qP}>
          <path d={`M ${x} ${y - 26} L ${x + 22} ${y} L ${x} ${y + 26} L ${x - 22} ${y} Z`} fill={pal.accent2}
            style={{filter: `drop-shadow(0 0 12px ${pal.accent2})`}} />
          <text x={x + 34} y={y + 10} fontFamily={pal.sansF} fontWeight={700} fontSize={32} fill={pal.accent2}>{query.label}</text>
        </g>;
      })() : null}
    </svg>
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
