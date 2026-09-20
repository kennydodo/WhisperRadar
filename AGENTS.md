# WhisperRadar — Agent Notes

YouTube channel monitoring + Studio video production pipeline.
Run: `python wr.py serve` (dashboard at http://127.0.0.1:8540). Tests: none formal — verify features over HTTP against a test production (never call paid APIs — Gemini/Renderly/Flow — for testing).
Lint/typecheck: none. Backend: `whisperradar/` (stdlib Flask, SQLite at `data/whisperradar.db`).
Docs: `README.md`. Key surfaces: dashboard/channels/transcripts (`webapp.py` + `dashboard.html`), Studio pipeline (`webapp.py` studio routes + `studio.py` + `templates/studio_detail.html`).

## Feature: Studio "Run till finish" automation (implemented 2026-09)

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
