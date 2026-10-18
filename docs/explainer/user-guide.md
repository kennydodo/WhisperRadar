# Explainer engine — user guide

How to build a fully local animated explainer inside WhisperRadar: no Flow
images, no video-generation models, no paid services (except your existing TTS
hook). The narration audio and its SRT are the master clock; scenes are
declarative JSON; every animation is deterministic.

## 0. Choosing a visual mode

Per production/channel (Video options -> `visual_mode`):

| Mode | What it does |
|---|---|
| `images` (default) | The original pipeline: stills, camera moves, AI clips. Unchanged. |
| `motion_graphics` | Explainer scenes from `explainer.json`: animated text, counters, bars, blocks, image cutouts (dark themes). |
| `whiteboard` | Explainer scenes drawn stroke by stroke on a light board: illustrations, real handwriting, arrows, annotations, erase/clear. |

An explicit `visual_mode` always wins — the mere presence of `explainer.json`
never flips a production out of image mode (legacy sidecar-only projects are
the one exception: they keep working). `whiteboard` with no sidecar is a clear
error, not a silent fallback.

## 1. The production folder

An explainer production needs exactly:

    <production>\
      audio.mp3            (any audio file, or audio\narration.mp3)
      subtitles.srt        (stage 4 generates this from the audio)
      explainer.json       (the scenes - see below)
      imgtovideo.json      (fps/size; defaults are fine)

No images, no shotlist, no visual manifest. When `explainer.json` is present,
the planner uses it instead of the image/shotlist stages, and the render
pipeline treats each scene as a prepared silent clip.

## 2. Writing explainer.json

    {
      "schema_version": 1,
      "theme": "finance_dark",
      "canvas": {"width": 1920, "height": 1080},
      "scenes": [
        {
          "id": "budget", "shot_id": "S01_01", "cue_range": [1, 5],
          "objects": [
            {"id": "title", "type": "text", "text": "Where does the surplus go?",
             "box": [0.08, 0.10, 0.84, 0.14], "style": "heading"},
            {"id": "surplus", "type": "counter", "from": 0, "to": 150,
             "format": "currency_usd", "box": [0.80, 0.42, 0.12, 0.10], "style": "stat"}
          ],
          "events": [
            {"target": "title", "action": "fade_in",
             "at": {"cue": 1, "edge": "start"}, "duration_ms": 400},
            {"target": "surplus", "action": "count",
             "at": {"cue": 1, "edge": "start"}, "duration_ms": 900}
          ]
        }
      ]
    }

Rules:

- **cue_range [first, last]** is 1-based, inclusive, and ranges must not
  overlap between scenes. Gaps between cue ranges are held by the earlier
  scene (the SRT rarely tiles the narration exactly).
- **box** is normalized 0..1 (x, y, w, h) of the canvas.
- **Events** anchor to `{cue, edge}` with an optional `offset_ms` (negative =
  before the anchor). Every event targets one object of a compatible type.
- The **subtitle-safe band** (bottom 12%) stays caption-free by convention;
  keep important text out of it.

Objects: `text, label, path, bar, counter, grid, blocks, callout, image,
stroke_asset, handwriting`.
Actions: `fade_in, fade_out, slide, type_on, draw, count, grow, reveal,
emphasis, draw_strokes, erase`. Combinations are validated (`draw` needs a
path, `count` needs a counter, `draw_strokes` needs a stroke_asset or
handwriting, ...).

## 1b. Whiteboard specifics

Doc-level: `"style": "whiteboard"` picks the light-board theme
(`whiteboard_light`) unless you name a theme explicitly.

**Stroke assets** (`type: "stroke_asset"`):

    {"id": "house", "type": "stroke_asset", "asset": "builtin:house",
     "box": [0.10, 0.25, 0.30, 0.40], "fill_after_draw": false}

`asset` is `builtin:<name>` (person, house, bank, coin_stack, bulb, calendar,
folder, leaf, tree, wrench, piggy_bank, chart_up, chart_down, bill, check,
circle, underline, arrow_right) or a project-relative JSON file:

    {"viewBox": [100, 100],
     "strokes": [
       {"path": ["M", 18, 46, "L", 50, 16, "L", 82, 46], "width": 5,
        "weight": 1.0, "pause_after": 0.2, "fill": false},
       ...
     ]}

Draw events (`draw_strokes`) budget the duration across strokes **by arc
length** with pauses; drawing is genuine stroke-by-stroke — the pen tip is
visible and erase reverses the strokes. A validator error tells you when a
duration is too short to read (about 120 ms per stroke minimum). Author
centerline strokes, not filled silhouettes; keep character identity stable
across a recurring character. Provenance: built-ins are drawn for this
project; record licensing for anything you import.

**Handwriting** (`type: "handwriting"`): real single-line glyphs (A-Z, a-z,
0-9, `.,!?:;-$%&()/'` + more), drawn in reading order with pen lifts. Missing
glyphs are a validation error — shorten the text or set
`"fallback_typed": true` to render it as typed text instead (disclosed, never
a silent substitution). This is English/Latin-script only in the MVP; no
universal-language claim.

**Erase/clear**: `{"target": obj, "action": "erase", "duration_ms": 500}` —
stroke objects erase stroke-by-stroke in reverse; other objects fade. Plan
one idea at a time: draw the subject, add the relationship, annotate; keep
connected objects on the board and erase the rest before it gets crowded.

## 3. The workflow

In the dashboard: open the production -> **Explainer scenes** panel.

1. Paste/import `explainer.json`. Imports are validated first: a broken file
   never replaces the previous one.
2. **Validate** checks schema AND cue timing against the SRT (human-readable
   errors: scene/object/event named, every time).
3. **Storyboard** writes start/mid/final snapshots per scene to
   `out\explainer\storyboard\` (shown in the panel).
4. **Render preview** writes `out\preview.mp4` with narration.

Command line equivalents:

    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli validate <folder>
    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli storyboard <folder>
    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli render-scene <folder> <scene_id>
    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli render-demo <folder> [--final]

The standard merge/render pipeline also works: `plan_project` detects
explainer.json, prepares the scene clips (content-hash cached in
`out\explainer\`), and `render_final` assembles + muxes them with the
narration. Transitions between scenes use the production's transition options.

## 4. Performance

- Prepared scenes are cached by content (schema version, resolved scene JSON,
  theme, fonts, canvas, fps). A re-render only re-encodes changed scenes.
- Measured on the dev machine (RTX 3050, 16 GB, software encoder): the 4-scene
  27 s demo renders cold in ~58 s, cache-warm in ~11 s.
- Text-only scenes run in the FFmpeg filtergraph; scenes with paths/bars/
  counters/blocks/bills run through the Python frame-pipe (one frame at a
  time, bounded memory).

## 5. Limitations (read before planning a video)

- Cue precision = whisper cue boundaries. Word-level sync is NOT claimed;
  use `offset_ms` to fine-tune individual events.
- Whisper sometimes splits cues mid-phrase; pick scene ranges that absorb it.
- No camera moves in MVP (`camera` is rejected); scenes are static viewports.
- Text wipes that "reveal" pre-drawn text are NOT supported (they fake
  handwriting reconstruction); type_on types real prefixes instead.
- NLE exports (Premiere/CapCut) carry baked clips, not editable layers.
- Fonts: default theme uses Segoe UI / Consolas from C:\Windows\Fonts;
  custom themes name explicit font files (missing files are a validation
  error, not a silent fallback).

## 6. Fonts

Theme fonts (`heading_font`, `body_font`, `mono_font`) resolve in this order:

1. the path **as given** — absolute works anywhere;
2. the repo **`fonts/` folder** — download a font, drop the `.ttf` there, reference it by file name (`"heading_font": "Caveat-Bold.ttf"` or `"fonts/Caveat-Bold.ttf"`);
3. **project-relative** — per-production font files inside the production folder;
4. `C:/Windows/Fonts` (system fonts by bare file name).

Where to get fonts: [fonts.google.com](https://fonts.google.com) — everything there is open license (SIL OFL or Apache 2.0); keep the license file next to the font when you download it.

Good handwriting-style pairings for whiteboard mode (all OFL): **Caveat**, **Shadows Into Light**, **Patrick Hand**, **Kalam**, **Gochi Hand**. Note: handwriting *text objects* (`type: "handwriting"`) draw real strokes and do not use font files — fonts apply to `text`, `label`, `callout` and counters (mono). A font file that cannot be found is a validation error naming every location checked; nothing falls back silently.

## 7. Themes

`finance_dark` (default), `nature_parchment`, `living_warm` ship in
`video/explainer/themes.py`. Add a theme there (data only) and reference it
by name.
