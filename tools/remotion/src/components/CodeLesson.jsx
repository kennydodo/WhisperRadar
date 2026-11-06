import React from 'react';
import {Footer, Heading, clamp01, lerp, mix, prog, roleColor, useMs, usePal} from '../motion.jsx';
import {Flow} from './Flow.jsx';
import {Pipeline} from './Pipeline.jsx';
import {Generation} from './Generation.jsx';
import {Levels} from './Levels.jsx';
import {Bars} from './Bars.jsx';
import {Line} from './Line.jsx';
import {Grid} from './Grid.jsx';
import {Split} from './Split.jsx';
import {Counter} from './Counter.jsx';

// A teaching-channel code scene: the concept diagram first (full screen), then a small-type editor
// that is typed out on the narrator's words, with an optional output panel and concept chips.
// Layout of the diagram: "before" (full screen, then gone), "beside" (half size next to the editor)
// or "before_then_beside" (full screen, then it shrinks to the right while the editor opens).
const DIAGRAMS = {flow: Flow, pipeline: Pipeline, generation: Generation, levels: Levels, bars: Bars,
  line: Line, grid: Grid, split: Split, counter: Counter};

const KEYWORDS = new Set(['def', 'return', 'for', 'in', 'if', 'elif', 'else', 'while', 'import', 'from', 'as', 'class', 'await',
  'async', 'const', 'let', 'function', 'with', 'try', 'except', 'lambda', 'not', 'and', 'or', 'is', 'pass', 'yield', 'raise',
  'True', 'False', 'None', 'self']);
const BUILTINS = new Set(['print', 'len', 'range', 'str', 'int', 'float', 'list', 'dict', 'set', 'sorted', 'sum', 'max', 'min',
  'zip', 'enumerate', 'open', 'map', 'filter']);
const TOKEN = /(#.*$|"[^"]*"|'[^']*'|@\w+|\b\d+(?:\.\d+)?\b|[A-Za-z_]\w*|\s+|.)/g;

// One line of code to coloured spans (keywords, builtins, calls, strings, numbers, comments).
export const tintLine = (line, pal) => {
  const col = {kw: pal.accent, str: pal.good, num: pal.accent2, com: pal.dim, fn: mix(pal.accent, '#ffffff', 0.35),
    bi: mix(pal.accent2, '#ffffff', 0.2), dec: pal.accent2};
  const out = [];
  let m, k = 0, prev = '';
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(line)) && m[0] !== '') {
    const s = m[0];
    let c = null, italic = false;
    if (s[0] === '#') { c = col.com; italic = true; }
    else if (s[0] === '"' || s[0] === "'") c = col.str;
    else if (s[0] === '@') c = col.dec;
    else if (/^\d/.test(s)) c = col.num;
    else if (/^[A-Za-z_]/.test(s)) {
      const rest = line.slice(m.index + s.length);
      if (KEYWORDS.has(s)) c = col.kw;
      else if (prev === 'def' || prev === 'class' || /^\(/.test(rest)) c = BUILTINS.has(s) ? col.bi : col.fn;
    }
    out.push(c ? <span key={k++} style={{color: c, fontStyle: italic ? 'italic' : 'normal'}}>{s}</span> : <span key={k++}>{s}</span>);
    if (/\S/.test(s)) prev = s;
  }
  return out;
};

const Chips = ({chips, pal, vertical, left, top, gate = 1}) => {
  const ms = useMs();
  return <div style={{position: 'absolute', left, top, display: 'flex', flexDirection: vertical ? 'column' : 'row', gap: vertical ? 22 : 16}}>
    {chips.map((c, i) => {
      const p = prog(ms, c.start_ms || 0, 450) * gate;
      const col = roleColor(pal, c.color, i);
      return <div key={i} style={{opacity: p, transform: `translateX(${(1 - p) * -18}px)`, padding: vertical ? '12px 26px' : '8px 22px', borderRadius: 6,
        minWidth: vertical ? 168 : undefined, textAlign: 'center', fontFamily: pal.sansF, fontWeight: 700, fontSize: vertical ? 30 : 26, color: '#0b0f12',
        background: `linear-gradient(135deg, ${col}, ${mix(col, pal.bg, 0.4)})`, boxShadow: '0 6px 18px rgba(0,0,0,0.35)'}}>{c.label}</div>;
    })}
  </div>;
};

const Diagram = ({spec, pal, theme, scale, x, y, opacity}) => {
  const C = DIAGRAMS[spec.component];
  if (!C || opacity <= 0) return null;
  const params = {...spec.params};
  delete params.title; delete params.sub; delete params.footer; delete params.points;
  // the diagram's own heading band is cut off: this scene's heading is the only one on screen
  return <div style={{position: 'absolute', left: x, top: y, width: 1920, height: 1080, opacity, transformOrigin: 'top left',
    transform: `scale(${scale})`, clipPath: 'inset(190px 0 0 0)'}}>
    <C {...params} theme={theme} />
  </div>;
};

export const CodeLesson = ({title, sub, filename, lines, highlights, chips, output, diagram, start_ms, type_ms, footer, theme}) => {
  const pal = usePal(theme);
  const ms = useMs();
  const place = diagram ? (diagram.place || 'before_then_beside') : null;
  const shrinks = place === 'before_then_beside', beside = place === 'beside';
  // 0 = diagram full screen / editor not shown, 1 = editor open
  const open = shrinks ? prog(ms, start_ms - 700, 800) : beside ? 1 : prog(ms, start_ms - 400, 600);
  const narrow = place === 'beside' || shrinks;           // editor shares the screen with the diagram
  const hasChips = chips && chips.length;
  const railed = hasChips && !narrow;                      // chips as a left rail only when the editor is wide
  const CX = railed ? 290 : narrow ? 60 : 110;
  const CW = railed ? 1580 : narrow ? 820 : 1700;
  const FS = narrow ? 20 : 24, LH = narrow ? 30 : 34;
  const CY = hasChips && !railed ? 262 : 205;
  const total = lines.join('\n').length;
  const typed = Math.floor(clamp01((ms - start_ms) / type_ms) * total);
  const codeH = lines.length * LH + 28;
  const cardH = 48 + codeH;
  const out = output && output.lines && output.lines.length ? output : null;
  const outP = out ? prog(ms, out.start_ms, 500) : 0;
  const hl = highlights || [];
  const showDiagram = diagram && ((place === 'before' && open < 1) || narrow || place === 'before_then_beside');
  const SC = 0.56, DX = 852, DY = 150;
  const dScale = shrinks ? lerp(1, SC, open) : beside ? SC : 1;
  const dX = shrinks ? lerp(0, DX, open) : beside ? DX : 0;
  const dY = shrinks ? lerp(0, DY, open) : beside ? DY : 0;
  const dOpacity = place === 'before' ? 1 - open : prog(ms, 0, 500);
  const cardOpacity = open;
  let used = 0;
  return <>
    <Heading title={title} sub={sub} pal={pal} />
    {showDiagram ? <Diagram spec={diagram} pal={pal} theme={theme} scale={dScale} x={dX} y={dY} opacity={dOpacity} /> : null}
    {hasChips ? <Chips chips={chips} pal={pal} vertical={railed} left={railed ? 60 : narrow ? 60 : 110} top={railed ? CY + 20 : 178}
      gate={place === 'before' || shrinks ? open : 1} /> : null}
    <div style={{position: 'absolute', left: CX, top: CY + (hasChips && !railed ? 0 : 0) + (1 - cardOpacity) * 26, width: CW, height: cardH, borderRadius: 14,
      background: pal.panel, border: `1px solid ${pal.line}`, opacity: cardOpacity, boxShadow: '0 24px 60px rgba(0,0,0,0.5)', overflow: 'hidden'}}>
      <div style={{height: 48, borderBottom: `1px solid ${pal.line}`, display: 'flex', alignItems: 'center', padding: '0 18px', gap: 9,
        background: mix(pal.panel, pal.bg, 0.4)}}>
        {['#ff5f56', '#ffbd2e', '#27c93f'].map((c) => <div key={c} style={{width: 12, height: 12, borderRadius: 6, background: c}} />)}
        <div style={{marginLeft: 16, fontFamily: pal.monoF, fontSize: 19, color: pal.dim}}>{filename}</div>
      </div>
      <div style={{position: 'relative', padding: '14px 0'}}>
        {hl.map((h, i) => {
          const p = prog(ms, start_ms + h.start_ms, 450);
          return <div key={i} style={{position: 'absolute', left: 8, right: 8, top: 14 + (h.line - 1) * LH, height: LH, borderRadius: 6,
            background: `${pal.accent2}22`, borderLeft: `4px solid ${pal.accent2}`, opacity: p}} />;
        })}
        {lines.map((ln, i) => {
          const show = Math.max(0, Math.min(ln.length, typed - used));
          const here = typed - used >= 0 && typed - used <= ln.length && typed < total;
          used += ln.length + 1;
          const note = hl.find((h) => h.line === i + 1 && h.note);
          const np = note ? prog(ms, start_ms + note.start_ms + 250, 450) : 0;
          return <div key={i} style={{position: 'relative', height: LH, display: 'flex', alignItems: 'center', fontFamily: pal.monoF,
            fontSize: FS, color: pal.text, whiteSpace: 'pre'}}>
            <span style={{width: 58, paddingRight: 18, textAlign: 'right', color: pal.dim, fontSize: FS - 5, opacity: 0.8}}>{i + 1}</span>
            <span>{tintLine(ln.slice(0, show), pal)}</span>
            {here ? <span style={{width: Math.round(FS * 0.55), height: FS + 2, marginLeft: 1, background: pal.accent,
              opacity: Math.floor(ms / 400) % 2 === 0 || show < ln.length ? 0.95 : 0.2}} /> : null}
            {note ? <span style={{position: 'absolute', right: 20, fontFamily: pal.sansF, fontSize: FS - 3, color: pal.accent2, opacity: np,
              transform: `translateX(${(1 - np) * 10}px)`}}>← {note.note}</span> : null}
          </div>;
        })}
      </div>
    </div>
    {out ? <div style={{position: 'absolute', left: CX, top: CY + cardH + 16 + (1 - outP) * 16, width: CW, borderRadius: 12, opacity: outP,
      background: mix(pal.bg, '#000000', 0.45), border: `1px solid ${pal.line}`, overflow: 'hidden', boxShadow: '0 18px 44px rgba(0,0,0,0.45)'}}>
      <div style={{height: 36, display: 'flex', alignItems: 'center', padding: '0 18px', gap: 10, borderBottom: `1px solid ${pal.line}`,
        fontFamily: pal.monoF, fontSize: 17, color: pal.dim, letterSpacing: 1, textTransform: 'uppercase'}}>
        <span style={{color: pal.good}}>›_</span>{out.label || 'Output'}
      </div>
      <div style={{padding: '10px 0 12px'}}>
        {out.lines.map((ln, i) => {
          const lp = prog(ms, out.start_ms + 200 + i * 160, 300);
          return <div key={i} style={{height: 28, display: 'flex', alignItems: 'center', fontFamily: pal.monoF, fontSize: FS - 2,
            color: pal.text, whiteSpace: 'pre', paddingLeft: 24, opacity: lp}}>{ln}</div>;
        })}
      </div>
    </div> : null}
    {footer ? <Footer footer={footer} pal={pal} /> : null}
  </>;
};
