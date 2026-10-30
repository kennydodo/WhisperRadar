import React from 'react';
import {Composition} from 'remotion';
import {Scene} from './Scene.jsx';

// One composition for every component: the scene's props carry the component
// name, its parameters, the frame count and the theme. Nothing else is trusted.
export const Root = () => (
  <Composition id="scene" component={Scene} width={1920} height={1080} fps={30} durationInFrames={30}
    defaultProps={{component: 'fit_chart', params: {}, frames: 30, fps: 30, theme: {}, fade: null}}
    calculateMetadata={({props}) => ({durationInFrames: props.frames, fps: props.fps})} />
);
