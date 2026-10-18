# Phase 1 demo report — narration-synced explainer demo

Date: 2026-10-17. Branch: `feat/explainer-engine`.

## What exists

`docs/explainer/demo/silent-timing-demo/`:

- `script.md` — original finance script (approved by owner before the TTS call)
- `audio.mp3` — OpenSpeaker/ai33 TTS, voice `edge_en-US-BrianMultilingualNeural`, speed 0.85 (27.3 s)
- `subtitles.srt` — faster-whisper transcription of that audio (14 cues) — the real
  stage-4 generator (`transcribe.transcribe_to_srt`)
- `explainer.json` — 4 scenes, cue ranges [1,5] [6,9] [10,12] [13,14]
- `out/preview.mp4` (crf 20) and `out/final.mp4` (crf 18), 1920x1080, 818 video
  frames = narration duration

## Reproduce

    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli validate docs\explainer\demo\silent-timing-demo
    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli render-demo docs\explainer\demo\silent-timing-demo
    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli render-demo docs\explainer\demo\silent-timing-demo --final

## Measured (RTX 3050 / 16 GB, software encoder)

- Cold full render (4 scenes 1080p30 + concat re-encode + mux): **58 s**
- Cache-warm final re-render (all 4 scene clips reused): **11 s**
- Memory: bounded (plate PNGs + one frame in flight; no full-production buffering)

## Effects proven

Cue-anchored counter count-up, arc-length arrow tracing (frame-pipe backend),
animated bars with fractions, staggered block reveal, heading fade-in,
callout slide-in, scene gap-fill hold, narration mux. Sync verified at cue
boundaries (t=8.0 budget resolved; t=14.5 split; frame walk f0..f817).

## Limitations (honest)

1. Cue precision = whisper cue boundaries, not word-level alignment; events
   anchor to cue starts/ends (+ optional `offset_ms`).
2. Demo assembly uses hard cuts. Crossfades between explainer scenes need the
   engine's transition trim/join machinery — Phase 3.
3. Whisper sometimes splits cues mid-phrase ("changes" / "everything");
   scene ranges were chosen to absorb this. Per-cue `offset_ms` is the tuning knob.
4. Prepared clips use libx264 `-bf 0` (engine preview convention) so concat is
   frame-exact; `-c copy` concat dropped 4 frames and was replaced by a
   re-encoding concat.
5. 720p preview scale-invariance is structural (normalized coords) but the
   preview-width pipeline (engine preview normalization) lands with Phase 3.

## Post-review hardening (2026-10-17, second render)

The external review found real issues; all fixed and covered by tests:

1. **Stale cache** — the fingerprint now includes the COMPILED timing (SRT
   dependent), theme values, font file contents and image asset contents
   (`_verify` also rejects wrong frame counts/fps). A changed narration can
   no longer reuse an old clip.
2. **Image objects** — accepted but never drawn; now rendered (contain/stretch
   into the box) by both backends, end-to-end test included.
3. **Filtergraph event semantics** — type-on now shows successive plate
   windows (true typewriter), declared fade_out is honored, the implicit
   scene-end fade is gone. Also fixed a double-offset bug this uncovered:
   text plates are full-canvas and must composite at (0,0); box plates
   (callout/image) at their box origin.
4. **Timing** — events outside a scene's final range are ERRORS (with the
   actual overshoot), not clamps; scene cue ranges may not overlap; the
   first scene is pinned to frame 0 BEFORE events compile, so a narration
   lead-in can no longer shift animations early.

The demo's `explainer.json` event anchors were also corrected to the real
narration cues (they had silently relied on the old clamp).
