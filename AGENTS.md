# WhisperRadar — Agent Notes

YouTube channel monitoring + Studio video production pipeline.
Run: `python wr.py serve` (dashboard at http://127.0.0.1:8540). Tests: none formal — verify features over HTTP against a test production (never call paid APIs — Gemini/Renderly/Flow — for testing).
Lint/typecheck: none. Backend: `whisperradar/` (stdlib Flask, SQLite at `data/whisperradar.db`).
Docs: `README.md`. Key surfaces: dashboard/channels/transcripts (`webapp.py` + `dashboard.html`), Studio pipeline (`webapp.py` studio routes + `studio.py` + `templates/studio_detail.html`).

## Feature: Studio "Run till finish" automation (implemented 2026-09)

### ALSO NEXT SESSION — OpenSpeaker favorites for the voice picker (user approved)

Goal: dropdown shows the voices the user starred in OpenSpeaker. API exists
(found in the app JS bundle, same base `https://api.ai33.pro` + `xi-api-key`,
NOT in published docs - verify with one read-only GET first):
- `GET /v3/favorites?provider=<p>` -> `{favorites: [{voice_data: {voice_id, name, ...}}]}` (try without provider for all)
- `POST /v3/favorites` `{provider, voice_id, voice_data}`; `DELETE /v3/favorites/<id>`; `POST /v3/favorites/bulk`
Plan: in `ai33.voices()` add a mode when `studio.ai33_voices == ["favorites"]`
(or a new config flag): fetch favorites, normalize with `_normalize`, keep
config-order/curated fallback; UI option "Sync favorites into shortlist".
Current curated mechanism lives in `ai33.curated_ids` / `_resolve_curated`.

### NEXT SESSION HANDOFF (2026-09-21, ~13:00) — driver rebuilt; blocked by Google abuse filter

Root causes found & FIXED in Renderly\extension-v2\flow.js (all proven live):
1. Trigger never clicked: synthetic Enter + `clicked: true` without a click.
   Now: trusted typing (click composer coords + Ctrl+A + type via Playwright),
   then trusted click on the composer arrow (aria-label exactly "Start
   generation", 90s enabled-wait), then trusted Enter fallback; every trigger
   is VERIFIED (busy or fresh result) and failures dump a button inventory +
   screenshot (trigger-fail.png / timeout-<card>.png in extension-v2).
2. Harvester discarded real results: finished images render as
   flow.google.com/asb/... URLs now; isFinalResultUrl accepts both hosts.
3. URL identity is unreliable: Flow re-signs tile URLs (whole-grid re-sign =
   24 "fresh" URLs) and old tiles load full-res variants progressively.
   Freshness is now CONTENT-based: every candidate is fetched, sha1-hashed
   (sessionHashes), skipped if seen; PLUS ownership check - a candidate is
   only adopted when its tile's prompt-derived aria-label contains the
   prompt's first 30 chars (H.tileLabelForSrc). Misattributions (house/
   living-room plates saved as card images) came from exactly this.
4. Echo-cure killed cards: refill+retrigger on echo aborted running
   generations and their results got swept as seen and lost. Echoes are now
   ignored; one late re-trigger at 60s only when submit is idle-enabled.
5. generationBusy false-busy: disabled submit == idle-empty composer too;
   now requires composer text.
6. Stale ingredient chips survive cards/runs and hijack generations
   (rendered BG_LIVING_ROOM instead of the prompt - user had noticed "more
   refs in the composer than the array"): H.clearComposerChips runs before
   every fill and runVersion HARD-FAILS if chips remain (chip count > the
   card's refs array must be impossible). --refs-mode none added for
   stepwise testing (run flow.js directly, no channel => no import).
7. Policy/abuse rejections detected via H.policyRejected (the Failed panel);
   2 auto-retries then a clear failure. NOT YET RELIABLE LIVE (see below).

THE REMAINING WALL - NOT CODE: Google is anti-automation-blocking the
driver's account/profile. Screenshot proof (extension-v2\trigger-fail.png):
"Failed - We noticed some unusual activity. Please visit the Help Center...
You have not been charged." Earlier failures said "might violate our
policies" - same soft-block, other flavor. Every generation attempt gets
rejected pre-charge; the submit arrow stays disabled; composer input is
dropped. All of today's 0/85 runs were this, not the (now fixed) code bugs.
Verified working end-to-end when a generation DOES pass: prompt -> trigger
-> content-hash harvest -> save (NOREF_S01_01.png in
C:\Users\Kehinde\AppData\Local\Temp\kilo\noref-test).

NEXT STEPS (user agreed, one at a time):
1. Wait out the abuse block (hours/day), then test step 1 = prompt only
   (no refs): node flow.js --file <one-card-batch> --out <tmp> --refs-mode
   none. Manual test in the same automated Chrome first to see if manual
   passes while automation doesn't. If manual also blocked: Help Center
   appeal. Test batch: Temp\kilo\noref-test\batch.json (S01_01 prompt).
2. Step 2 = exactly ONE ref; step 3 = 2-3 refs. Chip count must equal the
   refs array length - assert before trigger (already hard-fails on extras).
3. Only then restart the WhisperRadar auto-run for production 4 (85 cards,
   work_dir E:\YOUTUBE\PERSONAL FINANCE\These 10 Things At Home Worth
   Serious Money; images dir currently EMPTY). Old handoff notes below.

### PREVIOUS HANDOFF (2026-09-21 ~09:00) - superseded by the above

State: production 4 ("These 10 Things", work_dir E:\YOUTUBE\PERSONAL FINANCE\These 10 Things...)
is re-rendering all 85 images from the current shotlist.json. A batch with the latest
flow.js was restarted ~08:40 local and left running. VERIFY FIRST: /studio/job on :8540,
then VIEW the newest images in images\ next to their shotlist prompts (I read the .png
files directly - the mismatch was visual, not metadata).

The bug that was killing renders: gallery asset previews (uploaded ref plates -
BG_CLOSET/ KITCHEN/ LAUNDRY etc., 5504x3072) mount in the ingredient picker as fresh
flow-content.google URLs, indistinguishable from generation results by host alone.
flow.js adopted them as card results → plates/ref-sheets saved under card names
(S01_02 was literally Maya.png upscaled). Fixes now in extension-v2/flow.js:
1. Refs pre-uploaded ONCE per batch before any card (ensureRefsInGallery, chunked
   drops of 3 - 11 full-res files in one drop crashed the tab).
2. sessionSeen set: a flow-content URL is acceptable as a result exactly once per
   session; baseline-swept after load, after preupload, and on every panel open
   (harvestAssetPanel - panel tiles are gallery assets, never results).
3. Ingredient-echo guard in runVersion: if the candidate image's dimensions match ANY
   batch ref file (imageDimensions helper), it is an echo - mark seen, refill prompt,
   re-trigger (an echo can fake "auto-generation already started" and skip the real
   trigger), full timeout window restarts. Max 2 echo cycles per card.
4. waitForIdle sweeps straggler results of failed cards into sessionSeen.
NOT YET VALIDATED LIVE: fix 3's refill+re-trigger path (the batch was restarted right
after implementing it). If echoes still win: check the driver log (8030/api/status),
and consider comparing pixel data, not just dimensions.

Also this session: images stage re-reads shotlist.json mid-batch (round loop in
run_imagegen_flow - a shotlist edit stops the batch and restarts from the new plan);
start-over endpoint (style/script/images scopes; audio only with the checkbox);
merge guard pauses auto-run on ANY unrendered shotlist image (sanitize would drop
them forever); fast-stop cancels the Flow batch mid-images-stage; Flow Driver as
default image source; harness at C:\Users\Kehinde\AppData\Local\Temp\kilo\
wr_autorun_test\run_tests.py (64/64 + T7 mid-stage cancel + T8 merge guard + T9
shotlist hot-reload, with a mock driver on :8050 and real-DB-untouched assertion).
REMOVED as landmines: extension-v2\shotlist.json + prompts.json (generate.bat
fallbacks pointed at other productions' plans) - generate.bat also hardcodes 2 old
character refs on every card, don't use it for these renders. NOTE: driver-page runs
render ALL images (no skip-existing) - always render through WhisperRadar.


Goal: one button on the Studio production page that automatically executes all remaining
pipeline stages in order, so the user can do manual steps (e.g. upload audio) mid-pipeline,
click the button, and everything after runs by itself.

Where it lives: `whisperradar/autorun.py` holds every stage runner (used by BOTH the
manual routes in `webapp.py` - thin wrappers now - and the pipeline runner, so
single-stage and auto-run behavior always match). `run_stage()` returns
"ok" | "paused:<reason>" | "failed:<error>"; `_Paused` marks missing manual input.
Routes: `GET /studio/<pid>/auto-run/plan` (dry-run JSON for the confirm modal),
`POST /studio/<pid>/auto-run` (start/resume), `POST /studio/<pid>/auto-run/stop`,
and `POST /studio/<pid>/start-over` (reset from style/script/images: deletes that
stage's generated artifacts + step history, keeps inputs like bible/refs/versions).
The runner re-evaluates each stage when it is reached (a pause planned earlier can
resolve itself mid-run, e.g. shots created the shotlist). The production pointer is
advanced past completed stages and lands on review - review is NEVER auto-approved.
UI: header button + confirm modal (with inline fixes for pauses) + Start over
dialog + Stop/Resume in `templates/studio_detail.html`; `/studio/job` exposes
`stage`, `pipeline`, `paused`. Auto-run pauses at merge when most shotlist images
are unrendered instead of letting sanitize_shotlist shrink the plan.

### UX design (agreed with user)
- Button **"▶ Run till finish"** lives at the TOP of the production page (header row, next to
  the stage stepper — it is a page-level action, not a stage action). User suggested top; confirmed good.
- Clicking it shows a confirm modal listing exactly what will run from the current stage:
  stage list, which hooks will be used (TTS / Renderly API / Flow Driver incl. channel,
  project, refs, upscale from the saved flow settings), a credits warning for the images
  stage (N images × Renderly/Flow cost), and that merge can take a long time.
- Pipeline always **stops before review** — publishing stays a human decision. After merge,
  status shows "Ready for review".
- Any failure stops the run with the normal job error banner; the button becomes "Resume".

### Stage semantics (skip if already done, pause if manual input is missing)
- style: LLM from source transcript. No source → skip if style.md exists, else PAUSE ("needs source video or manual style").
- script: LLM generate (skip if script.md exists — manual scripts count as done).
- audio: if audio file exists → done. Else TTS hook if configured → run. Else PAUSE ("upload audio, then Resume").
- srt: Whisper alignment (auto).
- shots: manifest-brief shotlist via LLM (auto; needs script + srt). The updated
  ImgToVideo manifest-authoring-brief has a BIBLE GATE: the LLM refuses to plan
  without a character/reference bible, so shots PAUSES (auto-run) / errors
  (manual route) when pdir/bible.md is missing. The brief now outputs
  shotlist.json FIRST and the batch sheet second - parse_shotlist_output is
  already order-agnostic. Per-image "refs" entries pass straight through
  prepare_flow_batch to flow.js, which resolves bare names against pdir/refs.
- images: render missing shotlist images via the saved render_mode, defaulting to
  the Flow Driver (falls back to Renderly API only when the Flow Driver is not
  installed) + saved flow_channel/flow_project/upscale — reuse the images-stage form fields.
- merge: ImgToVideo hook render (long; needs audio, srt, images).
- review: STOP — human approves.

### Implementation plan (in order)
1. **Refactor stage jobs into reusable functions**: the studio route workers are closures;
   extract each stage's execution into `studio.py` (or a new `whisperradar/autorun.py`)
   functions like `run_stage(db, cfg, pid, stage, log, params)` returning
   ("ok" | "paused:<reason>" | "failed:<error>"). Keep the HTTP routes as thin wrappers so
   single-stage behavior is unchanged.
2. **Runner**: extend the `_Job` infra (webapp.py) with a pipeline mode: `POST
   /studio/<pid>/auto-run` validates the plan (dry-run list of stages + pauses) and starts a
   daemon thread that loops: pick next not-done stage → run_stage → on "ok" continue, on
   "paused" stop with a resumable marker (store the plan in-process on the job object like
   sjob; restart of the server cancels the run — acceptable for a local tool).
   `POST /studio/<pid>/auto-run/stop` sets a cancel flag checked between stages.
3. **State/observability**: job log lines like `[auto-run] stage 3/5: srt — started`; the
   dashboard poll (`/studio/job`) already streams sjob.log — add `stage` and `pipeline`
   fields to the status JSON. No DB schema changes needed (stage done-ness stays in
   production_steps).
4. **Frontend (studio_detail.html)**: header button + confirm modal (stage list, credits,
   pauses); "Stop" button while running; paused state renders the manual instruction +
   "Resume" re-arming the same pipeline. Buttons disabled while any sjob runs (existing
   job.running guard covers it).
5. **Guards**: refuse to start when a job is already running; refuse stages whose manual
   prerequisites are missing (plan shows them as pauses); images stage only runs when a
   shotlist exists (it will — shots precede it); never auto-approve review.
6. **Testing over HTTP** (no paid calls): create a test production, mock/pause points —
   verify stage skipping, pause-and-resume on missing audio, stop mid-run, and that the
   images stage is never reached without a shotlist. For a paid-API smoke test, ask the user.

Notes: settings for upscale/auto-download already exist in Renderly; flow fields come from
the images-stage form — persisting them per production is optional polish.
