"""Explainer engine: declarative 2D animated scenes inside the Python video engine.

Modules: schema/validate (contract), timing (cue->frames), layout (geometry),
evaluator (deterministic per-frame state), plates (Pillow art), compose
(filtergraph backend), pipe (frame-pipe backend for path tracing), prepare
(scene -> silent clip cache), cli (validate/storyboard/render-scene).
"""
