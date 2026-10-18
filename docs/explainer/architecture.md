# Explainer engine — architecture note (Phase 0 exit)

Date: 2026-10-17 (whiteboard extension: 2026-10-18). Branch: `feat/explainer-engine` (worktree `D:\Repos\WhisperRadar-explainer`).

## Backend assignment per action (FFmpeg 9.0.1 gyan full build — see ffmpeg-capabilities.md)

| Effect | Backend | Mechanism |
|---|---|---|
| Text fade in/out | filtergraph | Pillow text plate (RGBA PNG) + `overlay` + `fade=alpha=1` on plate input |
| Text slide | filtergraph | `overlay` with `eval=frame` x/y expressions |
| Type-on label | filtergraph | Pillow per-cue plates (one per substring) OR crop-window overlay; MVP = plate sequence |
| Counter | filtergraph | `drawtext` `text=%{eif:trunc(from+(to-from)*min(1,(t-t0)/dur)):d}` with theme font |
| Bar grow | filtergraph | `drawbox` `eval=frame`, width expression from 0→target |
| Grid/blocks appear | filtergraph | one `drawbox` per block with `enable='gte(t,t_i)'` or plate + staged overlays |
| Object emphasis (pulse) | filtergraph | `drawbox` outline with alpha expression (restrained) |
| Path/arrow draw | frame-pipe | arc-length sampling, Python draws partial stroke per frame, pipes RGB to ffmpeg |
| Photo cutout fade/slide/scale | filtergraph | PNG cutout plate + `overlay`/`scale` expressions + `fade=alpha` |

Plates are rendered ONCE per scene (Pillow, RGBA PNGs in `out/explainer/plates/<scene>/`),
the filtergraph animates them. Nothing re-renders per frame except frame-pipe scenes.

## Module layout (new package `whisperradar/video/explainer/`)

- `schema.py` — strict enums, dataclasses, JSON Schema for `explainer.json`
- `validate.py` — human-readable errors (scene/object/event ids), asset path checks
- `timing.py` — cue edges → integer frames (repo convention: `round(seconds*fps)`, end exclusive)
- `layout.py` — theme resolution, safe areas (incl. subtitle band), normalized→px, Pillow text wrap
- `evaluator.py` — deterministic object state at frame N (pure function)
- `plates.py` — Pillow rendering of text/path/diagram/cutout plates
- `compose.py` — filtergraph builder per scene (plates + drawtext/drawbox/overlay)
- `pipe.py` — frame-pipe writer (binary stdin extension of `FfmpegRunner`), path tracing
- `prepare.py` — scene → silent clip in `out/explainer/`, temp+atomic rename, ffprobe verify
- `cli.py` — `validate` / `storyboard` / `render-scene`

## Pipeline integration (Phase 3)

- `explainer.json` sidecar in the production folder; scenes reference `shot_id`s.
- Prep step in `render_project.render_final` before segment render (mirrors `reveal_images.ensure`).
- Timeline clips referencing prepared clips get an explicit explainer source branch in
  `render_plan` (plain video input → scale/crop normalize → trim piece; NO zoompan,
  NO motion gate, NO `video_path`).
- Cache: prep fingerprint = schema version + resolved scene JSON + theme + font ids +
  asset content hashes + fps + dimensions; prep output path embeds short hash; segment
  manifest reuse then works unchanged.
- Visual mode per production: `existing | mixed | explainer` (Phase 3/4 settings).

## Timing rules

- Narration/SRT is master clock. Cue ids = SRT indices (1-based, repo convention).
- Scene spans `cue_range [a, b]` → frames `[round(start_s*fps), round(end_s*fps))`.
- Action `at: {cue, edge: start|end}` → scene-local frame via global timestamp minus
  scene start; durations in ms → frames with round-half-away; end frames exclusive.
- Transition handles: prepared clip covers the shot's full frame range exactly as the
  timing engine requests it (inspect `transition_cuts` head/tail trims in Phase 3);
  action times never double-subtract transition time.

## Demo (Phase 1)

Silent timing demo first (labeled as such), then narration via the real pipeline:
user-approved script → ai33 TTS (`scripts/ai33_tts.py`) → whisper SRT
(`transcribe.transcribe_to_srt`) → re-render, cue-synced. No dashboard involvement.

## Whiteboard extension (doc section 16, 2026-10-18)

Third selectable mode per production: isual_mode: images | motion_graphics |
whiteboard (ProjectOptions + settings spec; explicit mode never silently
overridden by a sidecar - legacy sidecar-only projects keep working).

- strokes.py: curve flattening (cubic/quadratic, bounded error), arc-length
  tables, partial geometry and pen tip as pure functions of progress.
- glyphs.py: single-line handwriting alphabet (66 glyphs, em 100, baseline
  80) as ordered strokes; missing glyphs are validation errors with a
  disclosed typed-text fallback. A font wipe is never used for handwriting.
- strokes_library.py: 18 built-in assets (authored in-repo) + project JSON
  stroke assets with the same schema; restricted feature set, no scripts.
- schema v2: stroke_asset, handwriting, draw_strokes/erase,
  doc style field; v1 documents migrate (style defaults motion_graphics).
- Whiteboard scenes always render through the frame-pipe backend (strokes +
  plates composited in Python); marker width derives from glyph scale.
- Cache fingerprints include the stroke-library version and asset file hashes.

Optional marker hand (16.6) and canvas pan/zoom (16.3 last bullet) are
deliberately NOT built: they are phase-6 polish behind a separate quality gate.
