import React from 'react';
import {Heading, Note, Scribble, clamp01, prog, useMs, usePal} from '../motion.jsx';

const KW = /\b(def|return|for|in|if|else|while|import|from|class|await|async|const|let|function|True|False|None)\b/g;
const tint = (line, pal) => {
  const parts = [];
  const re = /("[^"]*"|'[^']*'|#.*$|\b\d+(\.\d+)?\b|\b(?:def|return|for|in|if|else|while|import|from|class|await|async|const|let|function|True|False|None)\b)/g;
  let last = 0, m, k = 0;
  while ((m = re.exec(line))) {
    if (m.index > last) parts.push(<span key={k++}>{line.slice(last, m.index)}</span>);
    const s = m[0];
    const c = /^["']/.test(s) ? pal.good : s[0] === '#' ? pal.dim : /^\d/.test(s) ? pal.accent2 : pal.accent;
    parts.push(<span key={k++} style={{color: c}}>{s}</span>);
    last = m.index + s.length;
  }
  if (last < line.length) parts.push(<span key={k++}>{line.slice(last)}</span>);
  return parts;
};

// Evidence on screen: real code or a source excerpt, typed out, with the line that matters lit up.
export const CodePanel = ({title, sub, filename, lines, highlights, start_ms, type_ms, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const total = lines.join('\n').length;
  const typed = Math.floor(clamp01((ms - start_ms) / type_ms) * total);
  let used = 0;
  const a = prog(ms, start_ms - 300, 600);
  const LH = 52, X = 200, Y = 330;
  const hl = highlights || [];
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    <div style={{position: 'absolute', left: 140, top: 250, width: 1640, height: 90 + lines.length * LH, borderRadius: 18,
      background: pal.panel, border: `1px solid ${pal.line}`, opacity: a, transform: `translateY(${(1 - a) * 24}px)`,
      boxShadow: '0 30px 80px rgba(0,0,0,0.5)'}}>
      <div style={{height: 56, borderBottom: `1px solid ${pal.line}`, display: 'flex', alignItems: 'center', padding: '0 22px', gap: 10}}>
        {['#ff5f56', '#ffbd2e', '#27c93f'].map((c) => <div key={c} style={{width: 14, height: 14, borderRadius: 7, background: c}} />)}
        <div style={{marginLeft: 18, fontFamily: pal.monoF, fontSize: 22, color: pal.dim}}>{filename}</div>
      </div>
    </div>
    {hl.map((h, i) => {
      const p = prog(ms, start_ms + h.start_ms, 500);
      return <div key={i} style={{position: 'absolute', left: 150, top: Y + 12 + (h.line - 1) * LH - 2, width: 1620, height: LH,
        borderRadius: 8, background: `${pal.accent2}26`, borderLeft: `5px solid ${pal.accent2}`, opacity: p,
        boxShadow: `0 0 28px ${pal.accent2}33`}} />;
    })}
    {lines.map((ln, i) => {
      const show = Math.max(0, Math.min(ln.length, typed - used));
      const cursor = show < ln.length || (typed - used >= ln.length && typed < total && typed - used === ln.length) ;
      used += ln.length + 1;
      return <div key={i} style={{position: 'absolute', left: X - 40, top: Y + 12 + i * LH, height: LH, display: 'flex', alignItems: 'center',
        fontFamily: pal.monoF, fontSize: 30, color: pal.text, whiteSpace: 'pre'}}>
        <span style={{width: 56, color: pal.dim, fontSize: 22}}>{i + 1}</span>
        <span>{tint(ln.slice(0, show), pal)}</span>
        {show > 0 && show < ln.length ? <span style={{width: 14, height: 30, background: pal.accent, marginLeft: 2}} /> : null}
      </div>;
    })}
    {hl.filter((h) => h.note).map((h, i) => <Note key={i} x={1180} y={Y + 12 + (h.line - 1) * LH - 6} start={start_ms + h.start_ms + 300} pal={pal} size={32}>← {h.note}</Note>)}
  </>;
};
