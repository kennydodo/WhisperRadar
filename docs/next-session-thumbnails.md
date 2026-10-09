# Next session - thumbnail upgrade (branch `feat/thumbnail-upgrade`)

Written 2026-10-09 from a review of the unfinished Qwen/VS Code session. Read
`AGENTS.md` first. Everything here is for whoever continues the branch.

## 1. What the owner wants (his words, condensed)

- Thumbnails are "lacking / not good enough".
- "Our thumbnail should look similar to the original video. If a video has a good
  view, the thumbnail is important." -> the thumbnail must follow what made the
  SOURCE video (and the niche's top outliers) work, not be a generic illustration.
- "Maybe we can adopt what vidIQ is doing."
- Production 23 ("10 Japanese Habits to Keep Your Home Clean and Clutter-Free",
  folder `E:\YOUTUBE\TO LIVE AND MORE\VIDEOS\10 Japanese Habits to Keep Your Home
  Clean and Clutter-Free`):
  - the `art` folder = thumbnails WhisperRadar generated -> "they don't look good";
  - the `thumbnail` folder itself = thumbnails the owner made with GPT -> the quality bar.
- **Local first**: "if we can implement them ourselves, we should do it. If there is
  something we can't do then I will consider MCP." No paid vidIQ dependency.
- He asked "is the MCP free?" - see section 6.

## 2. Where the branch stands

- Worktree: `D:\Repos\WhisperRadar-thumb`, branch `feat/thumbnail-upgrade`, created
  off master `ac33493`. The main checkout `D:\Repos\WhisperRadar` stays on master
  (dashboard and image generation must keep running) - work only in the worktree.
- **Zero commits on the branch.** The only work is ONE uncommitted change:
  `whisperradar/thumbnails.py` (+68 / -2). Master has moved on since `ac33493`
  (webchat fixes etc. - merge or rebase master first).
- Git inside the worktree only works from Windows: its `.git` file points at a
  Windows path (`D:/Repos/WhisperRadar/.git/worktrees/WhisperRadar-thumb`).

### What the uncommitted change does ("hand-drawn emphasis devices")

- New `EMPHASES = ("none", "ellipse", "arrow", "underline")` and `EMPHASIS_RGB`.
- `parse_concepts` reads a new per-concept field `emphasis` (default `none`);
  `_concepts_json` and the writer's JSON schema carry it.
- `_RULES` gets a "make it FEEL like a YouTube thumbnail" rule (bold subject, strong
  facial emotion, saturated colour, high contrast, readable at phone size) and an
  explanation of `emphasis`.
- `compose()` draws the device after the text: `_rough_ellipse` (red double-stroke
  circle on the side opposite the text), `_emphasis` (arrow), `_brush_underline`
  (under the last text line).

### Problems found in it (verified by running the tests)

1. **Bug: `compose()` crashes when a concept has no text.** The new `else:
   _emphasis(draw, concept)` branch runs when `text` is empty, but `draw` is only
   created inside `if text:` -> `UnboundLocalError`. Two existing tests fail because
   of it: `ComposeTests.test_text_changes_the_picture_where_it_is_placed` and
   `ComposeTests.test_wrong_aspect_art_is_covered_not_stretched` (the other 39 thumbnail
   tests in test_thumbnails / _inspiration / _at_review / _auto_api pass).
   Fix: create `draw = ImageDraw.Draw(img)` before the `if text:` branch (or inside
   the else).
2. No tests for emphasis at all (parse default/unknown value, JSON round trip,
   compose with each device with and without text, output size < 2 MB).
3. **No UI.** `whisperradar/templates/thumbs.html` (line ~160 has the "Place"
   select) and the save handler in `whisperradar/webapp.py` (~line 4396, where
   `text_pos` / `color_N` / `accent_N` are read) do not know `emphasis`, so an owner
   cannot see or change it, and saving the form may drop it - check.
4. The ellipse/arrow positions are fixed guesses (centre of the side opposite the
   text). They do not know where the focal object actually is, so on many images
   they will circle nothing. See Phase C.
5. Nothing yet addresses the actual complaint (art quality / similarity to the
   original) - the change only adds decoration.
6. The Qwen chat also mentions a larger "roadmap" ("thumbnails are the priority",
   "data-grounding items"). That roadmap was NOT in the pasted transcript. Ask the
   owner for it before building, or treat Phase D as the reconstruction.

## 3. What already exists on master (do not rebuild)

`whisperradar/thumbnails.py` (970 lines) already has:
- concepts: `writer_prompt` / `judge_prompt` / `run_concepts` (web-chat writer + judge)
  and `run_concepts_api` (Auto Run); `local_faults` rule checks; pass mark setting.
- inspiration: `inspiration_items` / `fetch_inspiration` / `inspiration_paths` - the
  source video's thumbnail first, then the niche's best outliers (multiplier >= 5),
  uploaded into the writer chat (`concepts_job`, merged in `ac33493`).
- art: `generate_art` through FlowBatch or Renderly, `art_prompt_for`, `_style_for`.
- compose: `compose` (cover-crop to 1280x720, gradient, big outlined text, < 2 MB),
  `compose_all`, `contact_sheet`, `set_art`.
- Data: `outliers.py`, `learning.py` (own 24h/7d/28d results feed the writer prompt),
  `packaging.py` (plan/kit), Research tabs. These are the local replacement for
  vidIQ-style data.
Tests: `tests/test_thumbnails.py`, `test_thumbnail_inspiration.py`,
`test_thumbnail_at_review.py`, `test_thumbnail_auto_api.py`, `test_image_thumb_names.py`.

## 4. Work left, in order

### Phase A - land and fix the emphasis work (small)
1. Merge/rebase current master into the branch.
2. Fix the `draw` bug; add the emphasis tests; add an `emphasis` select to
   `thumbs.html` and read it in the webapp save handler; keep the value through
   re-compose.
3. Run the full suite (`python -m unittest discover -s tests`), then commit.

### Phase B - diagnose before building (needs the owner's E: folders)
1. Open production 23's `art` (ours) and `thumbnail` (GPT) folders side by side.
   Ask for read access to the folder or have the owner copy ~6 images from each
   into the repo's scratch area.
2. Write down concrete differences (subject size and crop, face/expression,
   saturation and contrast, background clutter, text size/placement, number of
   elements). Do this BEFORE changing prompts - the fix depends on the cause
   (art prompts vs engine vs compose vs concept choice).
3. Keep that note in this file under "Findings".

### Phase C - look like the original / the niche winners (local)
Everything below is local (Pillow/numpy, no paid service):
1. **Thumbnail DNA extractor** for the source video and each outlier thumbnail:
   dominant palette, brightness/contrast, saturation, edge density (clutter), how
   much of the frame is the main subject (largest salient blob), subject side,
   amount and size of text, presence of a face. Store as JSON next to the
   inspiration images and feed a short summary into `writer_prompt`
   ("the winners use X; keep Y").
2. **Use the source thumbnail as a reference for the art engine** where the engine
   supports reference images (FlowBatch refs, Renderly `--ref-asset`), so the art is
   composed like the original in the channel's own style instead of only described.
3. **Local thumbnail scorer** used in `local_faults` and to pick between variants:
   contrast, saturation, clutter, text legibility rendered at 168x94 (mobile
   size), subject-to-frame ratio, safe area (bottom-right timestamp overlay). Generate
   2-3 art variants per concept and keep the best-scoring one.
4. **Mobile-size preview** in the thumbs page (168x94 and 360x202) so the owner sees
   what viewers see.
5. Make emphasis devices smart: place the ellipse/arrow on the detected focal
   blob instead of fixed positions; skip them when nothing salient is found.

### Phase D - "what vidIQ does", implemented locally
Reconstruct the list with the owner first (the roadmap was not captured). Likely:
thumbnail ideas from a title/keyword, a thumbnail score, CTR-oriented advice, outlier
analysis. WhisperRadar already has the data layer (outliers, results, learning);
what is missing is surfacing it on the thumbs page ("why this should work": matched
winner traits, score breakdown). Anything that truly needs vidIQ's own proprietary
numbers (keyword volume / competition scores) is the only place an MCP is worth
considering - see section 6.

### Phase E - finish
Full test suite, update `AGENTS.md` (thumbnail section) and this file, then the owner
merges `feat/thumbnail-upgrade` into master.

## 5. Rules for this branch

- Work only in `D:\Repos\WhisperRadar-thumb`; never in the running main checkout.
- Tests: `python -m unittest discover -s tests` (unittest, no network, temp DBs).
  LF line endings in the repo (`.gitattributes`).
- Do not print `.env` or key files. Do not push or merge unless the owner says so.
- No paid service as a dependency. Prefer small, tested modules (e.g. a new
  `thumb_analysis.py`) over growing `thumbnails.py` further.

## 6. Is the vidIQ MCP free? (answer given to the owner)

vidIQ advertises its Claude connector at <https://vidiq.com/claude> as "Free for a
limited time", with six tools that each cost 5 credits per call, and it requires a
vidIQ account to authorise. The page does not give the end date, the credit
allowance, or whether a paid plan is needed afterwards - so it cannot be assumed to
stay free. Treat it as optional and last; build the local version first.

## 7. Open questions for the owner

1. Paste the roadmap Qwen mentioned (the "data-grounding items").
2. Can the next session read the production 23 `art` and `thumbnail` folders on E:?
3. Which channel's look is the first target (the one in production 23)?
4. Should a failed/low-scoring concept auto-regenerate art, or wait for a click?

---

## Status update (branch `feat/thumbnail-upgrade`, master merged in)

Done: emphasis tests; `focal` field + placement (ellipse/arrow at the focal
point, arrow tail toward picture centre); writer anchored to the ORIGINAL
thumbnail's composition/palette; judge rule for attention devices; UI selects
(attention device, points at); contact sheet shows composed finals;
`phone_metrics()` local phone-size check (contrast/colour/brightness warnings
shown on the thumbnails page); plan title must be a vetted candidate (snaps
near-matches, fault + `apply_plan` refuses otherwise; a title typed on the page
is an explicit override); VPH (`vph`, `breakout`, sorts "views / hour" and
"breaking out now", badge); curated niche chips on Research (`niche_chips.py`,
local filter, no quota).

Still open (needs the user): engine/credits decision for art quality; run
production 15 images with the logged-in Chrome; verify webchat sign-in and the
Renderly upscaler on the user's machine; Platform trending feed, keyword
demand, visual-similarity ranking, best-time-to-post, Shorts clipping,
WhisperRadar-as-MCP are unstarted (Qwen's list A.3-A.8). Then: merge the
branch into master and `git worktree remove` the worktree.

### Update 2 - Qwen's vidIQ list (A.3-A.7) done locally, MCP deliberately skipped
- A.3 Trending feed: Research > Trending (`youtube_api.trending`, `trending.py`;
  ~101 quota units per search, result cached in trending.json).
- A.4 Keyword demand: `demand.py` (autocomplete + niche title counts -> 0-100
  score) on the Keywords tab. A local estimate, not a search volume.
- A.5 Visual similarity: `thumbnails.visual_similarity/rank_by_similarity`;
  the writer is shown the pooled winners closest to the original.
- A.6 Best time to post: `timing.py` (UTC weekday/hour by median multiplier).
- A.7 Shorts: `shorts.py` + /studio/<id>/shorts (picks 20-58 s windows from the
  subtitles, cuts 9:16 with blurred background + burned-in captions; checked with
  real ffmpeg). A.8 (MCP server) intentionally not built.
