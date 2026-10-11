# Plan: port ImgToVideo to Python inside WhisperRadar (branch `video-render`)

Decision (Kehinde 2026-10-10): convert to Python, live inside WhisperRadar; the C# repo
(`D:\Repos\ImgToVideo`) stays untouched as reference and fallback. Master stays the checked-out
branch in the main folder; all work happens in this worktree (`D:\Repos\WhisperRadar-video`).

## What WhisperRadar actually calls (the real scope)
`studio.py`/`autorun.py` shell out to `ImgToVideo.Cli` for only:
1. `export-batch <folder>`  - missing-images batch JSON (small; reads shotlist.json + images/).
2. `render-final <folder> --preview` and `render-final <folder>` (final.mp4 + captions.srt).
3. `export-premiere` / `export-capcut` (merge stage target) - OUT of the first cut; keep the C# CLI for them.
Not needed: `plan`, `strip-srt` (cheap to add later), the WPF app, ImageGen (WhisperRadar has its own engines).

## C# size (lines) and what ports
| Area | C# | Port |
|---|---|---|
| Core/Parsing (SRT, filename, scene map, natural sort) | ~500 | yes, first |
| Core/Options (imgtovideo.json) | ~400 | yes (dataclasses, same snake_case JSON) |
| Core/Analysis (ProjectLoader 610, SceneInference) | ~650 | yes |
| Core/Manifest (ShotList 853, ManifestPlanner 969, MissingShotList, VisualManifest) | ~2000 | yes (shotlist -> manifest; needed for scenes/reveals) |
| Core/Timing (293) + Motion (291) + Planning (EditPlanner 511, TransitionCuts) | ~1150 | yes - the heart; must be frame-exact |
| Core/Models, Serialization (timeline.json) | ~500 | yes |
| Core/Reporting (build-report) | ~280 | yes (simple) |
| Ffmpeg (PreviewRenderPlanFactory 914, RenderService 235, Runner 157, RevealImageWriter 203, SFX resolver/library ~215, EncoderProbe, ToolLocator) | ~2100 | yes - second heart |
| Premiere (529), CapCut (665), App (8273), ImageGen (1062) | ~10k | no (C# CLI stays for exports) |
Port target ~5k lines Python in package `whisperradar/video/` (modules mirror the C# names).

## Parity strategy (no dotnet on the VM, so no live C# to diff against)
Real golden data already exists: production folders under `E:\YOUTUBE` hold inputs
(shotlist.json, subtitles.srt, images/, audio, imgtovideo.json) AND the C#-produced
`out/timeline.json` + `out/build-report.json`, e.g.
`TEST PROJECTS/1_how investment fees eat compound growth`, `.../4_animals_four_minds...`,
`.../The 4 Morning Habits...`, `POV Your Investments Start Making More Than Your Salary (2)`.
1. Copy 3-4 of these (inputs + expected timeline.json; images can be tiny stand-ins sized from the
   real dimensions) into `tests/fixtures/video/` as golden cases.
2. Python `plan` must reproduce `timeline.json` field-for-field (frames, motion, viewports, transitions).
3. Port the key C# unit tests (TimingEngine, MotionEngine, EditPlanner, SrtParser, filename parser,
   Reveal, PreviewRenderPlanFactory, EncoderArgs) as Python unittests - the C# test files are the spec.
4. ffmpeg: compare the generated argument lists/filtergraphs with C# `PreviewRenderPlanFactoryTests`
   expectations; then render a short sample with the VM's ffmpeg (present) and check duration/frames via ffprobe;
   final visual check on Kehinde's machine against a C#-rendered preview of the same folder.

## Build order (each step green before the next)
1. Models + options + timeline JSON round-trip.
2. SRT / filename / scene-map parsing + ProjectLoader + scene inference.
3. Shotlist -> manifest planner (+ missing shotlist / `export-batch`).
4. Timing engine -> motion engine -> edit planner -> timeline.json golden match.
5. Reveal images + SFX resolver/library (needs ffmpeg for synthesized pops).
6. ffmpeg render plan + runner + encoder probe; preview first, then final (+captions.srt).
7. Wire into `studio.py` behind setting `video_engine = python | csharp` (default csharp until proven),
   `export-premiere/capcut` keep calling the C# CLI.
8. Flip default after Kehinde compares real renders.

## Risks / watch-outs
- Frame-exact rounding: C# `Math.Round` is banker's rounding (to even) by default; Python `round` is too, but
  `decimal`/float differences and `MidpointRounding` overrides must be checked per call site.
- Culture/invariant formatting in messages (C# fixed this 2026-10-06) - use plain formatting.
- Late-aligned transition placement was inverted and fixed on 2026-10-06 (`TransitionCuts.cs`): port the FIXED
  behavior (the golden timelines should be from after that fix - check file dates).
- Supersampled zoompan grid math (`supersample_target_width`, 4x cap) and 0 B-frames in preview.
- Hardware encoder probing (NVENC/AMF/QSV) must verify with the real args, as C# does.
- Paths: Windows backslashes in imgtovideo.json; use pathlib.

## Start point
Step 1-2 first, with golden fixtures copied in. Estimated 10-15 h total (see earlier estimate).
