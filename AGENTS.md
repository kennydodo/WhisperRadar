# WhisperRadar — Agent Notes

YouTube channel monitoring + Studio video production pipeline.
Run: `python wr.py serve` (dashboard at http://127.0.0.1:8540). Tests: none formal — verify features over HTTP against a test production (never call paid APIs — Gemini/Renderly/Flow — for testing).
Lint/typecheck: none. Backend: `whisperradar/` (stdlib Flask, SQLite at `data/whisperradar.db`).
Docs: `README.md`. Key surfaces: dashboard/channels/transcripts (`webapp.py` + `dashboard.html`), Studio pipeline (`webapp.py` studio routes + `studio.py` + `templates/studio_detail.html`).

## Feature: Studio "Run till finish" automation (implemented 2026-09)

### BACKLOG — fully automated producer (user approved design 2026-09-21; BUILD ONLY WHEN the Google Flow abuse-block is resolved or with renderly default)

One button/schedule: WhisperRadar looks at followed channels, picks a topic
from each, and creates + runs a production by itself. All pieces exist except
orchestration:

1. PICK: per active channel, newest transcribed video not already a
   production source (source_video_id NOT IN productions).
2. CREATE: LLM-crafted title (fallback video title), genre = channel's genre
   (reuse POST /studio/new logic + db.create_production).
3. SEED: per-genre default bible.md + refs/ copied into the production
   (without a bible auto-run PAUSES at shots - blocker for unattended runs).
4. DEFAULTS: narration voice + render_mode per production (see settings below).
5. QUEUE: sequential auto-runs (job system is single-flight), daily cap
   (e.g. per_day 1-2) as a cost guard - images cost credits.
6. TRIGGER: `python wr.py produce` (scheduler) + "Produce from channels"
   button on the Studio page.
7. Review stays human (never auto-approve).

USER REQUIREMENTS (their words, expanded):
- PER-CHANNEL settings: each channel has its OWN default voice (e.g. Alicia
  Invest videos use one voice, InkExplainer another) - all settable.
- A SETTINGS PAGE in the dashboard that persists all of this into the DB
  (new settings storage: channel columns like default_voice, and/or a
  key-value settings table; producer config: per_day cap, default render
  mode, per-genre bible/refs folders).
- "We still need to iron out so many things later" - treat details as open;
  confirm with the user before building (schema, UI layout, topic-pick
  logic: newest vs LLM-chosen best topic).

Image-stage reality check when building: Flow driver was abuse-blocked by
Google (see handoff above); unattended runs should default to Renderly API
(headless, credit cost) until Flow is stable, or make render_mode a
per-channel setting on the settings page.

### Settings page: own channels + global Auto Run criteria — DONE (2026-09-22)

Two pages, both on the normal dashboard port (no extra service):
- `/settings` - global Auto Run criteria only.
- `/my-channels` - the channels the USER publishes on, with add/edit/remove
  and a per-channel Renderly sync.

`channels` (Dashboard, unchanged) = competitor/source channels you monitor for
ideas. `own_channels` (My Channels, new) = the channels you publish on.

Schema (all in WhisperRadar's DB - Renderly keeps its own `renderly.db`; we
only store soft references, never its tables):
- `own_channels`: name (COLLATE NOCASE UNIQUE), description, genre,
  youtube_handle, active, default_voice, default_engine, default_render_mode,
  default_upscale, bible_dir, refs_dir, autorun_enabled, per_day, topic_pick,
  renderly_channel_id, renderly_channel_name.
- `settings` (key/value TEXT): global criteria, spec-driven.
- `productions.own_channel_id` (nullable, migration in `_migrate`).

Code:
- `whisperradar/settings.py` - SPEC drives the page, load() returns typed
  values, save() coerces/validates (int clamps, HH:MM check, seed_dirs parsed
  from "genre = folder" lines). Absent keys are left untouched.
- `db.py` - own-channel CRUD + settings get/set/all.
- `studio.py` - `renderly_channels()`, `resolve_renderly_channel()`,
  `renderly_channel_status()`, `sync_renderly_channel()`. `ensure_renderly_channel`
  now delegates to the resolver (legacy "whisperradar" channel).
- `webapp.py` - `/settings` + `/settings/save`; `/my-channels` +
  `/my-channels/{add,edit,sync,remove}`; `templates/settings.html` +
  `templates/channels.html`; nav link "My Channels" + "Settings" on all pages.

MIRROR RULES (agreed with the user - do not break these):
1. NAME is the identity; `renderly_channel_id` is only a cache. Resolve:
   stored id still exists -> adopt by name -> create (only when create=True).
   This self-heals stale ids and channels made by hand in Renderly.
2. Never block a save on Renderly: if it is down the channel is saved
   unlinked and the page shows "Renderly unreachable". Link lazily.
3. No reverse sync engine. Adopt by name; do not mirror Renderly -> WR.
4. NEVER auto-delete in Renderly. Removing an own channel clears the link only
   (Renderly's channel delete cascades assets/generations/storage).
5. Case-insensitive uniqueness in WR (Renderly's is effectively case-sensitive).
6. Renames: the mirror name is STABLE (`renderly_channel_name`), so renaming
   an own channel never moves/renames the Renderly channel.

STILL OPEN (next steps, in order):
1. **Prompt-length bug on the Renderly Flow path (NOT fixed yet).** The driver
   prepends the shotlist `style` to every prompt (`extension-v2/flow.js:155-159`
   + `:222-224` reads `style` as the master), and the real style is 4000 chars,
   so every card is sent ~4400 chars - over Flow's ~2450 ceiling, which Flow
   refuses with the SAME message as rate limiting. FlowImagesGen dodges this
   because we omit the style there; the Renderly path still does not. This is
   very likely the cause of the recent Flow batch failures.
2. Notifications when an unattended run pauses/fails (a 3am pause goes
   unnoticed otherwise).

### Per-stage service management — DONE (2026-09-22)

`whisperradar/services.py` (`MANAGER`) owns the external services the images
stage needs. `services_for(engine, mode)` says what a run requires:
renderly+flow -> backend + Flow Driver; renderly+api -> backend;
flowimagesgen -> nothing (a CLI that starts/stops its own browser).

Conservative rules - keep them:
- A service that already answers is YOURS: never tracked, never stopped, so a
  Renderly/Flow Driver you started by hand is safe.
- Only processes this module spawned are stopped, and only when
  `services_managed` is on (Settings, default OFF).
- The Renderly backend is shared and is deliberately never killed (`release`
  logs "leaving the Renderly backend running"); only the Flow Driver is stopped
  with `taskkill /T /F`.
- Starting Renderly prefers a direct
  `backend\.venv\Scripts\python.exe -m uvicorn main:app --port <port>`
  (trackable, no stray consoles) and falls back to start.bat, which is marked
  UNMANAGED because killing it would close windows you may be using.
- `studio.ensure_flow_services` now just delegates to `MANAGER.ensure`, so
  there is one implementation.
- `_run_images` wraps the engine calls in try/finally and calls
  `MANAGER.release(cfg, managed)`.

### Per-channel Flow project URL — DONE (2026-09-22)

`own_channels.flow_project_url` (nullable, added by
`db._add_column_if_missing`). Resolution order for the FlowImagesGen engine:
the images-stage field, then the channel's URL, then the global
`studio.flowimagesgen_project_url`; when all are empty the job simply omits
`projectUrl` so FlowImagesGen falls back to its own `config/settings.json` or
Flow's most recent project. Editable on My Channels, and the images stage
shows a URL box only while the FlowImagesGen engine is selected.

### Unattended-run notifications — DONE (2026-09-22)

`whisperradar/notify.py`, called from `producer._notify_outcome` at the end of
every `producer.run`. An unattended run that pauses at 3am is useless if nobody
hears about it.

Targets (both optional, configured in Settings, read fresh each time):
- **desktop**: a Windows balloon tip via PowerShell NotifyIcon - no modules
  needed (`notify.desktop_command`); fired detached with CREATE_NO_WINDOW.
- **webhook**: `notify_webhook_kind` = discord | slack | ntfy | json.
  ntfy takes the raw message + a Title header; the others POST JSON.

Policy: always notify on paused/failed; notify on success only when
`notify_on_success` is on. A run that finishes cleanly but produces nothing
reports why (e.g. "daily cap reached"). `notify.notify` and the whole
`_notify_outcome` are best-effort - a failing notification is logged and
swallowed so it can never fail a production. Settings shows the active targets
as a status line.

### Auto Run scheduler — DONE (2026-09-22)

`whisperradar/scheduler.py` - a thin ticker thread started by `create_app`.
Every 30s it checks `scheduler_enabled` + `scheduler_interval_minutes`, then
asks `producer.build_plan()` whether anything is runnable. It owns NO policy of
its own (no window/cap logic duplicated), so it can never disagree with the
Studio button. Runs go into the studio job slot, so a scheduled run and a
manual one cannot overlap and progress shows in the UI.

- Settings: `scheduler_enabled` (off by default) and
  `scheduler_interval_minutes` (60, min 5). Status (last attempt, last result,
  next run, whether a job is running) is shown on the Settings page.
- Runtime state lives in the settings table under `scheduler_last_attempt` /
  `scheduler_last_result` - NOT in the SPEC, so they never appear as fields and
  `settings.save` never touches them.
- `Scheduler.tick(now)` takes an explicit clock so tests drive it
  deterministically instead of waiting an hour.
- With the dashboard closed, use Windows Task Scheduler instead (it still
  honours the run window and caps, so a frequent trigger is safe):
  `schtasks /Create /TN "WhisperRadar Auto Run" /SC MINUTE /MO 30 /TR "\"D:\Repos\WhisperRadar\.venv\Scripts\python.exe\" \"D:\Repos\WhisperRadar\wr.py\" produce"`

### FlowImagesGen as a second image engine — DONE (2026-09-22)

`default_engine` (global in Settings, per channel on My Channels, overridable
on the images stage) now actually switches the images stage:
- `renderly` - the existing path (Flow Driver or Renderly API + Renderly
  upscale), unchanged.
- `flowimagesgen` - the standalone Playwright Flow CLI, consumed IN PLACE
  from `studio.flowimagesgen_repo` (never vendored: its Google session lives
  in a gitignored `profile\`, and a fresh profile means a new Google login,
  which is exactly where the Flow abuse-block lives).

Code: `studio.flowimagesgen_ready()`, `prepare_flowimagesgen_job()`,
`set_flowimagesgen_tier()`, `_adopt_flowimagesgen_outputs()`,
`run_imagegen_flowimagesgen()`; `autorun._run_images` dispatches on
`eff["engine"]`; the images panel has an Engine select.

DECISIONS worth keeping:
1. **The shotlist `style` is usually NOT sent.** Flow refuses prompts over
   ~2450 chars with the SAME message as rate limiting, and WhisperRadar's
   shotlist style alone measured 4000 chars - sending it would fail every
   item. The per-image prompts already carry the art direction. The style is
   sent only when `longest prompt + style <= 2420`.
2. **Refs are passed as NAMES**, with the shotlist `refs` registry plus every
   file in the production's `refs\` (keyed by stem) as the job's name -> path
   map, and `refMode: reuse` - so FlowImagesGen attaches existing Flow project
   assets by name instead of re-uploading (its README: uploads duplicate
   project assets).
3. **Outputs go to `flow_images\` and are adopted into `images\`**, preferring
   the upscaled `<stem>_<tier>.png` over the 720p master, so `images\` never
   holds two graded copies of the same shot.
4. **Upscale tier** is written to FlowImagesGen's own
   `config/upscale.local.json` via `upscale --set-tier` (what its UI does).
   Mapping: upscale 0-4 -> off/1k/2k/3k/4k.
5. **Job name is `wr-<pid>`**, so FlowImagesGen's `state\wr-<pid>.json` makes
   a re-run resume the items that are still missing.
6. Rate limiting is waited out by FlowImagesGen itself (cooldown 180s, up to
   10 waits); cancel kills the whole process tree with taskkill /T /F.

### Auto Run producer — DONE (2026-09-22)

`whisperradar/producer.py` + `python wr.py produce [--plan]` + the Studio
"Produce from channels" button + `GET /studio/produce/plan` (dry run, JSON,
free - no LLM calls) and `POST /studio/produce`.

AGREED DESIGN (do not change without the user):
- Monitored source channels map to an own channel by GENRE.
- Candidates: `videos.status='transcribed'`, monitored channel genre = the own
  channel's genre, `video_id NOT IN (SELECT source_video_id FROM productions)`,
  published within `candidate_window_days` (0 = no limit), newest first.
- `topic_pick`: 'newest' = most recent candidate; 'llm' = the producer LLM
  picks among candidates AND writes the production title. A returned id that
  is not in the candidate list falls back to newest (never trust the model).
- Caps: `per_day` (global + per channel) counts productions CREATED today
  (`date(created_at) = date('now')`). Run window is local time; start > end
  means an overnight window.
- After create + seed, each production runs `autorun.run_pipeline` and STOPS
  before review. The producer stops the whole run on the first pause/failure.
- Skips are logged per channel (no candidates / cap / auto-run off).

LLM PROVIDER SEAM: providers now carry `api` (default "openai"); only the
OpenAI-compatible `/chat/completions` adapter exists, in `CHAT_APIS`
(studio.py). `llm_generate` dispatches on it and raises a clear error for an
unimplemented api; `provider_ready` returns False for one, so the UI shows it
as not ready. Adding Claude/GPT: config-only through any OpenAI-compatible
gateway, or a new `*_chat` function + one CHAT_APIS entry for a native API.
New settings: `producer_llm_provider` (empty = studio.llm_default) and
`candidate_window_days` (default 90).

### Seeding + create-form channel picker — DONE (2026-09-22)

- `studio.seed_production(cfg, conn, prod)` copies `bible.md` and `refs\`
  into a production folder. Source order: the production's own channel
  (bible_dir / refs_dir), then the global per-genre `seed_dirs[genre]`
  (a single folder holding bible.md + a refs\ subfolder). Idempotent - it
  never overwrites existing files. Returns {bible, refs, source}; source ''
  means nothing was configured, so no writes happen.
- Called on `/studio/new` (when an own channel is picked) and at the start of
  the shots stage in `autorun._run_shots`, so an unattended run does not pause
  at the bible gate. Manual re-seed: `POST /studio/<pid>/seed` + the
  "Seed bible + refs from channel" button on the shots stage.
- Studio create form gained a "My channel" picker; the chosen channel also
  supplies the genre when the form's genre is blank. Production cards and the
  production header show the owning channel.

GOTCHA for tests: `studio.prod_dir()` reads `work_dir` from `cfg.db_path`, so
a test using a TEMP DB still resolves the REAL production's working folder by
id - it will write into the user's real folders. Patch `studio.prod_dir` or
pass an explicit `work_dir` when exercising anything that writes files.

TOOLS ARE CONSUMED IN PLACE - do not vendor them. Renderly (stateful service:
own DB, storage, venv, signed-in Chrome profile), ImgToVideo (.NET, invoked as
a subprocess) and FlowImagesGen (CLI, Google session in a gitignored profile)
are all used from their own checkouts via config paths. A submodule copy would
strand Renderly's database/storage/profile and add a second login for
FlowImagesGen. Paths live in config.yaml: `imgtovideo_repo`, `renderly_url`,
`flow_driver_dir`, `flowimagesgen_repo`.

### Per-channel defaults wired into the pipeline — DONE (2026-09-22)

Resolution order is **global setting <- own channel <- production**, in
`settings.for_production(conn, prod)` (returns voice, engine, render_mode,
upscale, per_day, topic_pick, autorun_enabled, bible_dir, refs_dir, the
own-channel row and its Renderly mirror name). A NULL/empty per-channel field
means "inherit the global".

- `own_channels` columns are now NULLABLE (the first shape had NOT NULL
  defaults, which made "inherit" impossible). `db._migrate_own_channels`
  rebuilds the old table in place, preserving rows.
- `autorun._effective(cfg, pid)` wraps it; `_default_render_mode` resolves to
  flow/api ('auto' = Flow when the driver is installed, else API).
- images stage: flow mode passes the own channel's mirror NAME, api mode
  resolves its Renderly channel ID (`studio.resolve_renderly_channel(...,
  create=True)`) and passes it + the effective upscale to
  `studio.run_imagegen(cfg, pdir, channel=, upscale=)`.
- audio stage: TTS voice = production.voice -> channel.default_voice ->
  global.default_voice.
- `/my-channels` edit row now exposes voice, engine, render mode, upscale,
  auto-run on/off, per-day cap, topic pick, bible folder, refs folder; empty =
  inherit. A compact summary line shows the resolved choices.
- Production page: "My Channel: X" / "no own channel" badge, a "change
  channel" control (`POST /studio/<pid>/own-channel`), and the images-stage
  Flow channel + upscale fields default from the channel.
- `default_render_mode` gained an "auto" choice (now the default) so a fresh
  install keeps the old "Flow when installed, else API" behaviour.

Unassigned productions keep the legacy behaviour (Renderly channel
"whisperradar"), so existing productions are unaffected.

### OpenSpeaker favorites for the voice picker — DONE (2026-09-22)

The audio-stage picker can now list the voices starred in OpenSpeaker
(ai33.pro). The endpoint was NOT in the docs and was verified live with a
read-only GET before wiring it:

- `GET /v3/favorites` -> `{success, favorites: [{created_at, provider,
  voice_id, voice_data}]}`. `provider` there is a generic "v3" — the real
  provider is the `voice_id` prefix. `voice_data` carries name/gender/
  language/accent/preview_url. 9 favorites on this account.
- `POST /v3/favorites` `{provider, voice_id, voice_data}`;
  `DELETE /v3/favorites/<id>`; `POST /v3/favorites/bulk` — NOT used yet.

Implementation:
- `ai33.favorites(cfg, refresh=False)` — fetch + `_normalize` + 10 min cache
  (`_favorites_cache`); dedupes by voice_id; provider from the prefix.
- `ai33.wants_favorites(cfg)` / `ai33.voices(cfg, source=...)` — favorites
  mode when `studio.ai33_voice_source: favorites` OR the sentinel
  `studio.ai33_voices: ["favorites"]`; an empty favorites list falls back to
  the shortlist/catalog. The sentinel is stripped before the curated branch.
- `GET /studio/voices.json?source=favorites` (+ `source` echoed in the JSON).
- Audio stage: a "☆/★ Favorites" toggle next to the voice search reloads the
  picker from favorites (`loadVoices(source)` in studio_detail.html).

DEFERRED: "Sync favorites into shortlist" (persisting the starred ids as a
shortlist) needs a settings store — it belongs with the Auto Run settings
page, not a config.yaml rewrite (WhisperRadar never writes config.yaml; a
dump would strip its comments). Channels/settings live in SQLite.


### NEXT SESSION HANDOFF (2026-09-21, ~21:40) — STEP 1 GREEN on the second account

flow-simple.js (extension-v2) is the minimal no-reference driver and it
PASSED step 1: S01_01 prompt (truncated master, no refs) rendered the
correct scene end-to-end on profile-b (second Google account, free tier,
Nano Banana 2 Lite) — 1/1, saved to Temp\kilo\noref-test. What made it work:
1. Prompt length: composed master+prompt of ~2500 chars keeps the submit
   arrow DISABLED. --maxchars (default 2400) trims the MASTER portion at a
   word boundary; card prompt always intact.
2. Submit arrow identity: aria-label "Start generation" in some views, a
   bare "arrow_forward" icon button in others — findArrow matches both.
3. View targeting: only the FEED ("What do you want to create?") hosts the
   create composer. Batch DETAIL views host "What do you want to change?"
   (edits an existing image!) — ensureComposeSurface navigates Back from
   detail views, never types into a change composer.
4. Harvest: FIRST grid tile (newest) + sha1 content-hash. Old tiles
   re-signing URLs / loading full-res variants keep their positions, so
   position 0 + new bytes = the result. Verify visually before trusting.

Run: node flow-simple.js --file <batch> --out <dir> --profile <dir>
     [--limit N] [--maxchars 2400] [--timeout ms]
batch.json: { style, images: [{file, prompt}] } — UTF-8 no BOM (PowerShell
5.1 Set-Content adds a BOM that breaks JSON.parse; node writes are clean).

The OLD flagged profile (extension-v2\profile, first Google account) is
still abuse-blocked ("unusual activity"). profile-b is clean. Do not
rotate profiles to dodge flags — the second account is the user's choice.

NEXT (agreed, one at a time):
1. STEP 2 — one reference: upload the image to Flow's gallery ONCE, then
   attach via the composer's "+" menu → gallery selection (NEVER disk
   drops into the composer). Assert: exactly ONE ingredient chip before
   triggering (chip count must equal the refs array length — user caught
   Flow stacking extra chips). Verify the render is on-model.
2. STEP 3 — two to three refs, same assertion, then re-add echo/label
   protections from flow.js only as needed.
3. Then point WhisperRadar's images stage at the proven script and restart
   the production-4 auto-run (85 cards, images dir empty).
4. Backlog (automated producer) stays parked until the Flow path is stable.
   (OpenSpeaker favorites shipped 2026-09-22 - see above.)

### HANDOFF (2026-09-21, ~13:00) — superseded, kept for the fix history

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

USER'S KEY EXPERIMENT (2026-09-21 ~11:00): the FULL master prompt (style.md
- esp. "Recurring character identity must be preserved exactly from the
supplied reference: mixed-race half-Asian woman..." + the long "no
photorealism/no Pixar..." list) trips the violation filter MANUALLY TOO.
The TRUNCATED master (cut at "Recurring character identity must be
preserved") passes manually - user rendered the correct S01_01 scene
(woman at grandma's cabinet, tea set, collector plates, bubble wrap) 4+
times. shotlist.json style field TRUNCATED accordingly
(shotlist.json.bak-full-master holds the original).

Automation profile status: flagged. The driver's composer refuses to ARM
(disabled=true) for ANY input method - trusted typing, execCommand
insertText, clipboard paste - even when the text verifiably lands in the
focused bottom-area editable (screenshots prove the visible pill empty /
arrow disabled). Manual use in the user's own browser passes normally.
Do NOT rotate profiles to dodge the flag - let it rest (24-48h) and retry,
or resolve via the Help Center link in the failure panel.

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
