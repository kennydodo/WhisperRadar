import React from 'react';
import {AbsoluteFill, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {FitChart} from './components/FitChart.jsx';
import {Counter} from './components/Counter.jsx';
import {ScreenshotZoom} from './components/ScreenshotZoom.jsx';

// Allowlisted components. The Python side validates names and parameters
// before anything reaches here; unknown names render nothing.
const REGISTRY = {fit_chart: FitChart, counter: Counter, screenshot: ScreenshotZoom};

export const Scene = ({component, params, theme, fade}) => {
  const f = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const ms = (f / fps) * 1000, total = (durationInFrames / fps) * 1000;
  const fin = fade && fade.in_ms ? Math.min(1, ms / fade.in_ms) : 1;
  const fout = fade && fade.out_ms ? Math.min(1, (total - ms) / fade.out_ms) : 1;
  const C = REGISTRY[component];
  const t = {background: '#121417', heading: '#f5c542', text: '#f2f2f2', ...theme};
  return (
    <AbsoluteFill style={{background: t.background}}>
      <style>{`@font-face{font-family:Handlee;src:url(${staticFile('Handlee-Regular.ttf')})}`}</style>
      <AbsoluteFill style={{opacity: Math.max(0, Math.min(fin, fout))}}>
        {C ? <C {...params} theme={t} /> : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
