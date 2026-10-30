import React from 'react';
import {Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {COL, Text} from './common.jsx';

// A screenshot shown whole, then zoomed onto `box` ([x, y, w, h] as 0..1 of the image).
export const ScreenshotZoom = ({image, box, start_ms, zoom_ms, caption, theme}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const ms = (f / fps) * 1000;
  const p = interpolate(ms, [start_ms, start_ms + zoom_ms], [0, 1],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  const W = 1632, H = 918, X = (1920 - W) / 2, Y = 40;       // the frame the image is fitted into
  const [bx, by, bw, bh] = box;
  const s = 1 + p * (Math.min(1 / bw, 1 / bh) - 1);
  const cx = (bx + bw / 2) * W, cy = (by + bh / 2) * H;
  const tx = p * (W / 2 - cx * s), ty = p * (H / 2 - cy * s);
  return <>
    <div style={{position: 'absolute', left: X, top: Y, width: W, height: H, overflow: 'hidden',
      border: `4px solid ${COL.grey}`, borderRadius: 12, boxSizing: 'border-box', background: '#000'}}>
      <img src={image} style={{width: W, height: H, objectFit: 'contain', transformOrigin: '0 0',
        transform: `translate(${tx}px, ${ty}px) scale(${s})`}} />
    </div>
    {caption && <Text ms={start_ms + zoom_ms} x={X} y={Y + H + 14} size={44} color={theme.text}>{caption}</Text>}
  </>;
};
