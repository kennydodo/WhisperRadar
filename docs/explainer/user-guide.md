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
circle, underline, arrow_right), `lucide:<name>` (about 2000 line icons - see
"Icon library" below) or a project-relative JSON file:

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

**Icon library** (`lucide:<name>`): the Lucide line-icon set (v1.64, ISC
license - `icons/LICENSE-lucide.txt`; checked in as `icons/lucide.json`, so no
Node or extra Python packages are needed). Each icon is drawn stroke by stroke
like any other asset, 5 units wide in the 100x100 asset box.

    {"id": "pig", "type": "stroke_asset", "asset": "lucide:piggy-bank",
     "box": [0.43, 0.26, 0.14, 0.25]}

Find names by word or meaning (every word must match a name or a tag):

    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli icons server
    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli icons savings
    .venv\Scripts\python.exe -m whisperradar.video.explainer.cli icons arrow down

Names are forgiving about case and spaces (`lucide:Piggy Bank`); a name that
does not exist is a validation error that lists the closest ones. Keep icon
boxes roughly square in pixels (a 0.14 x 0.25 box on a 1920x1080 canvas is
269 x 270 px) - the icon is scaled to fit and centred in its box. To refresh
or extend the set: `npm install lucide-static`, then
`python scripts/build_lucide_library.py <path>\node_modules\lucide-static`
(needs `pip install svgelements`; only that script uses it).

**Chalkboard theme**: `"theme": "chalkboard"` (with `"style": "whiteboard"`)
draws chalk-white lines on a near-black board, with a light-blue accent for
`"style": "accent"` objects. The light board remains the whiteboard default.
A worked example with the same narration as the light-board demo:
`docs/explainer/demo/chalkboard-demo/`.

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

### Colours and hand-drawn shapes (whiteboard / chalkboard)

Every object takes `"color"`: a chalk name (`white yellow green red orange blue
pink purple grey`), a theme role (`accent positive negative muted`) or `#RRGGBB`.
The names pick brighter chalk on dark boards and darker ink on light ones.

`"type": "shape"` draws hand-wobbled geometry from a name, so a script generator
never writes coordinates. The wobble is seeded from the object id (same id, same
drawing). Use `draw_strokes` to draw it and `emphasis` to make it glow.

| kind | placed by | options |
|---|---|---|
| `card` | `box` | `filled` (dark inside) |
| `oval` | `box` | |
| `underline` | `box` (bottom edge) | |
| `highlight` | `box` | `opacity` (default 0.38) |
| `brace` | `box` | `side`: `right` or `left` (tip direction) |
| `arrow` | `points` `[[x1,y1],[x2,y2]]` | `curve` -1..1 |
| `line` | `points` | `dashed` |
| `check`, `cross` | `box` | |

Demo: `docs/explainer/demo/chalkboard-shapes/` (an original timeline scene).

### Recipes: write a short plan, get the whole explainer.json

Instead of placing every object, write `plan.json` next to `subtitles.srt` and run

    python -m whisperradar.video.explainer.cli build <production folder>

It places everything, picks colours, schedules the pen cue by cue (compressing
the drawing speed when needed, never below readable) and writes `explainer.json`,
then validates it. Same plan + same subtitles gives the same file. Layouts:
`row`, `steps` (numbered), `timeline`, `stack`, `hero`, `compare` (two sides with
check/cross points). Options: `title`, `footer`, `connect` (`arrow`/`dashed`/`none`),
`cards`, `focus` (dim the previous item as the next one appears). Each item takes
`label`, `icon` (a Lucide name), `color`, `note`, `cue`; items without a cue are
spread over the scene. Too much for the time gives a readable error
("use fewer items, or earlier cues"). Demo: `docs/explainer/demo/recipes-demo/`.

### Rendering inside WhisperRadar

A production folder with `plan.json` or `explainer.json` is an explainer production.
In the Studio its shot list, references, images and motion stages are skipped (also by
auto-run). On the **Render** stage the button **Render explainer video** builds
`explainer.json` from `plan.json` + the subtitles when the plan is newer, draws every scene
(finished scenes are cached, so a re-render only redraws what changed), joins them, mixes in
the narration and writes `out/final/final.mp4`. Progress shows per scene in the job log.
Same code as `cli render-demo` (`video/explainer/render.py`). Plans can anchor scenes and
items to the narration by quoting words (`"start"`, `"at"`) instead of cue numbers.

### Scene plan: writer, checker, judge

On an explainer channel the **Shots** stage becomes the **scene plan**. The writer (web-chat site or API
provider, like the other stages) drafts `plan.json` from the narration, quoting the words where each scene
and item appears. The app finds those words in the subtitles and runs the same builder/validator as render
(words not in the narration, scene too crowded for its time, unknown icon, first scene not at the start):
faults go back to the writer as a short list. When the code is satisfied a **judge** (a different model)
scores the teaching (right picture for the sentence, nothing invented, short labels, variety, pacing,
colour meaning) and its faults/fixes go back to the writer, up to 5 rounds; the best plan is kept as
`plan.json` with the verdict in `plan_review.json`. Code: `whisperradar/explainer_scenes.py`. Auto-run does
this stage too (`autorun._run_scenes`).

### Colours and fonts (channel settings)

Channel settings (My channels > Channel type & explainer; also Video options) hold: heading colour, text
colour (a chalk colour name or `#RRGGBB`) and board colour (`#RRGGBB`), with a live preview. Recipes draw
titles in the colour name `heading` and labels in `text`, which resolve to the channel's colours (chalk
yellow / white by default). Scenes already drawn keep their cache until the look changes.
Fonts shipped in `fonts/` (all OFL, licences in `fonts/licenses/`): Gaegu, Patrick Hand, Kalam, Caveat,
Architects Daughter, Shadows Into Light, Indie Flower, Gloria Hallelujah, Handlee, Neucha, Covered By Your
Grace, Comic Neue, Amatic SC, Delius.

### Where the look is set: channel settings (saved in the database)

Theme and fonts are **channel settings**, not file settings: open
**Video options** (global, or a channel's own page) and use the **Explainer look**
group: *theme*, *hand font*, *heading font*, *body font*. They are saved in the
database (`video_options`, per own channel over global) and written into each
production's `imgtovideo.json` (`explainer` section), which the explainer reads.
Blank = inherit. A production's own `explainer.json` `theme` / `fonts` still
wins for that one production, so a script generator never has to set them.

### Spotlight: dim and undim

`{"target": "card1", "action": "dim", "at": {...}, "duration_ms": 400, "amount": 0.7}`
fades a drawing, shape or handwriting back (amount 0.1 to 0.95); `undim` brings it
home. Dim everything except the thing being talked about.

### Handwriting: real hand fonts or the pen strokes

`handwriting` objects write in one of two ways. Each channel (or object) picks:

- **A real hand font** (`.ttf`): the text writes on left to right, line by line,
  with a pen nib at the writing edge, and rubs out in reverse. No glyph limit
  (`=`, `$`, accents work). This is the default on the `chalkboard` theme.
- **`"strokes"`**: the built-in pen-stroke letters (limited character set, each
  letter drawn stroke by stroke).

The channel setting above is the normal way. To override one production, use
`explainer.json`:

```json
"fonts": {"hand": "Caveat-Bold.ttf", "heading": "Gaegu-Bold.ttf", "body": "Gaegu-Bold.ttf"}
```

`"hand": "strokes"` selects the pen strokes. A single object can override with
`"font": "Kalam-Bold.ttf"` or `"font": "strokes"`. Fonts are found in the repo
`fonts/` folder or the production folder. Shipped (all SIL OFL, see
`fonts/licenses/`): Gaegu-Bold, PatrickHand-Regular, Kalam-Bold, Caveat-Bold,
ArchitectsDaughter-Regular, ShadowsIntoLight-Regular. Drop any other `.ttf` in
`fonts/` and name it. Brace `side` may also be `top` or `bottom` for horizontal
braces.


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

`finance_dark` (default), `nature_parchment`, `living_warm`, `whiteboard_light`
and `chalkboard` ship in
`video/explainer/themes.py`. Add a theme there (data only) and reference it
by name.

## Remotion scenes (optional, experimental)

Most scenes are drawn by the built-in pen renderer. A scene can instead use the
Remotion backend for things the pen cannot do. Today there is one component:

```json
{"id": "fit", "start": "now the same seven", "layout": "fit_chart",
 "title": "What fits in 24 GB?", "limit": 24, "unit": "GB", "bits": 4,
 "items": [{"label": "7B", "params_b": 7, "at": "..."}, {"label": "70B", "params_b": 70, "at": "..."}]}
```

Sizes are calculated (parameters x bits / 8) and bars past the limit turn red.
Setup, once: install Node.js, then run `npm ci` in `tools/remotion`. Remotion is
free for teams of up to three people; check its licence if that changes.
Clips are cached separately in `out/remotion`, Stop works as for other scenes,
and normal channels are unaffected. Remotion scenes render at 1920x1080 or at
half size (960x540).
