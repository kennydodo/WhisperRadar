import React from 'react';
import {Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';

export const COL = {yellow: '#f5c542', green: '#5fd38d', blue: '#6db6ff', pink: '#ff8fb8', orange: '#ff9a4d',
  purple: '#b48cff', red: '#ff5c5c', grey: '#9aa0a6'};
export const fmt = (v) => String(+v.toFixed(2));

export const useP = (ms, dur = 600) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  return interpolate((f / fps) * 1000, [ms, ms + dur], [0, 1],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
};

export const Text = ({children, ms, x, y, size = 44, color = '#f2f2f2'}) => {
  const p = useP(ms, 500);
  return <div style={{fontFamily: 'Handlee', position: 'absolute', left: x, top: y, fontSize: size,
    color: COL[color] ?? color, opacity: p, transform: `translateY(${(1 - p) * 10}px)`, whiteSpace: 'nowrap'}}>{children}</div>;
};
