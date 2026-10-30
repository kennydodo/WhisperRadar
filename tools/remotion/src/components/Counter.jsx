import React from 'react';
import {Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {COL, Text, fmt} from './common.jsx';

// A number that counts from `from` to `to`, with a label under it.
export const Counter = ({title, label, unit, from, to, start_ms, count_ms, theme}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const ms = (f / fps) * 1000;
  const v = interpolate(ms, [start_ms, start_ms + count_ms], [from, to],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
  const done = ms >= start_ms + count_ms;
  const shown = Math.abs(to - from) >= 100 ? Math.round(v).toLocaleString('en-US') : fmt(v);
  return <>
    {title && <Text ms={0} x={134} y={64} size={72} color={theme.heading}>{title}</Text>}
    <div style={{position: 'absolute', left: 0, right: 0, top: 330, textAlign: 'center', fontFamily: 'Handlee',
      fontSize: 260, color: done ? COL.green : theme.text, transition: 'none'}}>
      {shown}<span style={{fontSize: 110, marginLeft: 24, color: COL.grey}}>{unit}</span>
    </div>
    <div style={{position: 'absolute', left: 0, right: 0, top: 690, textAlign: 'center', fontFamily: 'Handlee',
      fontSize: 52, color: COL.grey}}>{label}</div>
  </>;
};
