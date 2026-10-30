import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';

// Shared look for the motion scenes. Everything channel-specific comes from `theme.style`
// (colours, fonts), so another channel changes the preset, not the components.
// Everything here is derived from the channel's theme (colours, fonts, line style), which
// Python sends as theme.look / theme.fonts / theme.hand. The values below are only the
// fallback when the theme says nothing.
const FALLBACK = {bg: '#12161A', fg: '#F2EFE6', muted: '#8E96A0', accent: '#7FC8F8', good: '#8FD9A8', bad: '#FF9B8A',
  heading: '', text: ''};
const hex = (c) => { const m = /^#?([0-9a-f]{6})$/i.exec(c || ''); return m ? [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)) : [18, 22, 26]; };
export const mix = (a, b, t) => '#' + hex(a).map((v, i) => Math.round(v + (hex(b)[i] - v) * t).toString(16).padStart(2, '0')).join('');
const lum = (c) => { const [r, g, b] = hex(c); return (0.299 * r + 0.587 * g + 0.114 * b) / 255; };
export const SANS = 'Inter, "Segoe UI", "Helvetica Neue", system-ui, Arial, sans-serif';
export const MONO = '"JetBrains Mono", Consolas, "DejaVu Sans Mono", monospace';

export const usePal = (theme) => {
  const L = {...FALLBACK, ...(theme?.look || {})};
  const dark = lum(L.bg) < 0.5;
  const edge = dark ? '#000000' : '#ffffff';
  const F = theme?.fonts || {};
  const fam = (k, d) => (F[k] ? `"${F[k]}", ${d}` : d);
  return {
    accent: L.accent, accent2: L.heading || L.accent, good: L.good, bad: L.bad,
    text: L.text || L.fg, dim: L.muted, bg: L.bg,
    panel: mix(L.bg, L.fg, dark ? 0.07 : 0.04), line: mix(L.bg, L.fg, dark ? 0.2 : 0.16), bg2: mix(L.bg, edge, 0.35),
    heading: L.heading || L.fg,
    hand: !!theme?.hand,
    sansF: fam('body', SANS), headF: fam('heading', SANS), monoF: fam('mono', MONO), noteF: fam('note', SANS),
  };
};

export const useMs = () => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  return (f / fps) * 1000;
};
export const clamp01 = (v) => Math.max(0, Math.min(1, v));
export const ease = Easing.bezier(0.22, 1, 0.36, 1);
export const prog = (ms, start, dur) => ease(clamp01((ms - start) / dur));
export const lerp = (a, b, t) => a + (b - a) * t;

export const Backdrop = ({pal, children}) => {
  const ms = useMs();
  const drift = (ms / 1000) * 6;
  return (
    <AbsoluteFill style={{background: `radial-gradient(1200px 700px at 30% 20%, ${pal.panel}, ${pal.bg2})`}}>
      <AbsoluteFill style={{opacity: 0.35, backgroundImage:
        `linear-gradient(${pal.line} 1px, transparent 1px), linear-gradient(90deg, ${pal.line} 1px, transparent 1px)`,
        backgroundSize: '64px 64px', backgroundPosition: `${drift}px ${drift}px`}} />
      <AbsoluteFill style={{background: 'radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.45) 100%)'}} />
      {children}
    </AbsoluteFill>
  );
};

export const Heading = ({title, sub, pal}) => {
  const ms = useMs();
  const p = prog(ms, 0, 600);
  return (
    <div style={{position: 'absolute', left: 110, top: 64, opacity: p, transform: `translateY(${(1 - p) * 12}px)`}}>
      <div style={{display: 'flex', alignItems: 'center', gap: 16}}>
        <div style={{width: 6, height: 40, background: pal.accent, borderRadius: 3, boxShadow: `0 0 18px ${pal.accent}`}} />
        <div style={{fontFamily: pal.headF, fontWeight: 650, fontSize: 40, color: pal.heading, letterSpacing: -0.5}}>{title}</div>
      </div>
      {sub ? <div style={{fontFamily: pal.sansF, fontSize: 24, color: pal.dim, marginTop: 8, marginLeft: 22}}>{sub}</div> : null}
    </div>
  );
};

// A hand-drawn ring or underline for the one thing worth noticing.
export const Scribble = ({x, y, w, h, start, pal, color, kind = 'ring'}) => {
  const ms = useMs();
  const p = prog(ms, start, 700);
  const c = color || pal.accent2;
  const straight = !pal.hand;
  const d = straight ? `M 0 ${h} L ${w} ${h}` : kind === 'ring'
    ? `M ${w * 0.5} 4 C ${w * 0.95} 0, ${w + 8} ${h * 0.5}, ${w * 0.85} ${h * 0.9} C ${w * 0.5} ${h + 6}, 6 ${h * 0.95}, 2 ${h * 0.5} C 0 ${h * 0.1}, ${w * 0.35} -4, ${w * 0.72} 6`
    : `M 0 ${h * 0.6} C ${w * 0.3} ${h * 0.2}, ${w * 0.6} ${h}, ${w} ${h * 0.4}`;
  return (
    <svg style={{position: 'absolute', left: x - 10, top: y - 10, overflow: 'visible', opacity: p > 0 ? 1 : 0}}
      width={w + 20} height={h + 20} viewBox={`-10 -10 ${w + 20} ${h + 20}`}>
      <path d={d} fill="none" stroke={c} strokeWidth={5} strokeLinecap="round" pathLength={1}
        strokeDasharray={1} strokeDashoffset={1 - p} style={{filter: `drop-shadow(0 0 6px ${c})`}} />
    </svg>
  );
};

export const Note = ({x, y, start, children, pal, size = 34, color}) => {
  const ms = useMs();
  const p = prog(ms, start, 500);
  const hand = pal.hand;
  return <div style={{position: 'absolute', left: x, top: y, fontFamily: pal.noteF, fontSize: hand ? size : Math.round(size * 0.78),
    color: color || pal.accent2, opacity: p, transform: `translateY(${(1 - p) * 10}px)${hand ? ' rotate(-2deg)' : ''}`,
    whiteSpace: 'nowrap', borderLeft: hand ? 'none' : `4px solid ${color || pal.accent2}`, paddingLeft: hand ? 0 : 14}}>{children}</div>;
};

// The sentence the planner wants on screen for the point being made: appears on its words.
export const Footer = ({footer, pal}) => {
  const ms = useMs();
  const p = prog(ms, footer.start_ms, 500);
  return <div style={{position: 'absolute', left: 110, right: 110, top: 950, display: 'flex', alignItems: 'center', gap: 16,
    opacity: p, transform: `translateY(${(1 - p) * 10}px)`}}>
    <div style={{width: 5, alignSelf: 'stretch', background: pal.accent2, borderRadius: 3}} />
    <div style={{fontFamily: pal.sansF, fontSize: 32, color: pal.text}}>{footer.text}</div>
  </div>;
};
