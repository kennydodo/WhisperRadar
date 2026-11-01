import React from 'react';
import {AbsoluteFill, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {FitChart} from './components/FitChart.jsx';
import {Counter} from './components/Counter.jsx';
import {ScreenshotZoom} from './components/ScreenshotZoom.jsx';
import {Pipeline} from './components/Pipeline.jsx';
import {Generation} from './components/Generation.jsx';
import {CodePanel} from './components/CodePanel.jsx';
import {Levels} from './components/Levels.jsx';
import {Bars} from './components/Bars.jsx';
import {Flow} from './components/Flow.jsx';
import {Line} from './components/Line.jsx';
import {Grid} from './components/Grid.jsx';
import {Split} from './components/Split.jsx';
import {Backdrop, PointsColumn, usePal} from './motion.jsx';

// Allowlisted components. The Python side validates names and parameters
// before anything reaches here; unknown names render nothing.
const REGISTRY = {fit_chart: FitChart, screenshot: ScreenshotZoom};
const MOTION = {pipeline: Pipeline, generation: Generation, code: CodePanel, levels: Levels,
  bars: Bars, flow: Flow, line: Line, grid: Grid, split: Split, counter: Counter};

export const Scene = ({component, params, theme, fade}) => {
  const f = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const ms = (f / fps) * 1000, total = (durationInFrames / fps) * 1000;
  const fin = fade && fade.in_ms ? Math.min(1, ms / fade.in_ms) : 1;
  const fout = fade && fade.out_ms ? Math.min(1, (total - ms) / fade.out_ms) : 1;
  const C = REGISTRY[component] || MOTION[component];
  const isMotion = !!MOTION[component];
  const t = {background: '#121417', heading: '#f5c542', text: '#f2f2f2', ...theme};
  return (
    <AbsoluteFill style={{background: t.background}}>
      <style>{`@font-face{font-family:Handlee;src:url(${staticFile('Handlee-Regular.ttf')})}` + Object.entries(t.fontData || {}).map(([k, u]) => `@font-face{font-family:"${k}";src:url(${u})}`).join('')}</style>
      <AbsoluteFill style={{opacity: Math.max(0, Math.min(fin, fout))}}>
        {C ? (isMotion ? <MotionWrap C={C} name={component} params={params} t={t} /> : <C {...params} theme={t} />) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const SIDE = new Set(['counter', 'bars', 'flow']);   // simple diagrams give up their left quarter
const MotionWrap = ({C, name, params, t}) => {
  const pal = usePal(t);
  const pts = Array.isArray(params.points) && params.points.length ? params.points : null;
  if (!pts) return <Backdrop pal={pal}><C {...params} theme={t} /></Backdrop>;
  const place = params.points_place || (SIDE.has(name) && !(name === 'flow' && (params.nodes || []).length > 3) ? 'side' : 'below');
  const mode = params.points_mode || 'appear';
  // side: the diagram gives up its left quarter; below: a strip under the diagram, which keeps most of the width
  const box = place === 'side'
    ? {transform: 'scale(0.74)', transformOrigin: 'top right'}
    : {transform: 'scale(0.86)', transformOrigin: 'top center'};
  return <Backdrop pal={pal}>
    <div style={{position: 'absolute', left: 0, top: 0, width: 1920, height: 1080, ...box}}>
      <C {...params} theme={t} />
    </div>
    <PointsColumn points={pts} mode={mode} place={place} pal={pal} />
  </Backdrop>;
};
