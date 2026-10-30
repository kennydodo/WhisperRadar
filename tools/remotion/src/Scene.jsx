import React from 'react';
import {AbsoluteFill, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {FitChart} from './components/FitChart.jsx';
import {Counter} from './components/Counter.jsx';
import {ScreenshotZoom} from './components/ScreenshotZoom.jsx';
import {Pipeline} from './components/Pipeline.jsx';
import {Generation} from './components/Generation.jsx';
import {CodePanel} from './components/CodePanel.jsx';
import {Backdrop, usePal} from './motion.jsx';

// Allowlisted components. The Python side validates names and parameters
// before anything reaches here; unknown names render nothing.
const REGISTRY = {fit_chart: FitChart, counter: Counter, screenshot: ScreenshotZoom};
const MOTION = {pipeline: Pipeline, generation: Generation, code: CodePanel};

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
        {C ? (isMotion ? <MotionWrap C={C} params={params} t={t} /> : <C {...params} theme={t} />) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const MotionWrap = ({C, params, t}) => {
  const pal = usePal(t);
  return <Backdrop pal={pal}><C {...params} theme={t} /></Backdrop>;
};
