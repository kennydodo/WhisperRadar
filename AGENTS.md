# WhisperRadar — Agent Notes

YouTube channel monitoring + Studio video production pipeline.
Run: `python wr.py serve` (dashboard at http://127.0.0.1:8540). Tests: `python -m unittest discover -s tests` (never call paid APIs — Gemini/Renderly/Flow — for testing).
Lint/typecheck: none. Backend: `whisperradar/` (stdlib Flask, SQLite at `data/whisperradar.db`).
Docs: `README.md`. Key surfaces: dashboard/channels/transcripts (`webapp.py` + `dashboard.html`), Studio pipeline (`webapp.py` studio routes + `studio.py` + `templates/studio_detail.html`).

## DONE (2026-10-06) - gallery recovery after the account-throttle pause

When Flow throttles the ACCOUNT ("We noticed some unusual activity") the images
stage waits `images_throttle_wait_minutes` (default 60). Flow keeps finishing
cards while the account is out, so `_run_images` now runs the gallery recovery
(`_recover_after_stop(..., when="after the pause")`) right AFTER that wait and
BEFORE the next round: finished cards are adopted instead of re-rendered and
paid for twice; if that brings in everything the round is skipped. The gallery
is deliberately NOT read before a throttle pause (poking a throttled account
lowers its standing). The short pauses (the ~10 min "still busy" / refusal /
"too many cards failed" waits) get no extra recovery: the "cards failed" stop
keeps its single recovery BEFORE the pause, "still busy" has none. Best effort
(a failing recovery is logged, the resume goes on); a Stop during the pause
cancels without recovering. Tests: tests/test_recover_after_stop.py.

## DONE (2026-10-06) - Render resolution and Upscale tier are one setting

1080p<->1, 2k<->2, 4k<->4, flow-native<->0 (`settings.RESOLUTION_UPSCALE`).
Tier 3 (a duplicate of 2) is gone from the dropdowns; stored 3s read as 2.
- UI: changing either dropdown moves the other (Settings page and channel form;
  "inherit" on one makes the other inherit).
- Save (`settings.save`, `/my-channels/edit`): `reconcile_resolution_upscale` -
  a set resolution decides the tier (warning in the banner when the posted tier
  disagreed), a tier posted alone decides the resolution.
- Runtime: `for_production` derives `upscale` from the effective resolution, so
  old mismatched data heals itself; `eff["upscale_warning"]` is logged by the
  images stage when someone explicitly set a disagreeing tier.
- ImgToVideo: `prepare_project_folder` (every render path) writes
  output.width/height from the resolution; `studio.image_size_warning` logs when
  the images' long side is smaller than the output width (they would be scaled
  up). ImgToVideo itself has no upscale option - the tier only sizes the images.
- "Apply to all channels" on one of the pair applies its partner too
  (`settings.with_partners`). Tests: `tests/test_resolution_upscale_pair.py`.

## DONE (2026-10-06) - "apply to all channels"

Two actions, limited to `db.APPLY_ALL_FIELDS` (the channel fields that are
production defaults and have a global setting of the same name; identity,
voice, bible/refs, style, planning brief are never eligible):
- **Settings page**: button under each eligible setting -> saves the global
  value, then sets that field NULL on every channel (`db.reset_channel_overrides`)
  so all channels, including future ones, inherit it. The confirm shows how
  many channels currently override it.
- **My Channels form**: button next to each eligible field (injected by script
  from the whitelist) -> saves this channel, then copies its saved value to
  every other channel (`db.copy_channel_value_to_all`; a NULL/inherit value
  copies as inherit).
- OPT-IN: the buttons are hidden (and the server refuses `apply_all`) until
  Settings > Service handling > "Show apply to all channels buttons"
  (`show_apply_all`, default off) is ticked.
- Both ride on the existing save routes via a repeated `apply_all` form value;
  the buttons are `type=button` + `wrApplyAll()` (never submit buttons, so Enter
  cannot trigger them). Productions are never touched. Tests:
  `tests/test_apply_to_all_channels.py`.

## DONE (2026-10-06) - KILL switch, ONE mechanism for every engine (no retry)

Branch `feat/kill-switch`. Agreed with Kehinde 2026-10-01. There are only two
things to kill (FlowBatch and the Renderly API ImageGen), and the same call
kills both.
- Registry in `studio.py`: everything WR spawns for an image batch goes through
  `_popen_tracked` / `_run_tracked` and is registered under the production's
  folder (`batch_scope(pdir)`; the stage runners `_run_images`, `_run_refs`,
  `recover_images`, `upscale_images` carry `@studio.batch_scoped`). FlowBatch
  (generate / recover / upscale / refs via `_flowbatch_stream`, prepare via
  `_run_tracked`) and the Renderly API ImageGen `dotnet run` (`run_imagegen`,
  formerly an uncancellable `subprocess.run(timeout=7200)`) are the registrants.
- `studio.kill_image_batch(pdir)` tree-kills what is registered
  (`taskkill /T /F` on Windows, SIGTERM to the process group on POSIX) and
  returns `{killed: [labels], nothing_running}`. Nothing engine-specific is in
  the kill path.
- A user kill surfaces as `studio.BatchCancelled` (never a plain failure):
  `_run_images` re-raises it before the pause-and-resume branch, the local
  upscale `except Exception` handlers re-raise it, `run_stage` reports it as
  `stopped`.
- `POST /studio/<pid>/images/stop` (same URL) now: sets the job's cancel flag
  FIRST, then calls `kill_image_batch`, and reports honestly what died or that
  nothing was running. The button reads "Kill image generation" and its confirm
  says it ends the process now and does not retry. The gentle "finish the
  current card" stop is gone on purpose (one mechanism).
- The manual "Render images" job now passes a cancel callable (it passed none).
- Tests: tests/test_kill_switch.py (real child processes incl. a whole-tree
  check, no paid/Flow/Renderly calls) + the updated tests/test_images_stop.py.
- Not covered by a test, needs a live look: a real FlowBatch/Chrome kill on
  Windows (`taskkill /T` is the same call `_flowbatch_stream` already used).

## DONE (2026-10-02) - writing style vs visual style, named apart in the UI

Two different things were both called "style": the WRITING style (how the
script sounds; `writing_style.md`, made at stage 1, used only by the script
prompts and judge) and the VISUAL style (how the images look; the channel's
"style" field seeding `style.md`, used by the shotlist stage). Now: stage 1
is labelled "writing style"; the style stage explains it is not the visual
style; the script stage shows "writing style applied" (shots/images no longer
claim a style guide is applied); the shots stage has a "Visual style (how the
images look)" block showing the text in force and where to edit it; the
channel form says "visual style - how the images look". No file or field
names changed.

## DONE (2026-10-02) - external-LLM prompts for the script and shotlist stages

Studio -> script stage and shots stage each have a collapsible **"Use an
external LLM (ChatGPT / Claude)"** block that builds copy-paste prompts
(`whisperradar/external_prompts.py`, `POST /studio/<pid>/external-prompt`,
JSON out; it never calls an LLM). Everything reuses the built-in stages'
prompt functions and the channel's settings:
- script: writer prompt (`studio.script_prompt` + title + WRITING style +
  research notes - never the raw reference script - + target words + the
  channel's min rating / max overlap) and judge prompt (`studio.rating_prompt`
  + the pasted script, the software-measured overlap and the channel's bar).
  Optional "Style prompt" / "Notes prompt" extract those from the reference.
- shotlist: planner prompt (`studio.shotlist_prompt`: brief with the channel's
  motion profile / hold range / presentation, narration, visual style, bible,
  refs policy, pacing note + output rules so the JSON stays readable and long
  plans continue cleanly) and judge prompt (the channel's hard rules in plain
  text + `studio.alignment_prompt`'s completeness audit + narration + the
  pasted shotlist). The app's own code checks run on the pasted shotlist and
  show next to the judge prompt.
- The judge uses what is in the script / shotlist box on the page. Results
  are pasted back through the normal Save script / Save shotlist boxes.
- Style and bible go only where needed: writing style -> script prompts;
  visual style + bible -> shotlist prompts.

## DONE (2026-10-06) — shotlist patch helpers for a web-chat writer; script local checks / revise prompt removed

Found while testing z.ai as the shotlist writer on production 8: a patch prompt
that shows only the failure reason + narration lets the writer swap the subject
(cheetah -> dog / birds), and a reply can key a patch to an asset that was not
asked for. Added, NOT wired into the built-in API loop (`autorun` is unchanged):
- `studio.with_current_prompts(weak, data)`; `shotlist_patch_prompt` shows a
  CURRENT PROMPT per asset and a keep-the-subject rule when one is supplied
  (identical output when none is).
- `studio.check_shotlist_patch(patches, asked)` -> (usable, unknown, missing).
Tests: tests/test_shotlist_patch_guard.py.

Removed from the external-LLM prompts (the external judge's own feedback does
the check/revise): `external_prompts.script_local_checks`,
`script_revise_prompt`, the `script_revise` route kind, the Revise button and
feedback box on the Studio page, and the local-checks note under the script
judge prompt. The shot judge still shows the app's structural checks.

## DONE (2026-10-06) — web-chat stages (z.ai writes, DeepSeek judges; no API)

Runs the script and shotlist stages by driving chat websites in a real browser,
using the EXTERNAL-LLM prompts (`external_prompts`), not the built-in API ones.
The API loop (`autorun`) is untouched.
- `whisperradar/webchat.py`: the browser driver (Playwright, headed, one
  persistent profile per site under `<db dir>/webchat/<site>`). `Site` entries
  for z.ai and DeepSeek (selectors from live inspection). Uses installed Chrome,
  then Edge, then Playwright's Chromium, with automation flags removed (Google
  refuses sign-in in a browser that announces itself as automated).
  z.ai's Deep Think level is set to Low on each new chat (Max can eat the turn).
  `ask(..., new_chat=False)` answers inside the open chat (feedback to the
  writer). DeepSeek "Continue" is clicked when present (never observed yet).
  CLI: `python -m whisperradar.webchat login <zai|deepseek>` (window stays open
  until you press Enter) and `ask <site> "prompt"`.
- `whisperradar/webstages.py`: the loops. Script: writer -> judge (new chat) ->
  `autorun._script_gate` -> feedback to the same writer chat, max 3 rounds.
  Shotlist: planner (+ "continue" while the JSON is cut off) -> judge reviews
  the whole plan -> weak prompts go back as `studio.shotlist_patch_prompt`
  (with current prompts, keys checked), hard faults as a full corrected plan.
  A prompt over `INLINE_MAX_CHARS` (60000) switches to attached files. A run
  that never passes saves its best draft but does NOT advance the production.
  The transport is swappable (tests use a fake).
- Studio: "Run script / shotlist in web chat" buttons (writer + judge pickers)
  -> `POST /studio/<pid>/webchat/<script|shots>` (a normal background job).
- Reply-completion fixes after the first real run: z.ai shows only "Thinking..."
  while it thinks (no "Stop" text; its round stop button `button > span.size-3`
  is the signal), and the reply element starts with "Thought Process" - both
  handled by `clean_reply`/`generating_js`; `ask(ready=...)` keeps waiting until
  the reply is complete (script long enough / judge JSON parses); a send that did
  not register is repeated, and a start timeout reports what the page shows.
- z.ai's page keeps only the last ~56 lines of a long code block in the DOM, so a
  140 KB shotlist could never be read from it (first real shotlist run read 19,605
  characters and then sent a pointless "continue"). `Site.stream` makes the driver
  hook `fetch`, capture z.ai's `/chat/completions` event stream and return the
  joined `delta_content` of the `phase: answer` events (thinking left out); the DOM
  text is only the fallback. DeepSeek still reads the DOM (its replies are short).
  Every raw reply is also kept in `<production folder>/webchat_debug/`.
- NO round limit for the web-chat loops (decided by the owner: the external
  writer + judge run until the judge passes the work). `rounds=None` is the
  default; stop it with the "■ Stop web-chat run" button
  (`POST /studio/<pid>/webchat-stop`, sets the job's cancel flag, honoured after
  the current round). A corrected shotlist can come back WORSE (12 faults/2 weak
  -> 20 faults/11 weak was seen), so the loop remembers the best plan seen
  (3 x faults + weak prompts) and a stop/failure saves THAT one, never advancing.
  Raw replies per round: webchat_debug/plan_round<N>_*.txt, judge_reply_round<N>.txt.
- DeepSeek send safety: a send is repeated only when there is no sign it went
  out (box still full, address unchanged for a new chat, nothing generating, no
  new message) after 30 s - DeepSeek's send button becomes a STOP button, so a
  second click on a slow-to-accept big prompt cancelled the answer (5 min hang).
- Tests: tests/test_webchat.py, tests/test_webstages.py.
- NOT yet verified on the real sites: long (12k+) prompts through the driver,
  file attachments, DeepSeek Continue, z.ai guest-mode limits, whether z.ai's
  reply element includes its "thinking" text, judge strictness (DeepSeek was
  lenient on invented facts in a manual test).
- Possible later: Google sign-in via attaching to a normally started Chrome
  (remote debugging) as a `webchat_mode: attach` option; Claude / ChatGPT
  `Site` entries (their consumer terms restrict scripted use - read first).

## DONE (2026-10-01) — local upscale AFTER download, for both engines

Requested by Kehinde: stop upscaling inside the download loop. Download the
stills at native size, then upscale them locally in ONE pass once the batch
has finished - the same pass whichever engine produced them.

- Setting: **Flow native upscale level** (`flow_native_upscale`: off / 1k /
  2k / 4k, default off). It only appears (Settings -> Video render, and the
  channel form) when Render resolution = "Flow native"; a channel value
  overrides the global one. The old global `upscale_after_download` switch was
  removed. Other render resolutions still upscale inline as before.
- When the level is not "off", `autorun._run_images` drives the engine with
  upscaling OFF (`set_flowbatch_tier(..., 0)`) so the native masters land, then
  calls `studio.upscale_images_locally(..., tier=<level>)` once.
- `studio.upscale_images_locally` uses FlowBatch's local upscaler for
  every engine - `node src/cli.js upscale <images dir> --tier <t> --in-place`
  (Real-ESRGAN ncnn-Vulkan, Lanczos CPU fallback, warned in the log). There
  is NO Renderly-backend upscaling; without `studio.flowbatch_repo` it
  raises a clear error. `--in-place` skips files already at the tier, so a
  re-run can never upscale an upscale.
- Manual: the IMAGES stage's **"Upscale images (local)"** button ->
  `POST /studio/<pid>/images/upscale` -> `autorun.upscale_images()` (records
  an images step that is `done` only when no shotlist image is missing, so a
  partial upscale cannot make the stage look complete). Safe to re-run at any
  time; ideal for re-tiering or retrying.
- Gallery **recovery** follows the same rule: recovered masters are adopted
  first and the single local pass runs afterwards.
- Side note: such a run leaves FlowBatch's own config tier at `off` (WR sets
  the tier on every run anyway), and existing files already at the tier are
  skipped, so the 189 stills on production 20 need no work.

## DONE (2026-10-01) — manual "Recover from Flow gallery" (images stage, both engines)

Requested by Kehinde 2026-10-01: when a batch stops on consecutive failures,
the images Flow DID generate are often still in the project's gallery - never
downloaded. Re-running the prompts regenerates them (burning Flow quota);
the manual button pulls the existing results first.

Implemented across the checkouts:
- Button under the IMAGES stage (`studio_detail.html`, next to "Render
  images") -> POST `/studio/<pid>/images/recover` (webapp.py, manual-only -
  it never enters the pipeline plan) -> `autorun.recover_images(cfg, pid)` ->
  dispatch by engine:
  - `flowbatch`       -> `studio.run_flowbatch_recover` ->
    `node src/cli.js recover --job <flowbatch.json> --output <flow_images>
    --report <flowbatch_recover.json> --project-url <url>`; adoption still
    goes through `_adopt_flowbatch_outputs` (which also sweeps flow_images
    files a crashed run had downloaded but never adopted).
  - `renderly + api`  -> refused with "no gallery to recover from" (the API
    stores its results itself).
- The runner adopts ONLY files missing locally, match gallery tiles to
  shotlist items by prompt, report {recovered, still_missing}, record one
  "images" step (method "manual"; status done ONLY when nothing is still
  missing, so a partial recovery never flips the stage complete), and NEVER
  generate. A stored Flow project URL is required: reading Flow's MOST
  RECENT gallery could adopt another production's images under these names.
- FlowBatch (commands/recover.js): enumerates finished result tiles via the
  existing snapshotAssets facts (canRedo + finished host + not uploaded +
  not failed) across a new `driver.scanAssets()` virtual-scroller sweep;
  matches tile->item by label prefix (>=20 chars, never when ambiguous),
  then by reading each tile's own "Reuse prompt" redo control
  (`driver.readTilePrompt` - composer read + cleared again after EVERY tile,
  generateButton never touched), then submission order ONLY when the counts
  agree and all prompts are distinct; downloads via the same CDN-first
  3-attempt path, upscales per config, writes the item under its exact job
  `file` name, and marks its RunState item done. `recover --dry-run` plans
  without a browser.

Tests: WR tests/test_recover.py; FlowBatch test/commands/recover.test.js +
driver tests.

## DONE (2026-10-01) — per-channel planning brief: motion profile + presentation

Branch `feat/per-channel-brief-profile`. The manifest-authoring brief is no
longer read from ImgToVideo: it is a TEMPLATE, `whisperradar/brief_template.md`,
rendered per channel by `whisperradar/briefs.py` (ImgToVideo never reads the
brief - it only consumes shotlist.json + SRT + audio - so its own copy is just
a standalone paste-by-hand version and is left alone).
- Slots in the template: `HOLD_RULE`, `MOTION_SECTION`, `MOTION_FIELD_RULE`,
  `CANVAS_SPEC`, `CHECK_HOLD`, `CHECK_MOTION`, example codes `EX_A/B/C`, and
  `PRESENTATION` (renders to nothing when empty). The STANDARD preset's text is
  the original wording verbatim - `tests/test_brief_profiles.py` proves the
  default render equals the original brief (needs the sibling ImgToVideo repo).
- Per channel (`own_channels.brief_motion`, `brief_presentation`; NULL =
  standard / empty): My Channels > Planning brief. No global Settings entry on
  purpose - nothing set = today's behaviour.
- Presets (`briefs.MOTION_PRESETS`): `standard`, `static` (ST only, no ST/share
  caps), `long_holds` (10-30s, hold range replaces the global 12s ceiling).
  A profile carries BOTH the prompt wording and the gate limits, so
  `studio.shotlist_pacing(..., profile)` / `review_shotlist(..., profile=)`
  enforce what the brief says. The planner's pacing note is
  `briefs.pacing_note()` (profile-aware; standard text unchanged).
- Hold range: `own_channels.brief_min_hold` / `brief_max_hold` (seconds, NULL =
  the preset's / the global max) lie over the preset via
  `briefs.resolve_profile()`. They feed the prompt text, the pacing note AND the
  gates - editing the template alone would not change what the review enforces.
- `autorun._run_shots` resolves the profile, renders the brief, logs it and
  saves it as `versions/shotlist/brief_used.md`.
- To add a preset: a `MotionProfile` in briefs.py + `MOTION_PRESETS`. To
  change what the planner is told for all channels: edit brief_template.md.
- Tunable: `briefs.MIN_HOLD_MAX_SHORT_SHARE` (0.35) - a min-hold channel faults
  when more than this share of shots (last one exempt) hold under the minimum.

## DONE — refs now honor the image engine (fixed 2026-09-29)

Was: the refs stage always rendered reference images through FlowBatch,
even for a Renderly channel. Fixed: `_run_refs` (autorun.py) now branches on
`eff["engine"]` exactly like `_run_images` does - `"flowbatch"` still calls
`studio.run_flowbatch_refs`, everything else calls the new
`studio.run_renderly_refs` (studio.py), which drives the SAME Renderly
path `run_imagegen` uses for real shots (a throwaway shotlist-shaped temp
project folder, one image per ref, discarded after the results are copied
into refs\), resolving the channel and upscale the same way the images
stage's `_stage_params` does. Tests: `tests/test_refs_engine.py`.
A Renderly channel no longer needs FlowBatch or a Flow login for refs.

## NEXT SESSION — improvement backlog (found 2026-09-27, ranked)

Pick from the top; #1-#4 are the ones that cause user-visible errors.

1. **DONE (2026-09-27).** Shotlist truncation on long narrations - a plan
   that closes its JSON cleanly (not the mid-JSON "incomplete" cutoff
   `continuation_prompt` already handled) but simply stops before the final
   cue used to burn a whole extra attempt on a full re-plan, truncating at
   roughly the same point every time. Added `shotlist_tail_gap` (finds the
   highest covered cue vs the final one, only when the rest of the plan is
   otherwise clean - a real internal gap/overlap/out-of-order run still
   needs the full re-plan), `shotlist_tail_continuation_prompt` (asks for
   just the missing tail as a {shots, images} fragment, reusing the
   continuation path rather than a full re-plan) and
   `merge_shotlist_continuation` (appends it) - wired into `_run_shots`'s
   loop right after a successful parse and before review, budgeted by the
   existing `SHOTLIST_CONTINUE_ROUNDS`. The structural gate
   (`shotlist_structural_faults`) already verifies coverage, so it confirms
   the merge closed the gap. Unit tests in tests/test_shotlist_tail_gap.py,
   integration test (planner stub returning only the first half of the
   cues, asserting the tail continuation fires and the merged plan covers
   every cue) in tests/test_shotlist_tail_continuation_integration.py.

2. **CHECKED (2026-09-27), part (b) already correct; part (a) declined.**
   `provider_ready` (studio.py:2210) still checks a key's PRESENCE, not its
   VALIDITY, so a present-but-revoked key still reads as "ready" and can
   still be picked as the planner or the stall-fallback. But traced the
   actual request path end to end: a 401/auth error from either transport
   (`_openai_chat_curl`'s `fail-with-body`, `_openai_chat_urllib`'s
   `HTTPError` catch) already surfaces as a plain `RuntimeError`, never
   `LLMStalled`/`LLMEmpty` - so `llm_generate` already re-raises it directly
   (temperature==1.0 skips the fallback branch entirely) instead of masking
   it behind a fallback attempt. Added
   `test_an_auth_error_from_the_primary_provider_is_not_retried` (asserts
   `_fallback_provider` is never even called) to lock this in - part (b)
   needed no code change, just a test making it explicit. Asked the user
   about part (a) (a live `GET /models` check, cached, surfaced in the
   providers UI); user chose reactive-only for now, so (a) is deliberately
   not done - a bad-but-present key is still only caught when it's actually
   used (as a fallback, that means one wasted attempt, but both errors are
   reported together per item #3's fix, not masked).

3. **DONE (2026-09-27).** Fallback error masking in `_retry_on_different_provider`
   fixed - when the fallback ALSO fails, both errors are now reported
   together (chained via `from`) instead of only the fallback's, with a
   regression test (7ea4e73).

4. **DONE (2026-09-27).** `.gitattributes` added (LF everywhere except native
   `.cmd`/`.bat`/`.ps1`), plus a one-time normalize of the three files still
   CRLF (test_llm_limits.py, test_shotlist_faults.py, studio_detail.html) -
   verified with `--ignore-all-space` that only real content changes
   remained (7ea4e73).

5. **Audit the ~60 broad `except Exception` handlers.** Most are deliberate
   (`# noqa: BLE001` + comment), but the judge-outage bug just fixed
   (a7b7c37) was exactly a silent `except` swallowing a failure as "0 faults".
   Confirm none of the others hide a real error behind a silent default.

6. **Module size.** studio.py ~3693 lines, webapp.py ~2476. The LLM transport
   (openai_chat/curl/stall), shotlist planning+review, refs and image-gen are
   now fairly separable - splitting them reduces the parallel-edit collisions
   between agents (see #8).

7. **CHECKED (2026-09-27), no fix needed.** Swept every test file for a call
   that touches `cfg.studio_dir` (prepare_project_folder, seed_production,
   run_stage, run_merge_render, ...) without redirecting it. Two more hits
   besides test_bible_seeding (test_batch_queue.py, test_stage_provider.py)
   both fully mock `run_pipeline`/`run_stage`/`Popen`, so nothing in them
   ever reaches real disk. Nothing left to fix.

8. **Coordination (process).** Today the `productions.llm_provider` pin was
   added, removed, and re-added differently across two agents, and both
   edited studio.py/autorun.py in parallel. Smaller commits + a shared
   "in progress" line here would cut merge conflicts.

Still-open known bugs (see "WhisperRadar bugs to fix (found 2026-09-24)"
below): #2 flow_project_url precedence (verify - may now be fixed by the
flowbatch project_url path), #3 renderly_upscale default 4 vs 2K, #4
FlowBatch ref-resume re-generating a `done` item whose files were deleted.

## NEXT SESSION — shotlist speed: two-pass planning design (queued 2026-09-26, needs discussion before building)

A 484-cue narration forces one giant LLM call: 19-minute generations,
malformed-JSON attempts (2 of 3 failed on p16), output-limit continuation
rounds. Sketch agreed so far — **inputs stay whole**: the SRT, bible and
style go in complete and stay in the production folder untouched; what is
SPLIT is the RESULT generation:
- pass 1 (one small call over the FULL narration): a scene outline only -
  main beats S01..SNN with cue ranges + one line each. Tiny output, fast,
  globally coherent.
- pass 2 (one small call per beat): brief + that beat's cues + previous-beat
  context + the running refs registry so later beats REUSE refs instead of
  inventing duplicates. 5-15 shots per call - no truncation, fast, parseable.
- the pipeline (not the model) stitches: coverage validation, sequential
  scene/sub-beat renumbering, refs registry merge. Gates and judge unchanged.
- optional brief edit in ImgToVideo: Document 1 (batch sheet) becomes
  optional - it is a deterministic rendering of the JSON the pipeline can
  produce itself; dropping it cuts model output ~30%.
OPEN QUESTIONS for the discussion: beat boundaries from pass 1 vs meaning,
parallel pass-2 calls (fast, duplicate-ref risk) vs sequential (consistent),
how the stitcher reports/repairs cross-beat faults, wall-time target.

RE-MEASURE (2026-09-27): the JSON-parsing bug in the script/shotlist judges
and the missing stall/fallback handling on `llm_generate` - both fixed this
session - were a real part of what made the single giant call slow and
fragile (malformed-JSON retries, 19-min stalls with no fallback). Run one
large-narration production against the current code first and see whether
the pain that motivated this design is still there before building it.

## DONE (2026-09-27) — code review of the 2026-09-25/26 session

Reviewed commit range `4855642..bb47ffd`. Findings and outcomes:
- reference-cleanup SQL (`settings_providers_save` / `/settings/providers/reset`):
  correct against renamed providers. Gap found: the new `llm_fallback_provider`
  setting (added 2026-09-27) was missing from both - FIXED, plus tests.
- `migration_llm_provider_cleared`: the one-time migration is fine, but
  `producer.run()` (the auto-run producer) still pinned `llm_provider` on every
  production it created - the same bug 2615632 fixed for the manual stage
  runners, reintroduced on a path that commit didn't touch. FIXED, plus a
  regression test (`test_producer_no_pin.py`). The column/precedence itself
  (production -> channel -> global) is fine now that nothing pins it silently.
- `shotlist_prompt` size growth is real (bible + supplied-refs + pacing text
  stack up) but no longer dangerous: a stall on the big prompt now retries on
  a fallback provider instead of hanging the run (fixed the same day). No
  further action.
- template/JS: confirmed the last-card-delete bug exactly as suspected -
  `delProvider()` only mutated the in-memory array with no Save button left to
  submit it. FIXED (`delProvider`/`saveProvider` now read back every card and
  submit immediately via a shared `readBackAll()`/`submitProviders()`), which
  also fixed a related bug: editing two cards and saving/deleting only one
  used to silently discard the other's edits.
- test isolation: `test_bible_seeding.py` correctly redirects both `db_path`
  and `studio_dir`; no other test touched in that session writes into
  `cfg.studio_dir`, so no other fix was needed.

## NEXT SESSION — RETEST the refs productions (written 2026-09-24, end of day)

BLOCKED until: (a) FlowBatch fixes the refs-stage issues in
`D:\Repos\FlowBatch\NEXT_SESSION.md` (assetTile detection with refs attached;
prepare/generate ref-presence agreement; promptReferenceChip confirmation), and (b) Renderly
implements the prepare/project-URL handover in `D:\Repos\Renderly\NEXT_SESSION.md`
(`prepare` + `FLOW_PROJECT_URL` + atomic report, per-ref status, and a signed-in driver profile).

Then: **retest the two failed productions and add two new ones.**

### State of the three test productions (`data\studio\7|8|9`)
- **pid 7 "ZZTEST refs To Live and More"** - channel 1, renderly/flow, upscale 2.
  shots DONE (4 images; ON-THE-FLY refs `CH_HOST, BG_KITCHEN, BG_OFFICE, BG_TATAMI`);
  refs DONE (all 4 generated via FlowBatch into project `7585c0da`);
  images NOT done - forced through FlowBatch, renders happened but `assetTile` detection timed
  out, **0/4 local**. RETEST images on the renderly engine once Renderly's handover lands.
- **pid 8 "ZZTEST refs The Nature Made Us"** - channel 2, flowbatch, upscale 0.
  DONE end to end: shots (6 images, NO refs) + refs skipped + images **6/6 rendered**. Keep as the
  no-refs reference.
- **pid 9 "ZZTEST refs Kenny Invest"** - channel 3, flowbatch, upscale 2.
  shots DONE (6 images; SUPPLIED refs `MAYA`, `BG_LIVING_ROOM_01`, `BG_KITCHEN_01` with paths into
  `refs\`); refs skipped ("all 3 supplied"); images FAILED (prepare/generate ref disagreement +
  promptReferenceChip). RETEST images after the FlowBatch fixes.

### Retest plan — see `RETEST_RUNBOOK.md`
1. **DONE (2026-09-24):** pid 7 images rendered 4/4 on the renderly engine.
2. **DONE (2026-09-24):** pid 9 images rendered 6/6 on flowbatch.
3. **READY:** two new full-pipeline productions were created for the retest —
   **pid 10** (Kenny Invest, flowbatch, SUPPLIED refs, glm-flash -> exercises the stall
   fallback) and **pid 11** (To Live and More, renderly, ON-THE-FLY refs, deepseek). Run them
   stage by stage with `python scripts/run_stage.py <pid> <stage>`; details in `RETEST_RUNBOOK.md`.
4. Re-confirm the three ref modes end to end: ON-THE-FLY (pid 7 / 11), SUPPLIED (pid 9 / 10),
   none (pid 8).

### How the stages were driven (reuse this)
- Shots: `autorun.run_stage(cfg, pid, "shots", params={"provider": "deepseek"})`.
- Refs: `autorun.run_stage(cfg, pid, "refs", params={"log": print, "cancel": None})`.
- Images: `params = autorun._stage_params(cfg, pid, "images", log)` then
  `autorun.run_stage(cfg, pid, "images", params=params)`. Force an engine with
  `params["engine"] = "flowbatch"` and pin a project with `params["flow_project_url"] = <url>`.
- Python: `D:\Repos\WhisperRadar\.venv\Scripts\python.exe`.

### WhisperRadar bugs to fix (found 2026-09-24)
1. **DONE (2026-09-27).** `glm-flash` hangs on large shotlist prompts were the same stalled-stream
   problem fixed generally this session: `_curl_read_stream`'s first-token/idle/hard-deadline guards
   raise `LLMStalled`, and `llm_generate` now retries on a different (fallback) provider. No
   separate fix needed here.
2. **CHECKED (2026-09-27), not reproducible in the real auto-run path.** Traced `_stage_params`
   -> `_run_images` -> `flow_project_url_for`: the images stage's `_stage_params` never sets
   `flow_project_url`, so it reaches `flow_project_url_for` as `None` and resolves production
   -> channel -> global correctly. This only happened when the RETEST runbook's manual driving
   passed `params["flow_project_url"]` explicitly at the call site (see "How the stages were
   driven" above) - a test-harness quirk, not a pipeline bug. Leave as-is unless it recurs in a
   real auto-run.
3. **`renderly_upscale: 4`** in `config.yaml` made the refs job upscale to 4K (`_4k` variants), not
   the 2K the tier doc states. Confirm the intended default.
4. Ref-generation state resume (FlowBatch side): a `done` item whose files were deleted is not
   re-generated (`state/wr-7-refs.json` skipped `CH_HOST`). Also noted in FlowBatch's file.

### Notes
- No channel settings or repo source were modified. Productions 7-9 and their folders are left in
  place; do not touch productions 1-6.
- The Renderly backend (:8022) was started as a managed service during testing;
  FlowBatch uses its local `profile`, **agent mode OFF** (never turn it on).
- Channel engines: 1 = renderly/flow, 2 = flowbatch upscale 0, 3 = flowbatch upscale 2.

## NEXT SESSION — handoff (written 2026-09-23, end of day)

### 1. Video render: preview + NLE export — DONE (2026-09-23)

The merge stage no longer full-renders `final.mp4` by default. It now builds a
fast **preview draft** and then exports an **NLE project**, matching
ImgToVideo's app flow (Build Preview -> Export to NLE):

```
render-final <folder> --preview    -> out\preview.mp4                        [fast draft, always]
export-premiere <folder>           -> out\premiere.xml   (FCP7 XML, File > Import)
export-capcut <folder>             -> out\capcut\<name>\ (copy into CapCut's draft root)
render-final <folder>              -> out\final\final.mp4 (+ captions.srt)   [legacy/hook/upload only]
```

- Setting `render_target` in its OWN Settings category **"Video render"**
  (`whisperradar/settings.py` SPEC + GROUPS), choices `premiere` (DEFAULT) |
  `capcut`, friendly labels via the new generic `choice_labels` extra rendered
  by `templates/settings.html`. Global, overridable per channel
  (`own_channels.render_target`, editable on My Channels with an "inherit"
  option) and resolved in `settings.for_production` as global <- channel
  (`c248e88`); the manual merge route uses the production's EFFECTIVE target.
- `studio.run_merge_render(cfg, pid_dir, target)` returns
  `{preview, target, project}` and runs BOTH commands. Helpers:
  `find_preview`, `nle_project`, `find_nle_projects`, `find_review_video`
  (final -> preview), `merge_done`. `RENDER_TARGETS`/`RENDER_TARGET_LABELS`.
- `autorun._run_merge` resolves the target from `_effective(...)["render_target"]`
  on the `cli` path; `stage_action("merge")` uses `studio.merge_done` and names
  the target. The hook path and the manual `final.mp4` upload are unchanged.
- Dashboard: merge stage plays the preview and links the export (Premiere XML
  file; CapCut draft `.zip` via `GET /studio/<pid>/capcut.zip`), review stage
  lists "Preview / final video" + "NLE export". Button/confirm modal text now
  names the target.
- NOT auto-copied: the CapCut draft stays in `out\capcut\<name>\` (download or
  copy it into `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`
  with CapCut closed) - matching the CLI's own behaviour.
- STILL TO MEASURE: what each target leaves in `out\` (the "fewer files /
  smaller" claim). Tuning knobs live in ImgToVideo's own `imgtovideo.json`:
  `PreviewWidth/Height`, `PreviewPreset`, `PreviewCrf`, `FinalPreset`,
  `FinalCrf`, `Output.Width/Height`, `FfmpegPath`.
- PREVIEW SHIMMER (2026-09-24): the first preview builds looked "shaky". Root
  cause was NOT the motion filter (the crop geometry is identical to the final
  render) but the **encoder's B-frames**: at the preview's 960x540 + CRF28 the
  B-frame quality pattern pumps the sharpness every (bframes+1) frames — a
  period-4 alternation measured on `out\preview.mp4` (laplacian ~13.4 vs ~12.7)
  that reads as shimmer. The 2560 final hid it (downscaling averages it out).
  FIXED in ImgToVideo: `RenderOptions.PreviewBframes` (default 0) /
  `FinalBframes` (default 3), emitted as `-bf` in
  `PreviewRenderPlanFactory.VideoEncoderArgs`; the final-render clone copies
  `FinalBframes` -> `PreviewBframes`. Verify with
  `ffprobe -show_entries stream=has_b_frames` (preview now 0, final still 2-3).
  Re-render old previews to clear it. ImgToVideo side committed `8317522`; the
  preview stays 960x540 (the user's call - they normally deliver 2K/4K, so a
  sub-2K preview was the first place this showed).
- MOTION JITTER (2026-09-24, second cause): after the B-frame fix a slow
  pan/zoom still stair-stepped. `zoompan` rounds the crop to whole INPUT pixels,
  so the pan moves in (output / grid) pixel steps. The source is supersampled 2x
  only under 3840, so a 720p source (prod5/6 images are 1376x768) sat on a 2752
  grid: ~0.5 px/frame rounded into a hard period-2 stair-step (pan trajectory
  std 0.310, jerk 0.399). FIXED in ImgToVideo: `RenderOptions.
  SupersampleTargetWidth` (default 4608) drives `PreviewRenderPlanFactory.
  SupersampleFor` (cap 4x; >=3840 never supersampled), so 720p -> 5504 grid,
  1080p -> 5760, 2304/2880 unchanged. Measured on prod5's preview: trajectory
  std 0.310 -> 0.083, jerk 0.399 -> 0.142, motion otherwise identical; render
  time 2.5 -> 6.8 min (cost is ~quadratic in the factor). The residual keeps
  falling as 1/grid with no floor, so more is possible at more cost - the knob
  is exposed for that.
  KEY POINT for the "just use higher-res images" plan: a 4K source gets grid
  5504 too, so higher-res images give the SAME smoothness as this fix, not
  more - and prod1 (the "smooth" reference) was all-STATIC clips, so it never
  exercised motion. The real remaining lever is a sub-pixel motion filter, not
  resolution.
- RESOLUTION ALIGNMENT (2026-09-24): the generators now deliver
  **2560x1440 16:9**, which meets ImgToVideo's 2304x1296 canvas spec ("larger
  same-aspect is fine") and matches its default output. Renderly `4e9f02c`:
  generation stays native Gemini 1K (cost unchanged), the local Real-ESRGAN
  step targets the 2K preset, `DEFAULT_RESOLUTION` 2K, legacy scale 0 rejected;
  FlowBatch: tiers were already 1k/2k/4k (`d1c1922`); the local override
  flipped off -> 2k (gitignored). WhisperRadar: tier 0 omits `--upscale`
  (ImageGen rejects 0 and the whole run failed), tier 3 -> 2k ("3k" was dropped
  upstream and normalizeTier throws), config `renderly_upscale` default 4 -> 2
  and a configured 0 no longer collapses to 4. TIER GUIDE: 0 = native 1K below
  spec, 1 = HD 1920x1080 below spec, 2 = 2K 2560x1440 RECOMMENDED, 3 = 2K,
  4 = 4K over-spec. REMAINING: PL/PR/PU/PD/PV overscan needs non-16:9 canvases
  no generator can produce (Gemini 1:1/16:9/9:16/4:3/3:4, Flow 16:9) - those
  shots keep the planner's fallback framing; PU/PD could get real vertical
  overscan later via 1:1 generation on Renderly.

### 2. FlowBatch `prepare --report` — RECEIVING END DONE (2026-09-23)

WhisperRadar side is implemented (`c130b5e`), so FlowBatch only has to
WRITE the report:

- `run_flowbatch_prepare()` runs `prepare --job <job> --report <prod
  dir>\flow_prepare.json` before generating, echoes the `FLOW_PROJECT_URL=`
  marker if printed, and reads the report back defensively (atomic write, so a
  malformed file is retried; absent = "no project yet").
- `_apply_prepare_report()` persists `projectUrl`/`projectId` onto the
  production row, writes `projectUrl` into the job, and switches the job to
  `refMode: "assets"` when every ref reports uploaded/reused/generated (a
  `missing` ref warns and falls back to `reuse`). A report with
  `created: true` while the production already had a different URL warns loudly
  - that is the duplicate-project signal.
- Optional throughout: a missing `prepare` command (detected from its usage
  output), a failure, or an absent report all return `{}` and generation
  proceeds on the stored URL.

STILL TO DO on the FlowBatch side: the `prepare` command itself
(create-or-open the project, get the refs into the gallery), the
`FLOW_PROJECT_URL=` marker, and the atomic report write. Also: the refs
GENERATION (item 3) is what makes `refMode: assets` useful - until refs exist in
the gallery the report will keep saying `missing`.

### 3. Reference generation stage (optional per channel) + naming

Agreed design: an optional stage BETWEEN shots and images that gets every
reference into the Flow project gallery once, so the image batch never uploads
(FlowBatch `refMode: assets`) and never duplicates project assets.

- Shotlist refs registry must widen from `name -> path` to carry
  `{kind, prompt, provided}` so the stage can: `provided` -> ensure the file,
  otherwise GENERATE it from its prompt and save as `<name>.png`.
- Naming convention (user-specified, now enforced by the shots gate):
  `CH_*` characters, `BG_*` backgrounds, `OBJ_*` objects, e.g. `CH_MAYA`,
  `BG_BATHROOM_01`. Rule: `^(CH|BG|OBJ)_[A-Z0-9]+(_[0-9]{2})?$`.
- Per-channel opt-in flag (e.g. `generate_references`).
- CONSEQUENCE: existing shotlists use names like `hero_kimono_woman` /
  `traditional_japanese_home`, which the new gate REJECTS. They must be renamed
  before a re-run.

UPDATE 2026-09-24 — the ON-THE-FLY mode is implemented end to end. The brief
(manifest-authoring-brief.md) now teaches three refs modes: SUPPLIED (used
verbatim, name kept even when it breaks the convention), NOT NEEDED (omit the
registry), ON THE FLY (invent: `refs` path `null` + a generation prompt in the
new top-level `refPrompts` map, names per CH_/BG_/OBJ_, HARD CAP 20 per
shotlist). The gate follows the same split: provided refs keep their names,
only generated ones must match REF_NAME_RE, and >20 planned generations is a
fault - the refs stage also hard-caps at 20. `parse_shotlist_output` needed no
change (the extra keys pass through) and `shotlist_refs`/`refs_to_generate`
already consumed this shape. Verified with synthetic shotlists: mixed
provided+invented -> no faults; invented off-convention -> naming fault;
25 invented -> cap fault; no prompt -> stranded; supplied file on disk ->
provided, only the invented one lands in refs_to_generate.

### 4. `flow_batch.json` — REMOVED (2026-09-23)

`prepare_flow_batch()` wrote `data\studio\<n>\flow_batch.json` with ref names
resolved through the registry, but nothing consumed it: `run_imagegen_flow`
never used the returned path (the engine reads `shotlist.json` and resolves
refs itself). Its only other reference was
`RESET_FILES` in webapp.py. The function is now `missing_flow_images()`, which
returns just the count of images still missing, and the stray file in
`data\studio\6\` was deleted.

### 5. Production #6 — finish by hand, then Resume

To Live and More, Renderly flow engine. Script gate failed (rating 5.5 vs the
channel's 9.4; overlap 6.2% passed), shots gate PASSED (113/119 prompts detailed
enough, 0 structural faults). Images stopped at **98 of 118** — Flow began
reporting "still busy" and every card after ~98 timed out. The batch and the
poller are stopped; the 98 images are kept. Upload the remaining 20 into
`data\studio\6\images\` (the upload preserves filenames), then Resume: the
images stage now treats "everything already present" as success and advances to
merge.

### 6. Script rating gap — diagnose on the next run

The judge (glm-flash, deliberately a different provider from the deepseek
writer) scored the best of 3 drafts 5.5 against a 9.4 bar. `versions\script\review.json`
now persists per-attempt score, criteria, feedback and weak spans (added this
session), so the next run tells us whether the judge is harsh or the drafts are
genuinely weak — which decides whether to adjust the rubric or the bar.

### 7. Flow reliability after ~98 images (both engines)

FlowBatch hit a refusal loop at item ~82 ("might violate our policies" /
"you have not been charged"), and the retired Renderly driver hit "still busy"
timeouts from ~98. In both cases the first ~80-100 items rendered fine, so this looks
session-level rather than per-prompt. Worth investigating before
trusting unattended batches of 100+ images.

### 9. Settings page needs tabs (user request 2026-09-24)

`/settings` now renders 28+ fields in seven sections on one long page (Auto Run,
Script quality gate, Shotlist gate, Production defaults, Scheduler,
Notifications, Service handling, plus the Tools status panel). The user wants it
as TABS - one tab per group, with the Tools panel as its own tab. `settings.py`
already exposes `grouped_spec()` returning `[(group, [entries])]`, so the
template only needs to render tab headers + one panel at a time (no server-side
change needed beyond passing the active tab, or a small JS switcher).

### 8. Loose ends from this session

- Auto Run's master switch is still ON and the global `per_day` is 2 (both set
  for the #2 validation run).
- `D:\Repos\FlowBatch\NEXT_SESSION.md` is modified but uncommitted (the
  frozen contract + the download/policy-refusal write-up).
- `CombineAll` is ahead of `origin/CombineAll`; merge to `master` when ready.



### BACKLOG — fully automated producer (user approved design 2026-09-21; BUILD ONLY WHEN the Google Flow abuse-block is resolved or with renderly default)

One button/schedule: WhisperRadar looks at followed channels, picks a topic
from each, and creates + runs a production by itself. All pieces exist except
orchestration:

1. PICK: per active channel, newest transcribed video not already a
   production source (source_video_id NOT IN productions).
2. CREATE: LLM-crafted title (fallback video title), genre = channel's genre
   (reuse POST /studio/new logic + db.create_production).
3. SEED: the channel's bible/refs (text or bible_dir/refs_dir) copied into the production
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
  mode; bible/refs folders are per channel).
- "We still need to iron out so many things later" - treat details as open;
  confirm with the user before building (schema, UI layout, topic-pick
  logic: newest vs LLM-chosen best topic).

Image-stage reality check when building: Flow once abuse-blocked an automated
profile; unattended runs should default to Renderly API
(headless, credit cost) until Flow is stable, or make render_mode a
per-channel setting on the settings page.

### Settings page: own channels + global Auto Run criteria — DONE (2026-09-22)

Two pages, both on the normal dashboard port (no extra service):
- `/settings` - global Auto Run criteria only.
- `/my-channels` - the channels the USER publishes on, with add/edit/remove
- Channel settings save/load: `channel_io.py` + `/my-channels/export[?id=..&id=..]` (JSON download of the ticked channels, or all) and `/my-channels/import` (upload). Matched by NAME; existing channels are kept unless 'replace' is ticked, and a replace from a real export puts unlisted settings back to inherit. `id` and the Renderly link (`renderly_channel_*`) are never exported or imported; unknown watched channels, providers and invalid values are dropped and reported. New channel field => add it to `db._OWN_CHANNEL_FIELDS` and it travels automatically (add a type rule in `channel_io` if it is not text).
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
  values, save() coerces/validates (int clamps, HH:MM check). Absent keys are left untouched.
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
1. Notifications when an unattended run pauses/fails (a 3am pause goes
   unnoticed otherwise).

### FlowBatch pacing and the assetTile trap (2026-09-23)

Measured on a real 156-image batch (production #5, The Nature Made Us):

- `config/settings.json` `generation.delayBetweenItemsMs: 20000` - a deliberate
  20s pause between items, plus ~40-60s of actual generation. So ~70s/item is
  the floor and 156 images is ~3h minimum.
- **The real cost is asset-tile detection, not generation.** FlowBatch
  waits for a NEW asset tile to appear; when it misses one it times out after
  `timeouts.generationMs` (300s), then the retry succeeds in under a minute.
  Observed: item 2 wasted 300s for a 38s generation. The error says "calibrate
  assetTile" and dumps debug\error-<item>-<stamp>.png - that screenshot plus
  `npm run discover` is the fix.
- **`generation.maxCooldowns: 0` disables waiting on a rate-limit refusal**
  (`src/runner/run.js:345` requires maxCooldowns > 0). The batch then STOPS,
  deliberately: the block is a reCAPTCHA score on the profile and grinding
  lowers it further. Resuming later continues from `state\wr-<pid>.json`.
- **`retries: 1` is a hard-stop risk** - a second consecutive detection failure
  is non-retryable and stops the batch. Raise it to 3 for long batches.
- WhisperRadar does NOT manage the `generation` block; it only writes the
  upscale tier via `upscale --set-tier`. Pacing is the user's call in
  FlowBatch's own config.

### FROZEN CONTRACT with FlowBatch (do not change silently)

Agreed 2026-09-23. Changing the marker or the report schema means updating BOTH
sides and both handover notes.

1. **Marker** (convenience only, for manual runs): one un-prefixed stdout line
   `FLOW_PROJECT_URL=https://flow.google.com/project/<uuid>`. Never the
   contract - it couples us to log formatting and is lost if the process dies.
2. **Report file is the contract**: we pass `--report
   <production dir>\flow_prepare.json` to `node src/cli.js prepare --job
   <job.json> --report <path>`, and it is written ATOMICALLY as soon as the
   project exists (not at exit). Schema: `schemaVersion`, `projectUrl`,
   `projectId`, `project`, `created`, `jobName`, `preparedAt`, and
   `refs: [{name, kind, status, path}]` with `status ∈ uploaded | reused |
   generated | missing`. Read it defensively (retry once; a malformed or
   absent report is treated as "no project yet").
3. **Ownership**: the report is the INTERFACE, the DB is the TRUTH. Persist
   `projectUrl`/`projectId` on the production row and mirror `projectUrl` into
   the job we own. `prepare_flowbatch_job` must write projectUrl FROM THE
   DB - today it rebuilds the job wholesale and would lose it.
4. **Ref mode**: every ref present -> generation runs `refMode: "assets"`
   (attach by name, never upload, no duplicate project assets); otherwise
   `reuse`. `refs[].status: missing` replaces scraping `⚠ reference not found`
   out of the log.
5. **Precedence** for the project URL: production row -> channel
   `flow_project_url` -> global `studio.flowbatch_project_url` -> none. With
   none, log LOUDLY and set a production warning; Flow's "most recent project"
   fallback must never be silent.

### Where Flow references must live (2026-09-23)

A shotlist can declare a refs registry (`{"hero_kimono_woman":
"refs/hero_kimono_woman.png"}`) and per-image `refs: ["hero_kimono_woman"]`.
Those names resolve against the production's own `refs\` folder, which seeding
fills from the channel's `refs_dir` (or `bible_dir\refs`). Make sure every name
the shotlist uses exists there; text-only consistency is fine for test runs,
but add real refs before publishing.

### Restart the dashboard after Python changes (2026-09-23)

Flask auto-reloads TEMPLATES but not Python, so after committing a change the
running dashboard can render the NEW form fields while its OLD route code
silently drops the new form keys - the symptom is "I set values, saved, and
they are gone when I reopen", with some fields (added earlier) still saving
fine. Check the listener's start time against `git log` before debugging the
code:

    Get-NetTCPConnection -LocalPort 8540 -State Listen |
      ForEach-Object { Get-Process -Id $_.OwningProcess } |
      Select-Object Id, StartTime

Restart with `.venv\Scripts\python.exe wr.py serve --port 8540` (run it
persistent so it survives the session). This bit the per-channel gate fields
on 2026-09-23: server from 07:50, fields committed 13:20/13:59.

### Genre is the join key - keep it forgiving (2026-09-23)

Monitored channels feed an own channel through `genre`, and it is free text on
both sides. Two guards, both added after both of the user's channels sat on
`genre = general` and silently found zero candidates:

- `producer.candidates` matches with `LOWER(c.genre) = LOWER(?)`, so
  `Human & Animal`, `human & animal` and `HUMAN & ANIMAL` all work.
- The own-channel genre input is a `<datalist>` of the genres that actually
  exist on monitored channels (free text still allowed for a new genre), and
  My Channels shows "no monitored channel has this genre" when a channel's
  genre matches nothing - that warning is the difference between "Auto Run
  does nothing" and knowing why.

### Fewer ports: the dashboard is the control surface (2026-09-23)

The pain was operational sprawl, not the HTTP boundary (the 4s My Channels
load was a per-channel blocking call - fixed in 504cb7d). What is actually
needed, and when:

| Tool | Port | Needed when |
| --- | --- | --- |
| WhisperRadar dashboard | 8540 | always |
| Renderly backend | 8022 | Renderly engine only (Gemini, imports, upscale) |
| Renderly frontend (Vite) | 5173 | NEVER - WhisperRadar uses the backend API |
| FlowBatch | none | FlowBatch engine only (CLI spawns its own Chrome) |
| FlowBatch UI | 8787 | NEVER - optional frontend |

So the FlowBatch engine needs no extra service at all. Renderly's
start.bat is the main source of sprawl (it opens backend + frontend
consoles); WhisperRadar starts the backend directly with uvicorn instead.

- **Settings > Tools** lists Renderly backend / FlowBatch
  with status and Start/Stop buttons (`POST /services/<name>/start|stop`), so
  no .bat needs to stay open. Status uses `services.MANAGER.status_cached`
  (stale-while-revalidate, never blocks a page render).
- **Stop only ever touches a service WhisperRadar started.** Stopping an
  untracked one returns "left alone". A force stop exists in the API
  (`force=1`, matches the listening port) but is deliberately NOT in the UI.
- `services_autostart` (Settings, default off) brings the backend up
  with the dashboard, in a background thread so boot stays instant.
- Keep the thread-spawn pattern defensive: set the `loading` flag, spawn, and
  reset the flag if the spawn raises - a stuck flag silently disables the
  cache forever (that bug bit both `services.status_cached` and
  `studio.renderly_channel_list`; `threading` was also missing from services).

### Per-channel vs global settings — map (2026-09-22)

The Settings page holds GLOBALS; every channel can override the production
ones on My Channels ("edit / defaults"). Resolution is always
**global <- own channel <- production** via `settings.for_production`.

Per channel (own_channels): narration voice, image engine, render mode,
upscale, auto-run on/off, per-day cap, topic pick, run window start/end,
candidate window (days), producer LLM, bible folder, refs folder, Google Flow
project URL, plus the Renderly channel mirror.

Global only (Settings): the Auto Run master switch (`autorun_enabled`), the
global per-day cap, scheduler on/off + interval, notifications, "stop services
after images". These are process-level, not per-channel.

REMOVED (2026-10-06): the global "Narration voice" (`default_voice`) and the
global per-genre "bible/refs folders" (`seed_dirs`) settings. Voice and art
direction are per channel (My Channels: voice, bible, bible_dir, refs_dir) or
per production. Stored values of the old keys are ignored.

The producer evaluates the run window, candidate window, topic pick and LLM
PER CHANNEL (`settings.for_production`), so one channel can run at 02:00-03:00
with a 7-day window and DeepSeek while another inherits the globals. The
scheduler calls `producer.build_plan`, so it inherits all of that for free.
An unknown `producer_llm_provider` on a channel is stored as NULL (inherit)
rather than kept, so a stale provider name cannot silently break picking.

Two bugs found 2026-09-22 and fixed - both places had ignored the channel:
- the audio-stage picker passed only `prod["voice"]`, so it showed the
  placeholder even though the run used the channel's voice. It now preselects
  the effective voice and shows "Voice inherited from channel: X" (or
  "this production").
- `stage_action("audio")` reported "(default voice)" regardless of the
  channel; it now names the voice and where it came from.

### Per-stage service management — DONE (2026-09-22)

`whisperradar/services.py` (`MANAGER`) owns the external services the images
stage needs. `services_for(engine, mode)` says what a run requires:
renderly -> backend (for the PL/PR API pass); flowbatch -> nothing (a CLI that starts/stops its own browser).

Conservative rules - keep them:
- A service that already answers is YOURS: never tracked, never stopped, so a
  Renderly backend you started by hand is safe.
- Only processes this module spawned are stopped, and only when
  `services_managed` is on (Settings, default OFF).
- The Renderly backend is shared and is deliberately never killed (`release`
  logs "leaving the Renderly backend running").
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
`db._add_column_if_missing`). Resolution order for the FlowBatch engine:
the images-stage field, then the channel's URL, then the global
`studio.flowbatch_project_url`; when all are empty the job simply omits
`projectUrl` so FlowBatch falls back to its own `config/settings.json` or
Flow's most recent project. Editable on My Channels, and the images stage
shows a URL box only while the FlowBatch engine is selected.

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

### FlowBatch as a second image engine — DONE (2026-09-22)

`default_engine` (global in Settings, per channel on My Channels, overridable
on the images stage) now actually switches the images stage:
- `renderly` - FlowBatch for most shots plus the Renderly API for PL/PR (see
  "Consolidated engines" below).
- `flowbatch` - the standalone Playwright Flow CLI, consumed IN PLACE
  from `studio.flowbatch_repo` (never vendored: its Google session lives
  in a gitignored `profile\`, and a fresh profile means a new Google login,
  which is exactly where the Flow abuse-block lives).

Code: `studio.flowbatch_ready()`, `prepare_flowbatch_job()`,
`set_flowbatch_tier()`, `_adopt_flowbatch_outputs()`,
`run_imagegen_flowbatch()`; `autorun._run_images` dispatches on
`eff["engine"]`; the images panel has an Engine select.

DECISIONS worth keeping:
1. **The shotlist `style` is usually NOT sent.** Flow refuses prompts over
   ~2450 chars with the SAME message as rate limiting, and WhisperRadar's
   shotlist style alone measured 4000 chars - sending it would fail every
   item. The per-image prompts already carry the art direction. The style is
   sent only when `longest prompt + style <= 2420`.
2. **Refs are passed as NAMES**, with the shotlist `refs` registry plus every
   file in the production's `refs\` (keyed by stem) as the job's name -> path
   map, and `refMode: reuse` - so FlowBatch attaches existing Flow project
   assets by name instead of re-uploading (its README: uploads duplicate
   project assets).
3. **Outputs go to `flow_images\` and are adopted into `images\`**, preferring
   the upscaled `<stem>_<tier>.png` over the 720p master, so `images\` never
   holds two graded copies of the same shot.
4. **Upscale tier** is written to FlowBatch's own
   `config/upscale.local.json` via `upscale --set-tier` (what its UI does).
   Mapping: upscale 0-4 -> off/1k/2k/3k/4k.
5. **Job name is `wr-<pid>`**, so FlowBatch's `state\wr-<pid>.json` makes
   a re-run resume the items that are still missing.
6. Rate limiting is waited out by FlowBatch itself (cooldown 180s, up to
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

### LLM settings are DB-only — DONE (2026-09-25)

- config.yaml no longer carries `studio.llm_providers` / `studio.llm_default`
  (or the legacy single-LLM keys) — the `settings` table is the single source
  of truth. `Config` still exposes `studio_llm*` attributes, always empty, for
  compatibility only.
- `studio._saved_llm_settings(cfg)` reads the saved nested `llm_providers` +
  `llm_default` in one connect; `studio.providers()` / `providers_nested()` /
  `llm_default()` build on it. Empty DB = no providers (no yaml seed), and the
  Settings > LLM providers page is the only place to configure them.
- Provider precedence is unchanged: production `llm_provider` -> channel
  `producer_llm_provider` -> DB `llm_default` (Settings > LLM). There is no
  yaml fallback anymore; an unset global default means no default provider.
- The manual Studio generate routes (style/script/shotlist) fall back to
  `autorun._default_provider(cfg, pid)` instead of a yaml default.

### Provider deletion cleans references + full reset — DONE (2026-09-26)

- Saving the providers editor now diffs old vs new names: a DELETED provider
  has every reference cleared (Default LLM, both judge picks, channels'
  producer LLM, production pins) - references to KEPT providers survive. A
  stale reference otherwise keeps surfacing as errors about a provider that
  no longer exists (the brother's "claude" errors).
- "Reset LLM providers (fresh start)" button on Settings > LLM wipes ALL
  providers and references at once (confirm-guarded) - for a broken or
  inherited setup. After a reset: add the gateway, pick a Default LLM.
- A trap fixed alongside: stage runners no longer auto-pin
  `productions.llm_provider` (an implicit pin outranked the Default LLM
  forever); a one-time migration cleared the implicit pins.

### Supplied-refs inventory in the planning prompt — DONE (2026-09-26)

- The planner has no filesystem access, so a character the user uploads could
  only reach the shotlist through bible conventions - and the upload route
  slugifies file names, so a mismatch silently turned the character into a
  "generate" candidate.
- `studio.find_supplied_refs(pdir)` lists the image files in the production's
  `refs\` folder EXCLUDING the ones the refs generator wrote (tracked in
  `refs_generated.json`), and `_run_shots` passes them to
  `shotlist_prompt(..., supplied_refs=...)`, which appends
  "INPUT 6 - SUPPLIED REFERENCE FILES ALREADY ON DISK": exact `refs/<file>`
  paths, attach-by-name rules, no refPrompts for supplied files, and the
  reminder that everything else is generated on the fly (CH_/BG_/OBJ_).
- The shotlist gate still fails a plan that declares supplied refs but never
  attaches them. Mixing one supplied character with on-the-fly backgrounds
  and props is now prompt-driven, not convention-driven.

### FlowImagesGen renamed to FlowBatch — DONE (2026-09-25)

- The tool's repo is now `D:\Repos\FlowBatch` (same layout: `src\cli.js`,
  `config\`, `state\`, `profile-*`, NEXT_SESSION.md). Its CLI commands, flags
  and job JSON are UNCHANGED, so the frozen contract below still holds - only
  the name moved.
- Adopted here wholesale: engine id `flowbatch`, config keys
  `studio.flowbatch_repo` / `studio.flowbatch_project_url`, studio functions
  (`flowbatch_dir/ready`, `prepare_flowbatch_job`, `run_imagegen_flowbatch`,
  `run_flowbatch_prepare/refs`, `set_flowbatch_tier`, `_adopt_flowbatch_outputs`,
  `_flowbatch_cmd`), job files `flowbatch.json` / `flowbatch_refs.json` in
  production dirs, and all UI labels.
- `db._migrate` carries stored engine choices over
  (`own_channels.default_engine` and the global `settings.default_engine`:
  'flowimagesgen' -> 'flowbatch'), idempotent on every startup. Older entries
  in this file still say FlowImagesGen - same tool.

### Settings page regroup — DONE (2026-09-25)

- `settings.GROUPS` (settings.py) now renders: LLM (default LLM + producer
  LLM only — every LLM *setting* lives here), Auto Run (no LLM duplicate),
  Script & shotlist (both gates + their judge dropdowns), Production & images
  (engine/voice/seed dirs + image batch controls), Video render, Scheduler,
  Notifications, Service handling.
- Each settings key renders exactly once, so the duplicated provider dropdowns
  (same key on two tabs) are gone. The per-job LLM picks are the judge
  dropdowns on the Script & shotlist tab; Auto Run's LLM is the producer LLM
  on the LLM tab.
- The LLM providers editor (gateways/keys/models) is its own `<form>`, so it
  cannot nest in the save form — it renders in a second
  `<div class="tabpanel" data-tab="LLM">` right after it, visible ONLY on the
  LLM tab (the tab JS toggles every .tabpanel by data-tab).
- There is no separate "shotlist writer LLM" by design: style/script/shotlist
  all use one writer chain (production -> channel -> Default LLM); only the
  judges are separately selectable.
- channels.html section labels renamed to match ("Production", "Script",
  "Shotlist").

LLM PROVIDER SEAM: providers now carry `api` (default "openai"); only the
OpenAI-compatible `/chat/completions` adapter exists, in `CHAT_APIS`
(studio.py). `llm_generate` dispatches on it and raises a clear error for an
unimplemented api; `provider_ready` returns False for one, so the UI shows it
as not ready. Adding Claude/GPT: config-only through any OpenAI-compatible
gateway, or a new `*_chat` function + one CHAT_APIS entry for a native API.
New settings: `producer_llm_provider` (empty = the Default LLM in Settings >
LLM) and
`candidate_window_days` (default 90).

### Seeding + create-form channel picker — DONE (2026-09-22)

- `studio.seed_production(cfg, conn, prod)` copies `bible.md` and `refs\`
  into a production folder. Source order: the production's own channel
  (bible text / bible_dir / refs_dir; a bible_dir holding a refs\ subfolder is
  used for the refs too). There is no global fallback. Idempotent - it
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
a subprocess) and FlowBatch (CLI, Google session in a gitignored profile)
are all used from their own checkouts via config paths. A submodule copy would
strand Renderly's database/storage/profile and add a second login for
FlowBatch. Paths live in config.yaml: `imgtovideo_repo`, `renderly_url`,
`flowbatch_repo`.

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
  flow/api ('auto' = API; 'flow' = every shot on FlowBatch).
- images stage: flow mode passes the own channel's mirror NAME, api mode
  resolves its Renderly channel ID (`studio.resolve_renderly_channel(...,
  create=True)`) and passes it + the effective upscale to
  `studio.run_imagegen(cfg, pdir, channel=, upscale=)`.
- audio stage: TTS voice = production.voice -> channel.default_voice
  (no global voice since 2026-10-06; empty = the TTS built-in default).
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

### Cloned voices in the favorites picker — DONE (2026-10-03)

Favorites mode showed ONLY starred voices, so your cloned voices
were unreachable from the Studio audio stage. The clone catalog was verified
live with a read-only GET first: `GET /v3/voices?provider=clone` lists
exactly your own clones (voice_id `clone_<id>`; this setup had
Stickly/Animal Channel/To Live and More, total 3, no shared catalog).

- `ai33.cloned_voices(cfg, refresh=False)` — `_fetch_voices(cfg, "clone")`
  + 10 min cache (`_clones_cache`), normalized like the catalog.
- `ai33.voices()` favorites branch now returns favorites + clones deduped by
  voice_id; a failed clone fetch is logged and favorites still come back;
  the shortlist/catalog fallback only fires when BOTH are empty.
- No new source sentinel; `?source=favorites` and
  `studio.ai33_voice_source: favorites` both pick this up. `clone_...` ids
  already round-trip through `generate()` (PROVIDERS includes "clone").
- studio_detail.html labels: "favorites + cloned voices" (loading/empty/
  count messages + favLink title).
- tests/test_favorites_include_clones.py — patched `ai33._request`, no
  network: merge+dedupe, explicit source, clone-failure keeps favorites,
  both-empty falls back.
- NOTE: ~29 webapp route tests error in tearDown (locked temp wr.db,
  WinError 32) at baseline with a live dashboard on :8540 — pre-existing,
  not related to this change.

### OpenSpeaker favorites for the voice picker — DONE (2026-09-22)

The audio-stage picker can now list the voices starred in OpenSpeaker
(ai33.pro). The endpoint was NOT in the docs and was verified live with a
read-only GET before wiring it:

- `GET /v3/favorites` -> `{success, favorites: [{created_at, provider,
  voice_id, voice_data}]}`. `provider` there is a generic "v3" — the real
  provider is the `voice_id` prefix. `voice_data` carries name/gender/
  language/accent/preview_url. 9 favorites here.
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


### Auto-run pipeline design

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
  stage list, which hooks will be used (TTS / Renderly API / FlowBatch incl. channel,
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
  already order-agnostic. Per-image "refs" entries are resolved by the engine
  itself against the production's refs\ folder (the old flow_batch.json that
  pre-resolved them was unused and has been removed).
- images: render missing shotlist images via the saved render_mode, defaulting to
  FlowBatch + saved flow_channel/flow_project/upscale — reuse the images-stage form fields.
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

- Batch sheet: the planning brief, the external planner prompt and the
  cut-off continuation prompt ask for the shotlist JSON ONLY (Document 1 is
  gone). `studio.batch_sheet_text(data)` builds the readable sheet from
  shotlist.json; `/studio/file/<pid>/batch_sheet.txt` serves that, never a
  stored file. parse_shotlist_output still tolerates a sheet after the JSON
  (custom briefs). The external planner prompt carries the channel's brief
  inline; in files mode the only attachment is narration.txt.

## Consolidated engines (branch feat/consolidate-engines)

- `engines/flowbatch/` is FlowBatch (Playwright Flow CLI) and
  `engines/renderly-api/` is the Renderly FastAPI backend (port 8022, API
  contract unchanged, own `renderly.db` + `storage/`). Both are plain copies;
  runtime data (`node_modules`, `profile`, `state`, `renderly.db`, `storage`)
  is git-ignored and must be copied or recreated (`npm install`, `npm run login`).
- `studio.flowbatch_repo` and `studio.renderly_api_dir` default to those
  folders. `services._start_renderly` runs `uvicorn main:app` from
  `engines/renderly-api` with the repo `.venv` (packages are in
  requirements.txt - rerun setup.cmd after pulling).
- Engines are: `flowbatch` = every shot on FlowBatch;
  `renderly` = FlowBatch FIRST for every shot except PL/PR
  (`run_imagegen_flowbatch(... skip_motion=("PL", "PR"))`), then the API for
  PL/PR (`run_imagegen(... motion_filter=("PL", "PR"))`), then - only if the API
  missed some (quota/outage) - a FlowBatch pass for the leftover PL/PR as 16:9.
  The API goes last so nothing is paid for while the bulk is unfinished, and a
  resume round never repeats it (`studio.missing_shot_files` decides which
  passes are needed). A start-of-run Renderly check logs early if it is down.
  The job-wide art style is judged against the WHOLE shotlist's longest prompt
  (never just the remainder), falls back to style.md, and a style that cannot
  fit Flow's 2420-char ceiling puts a visible warning on the production.
  Stage 7 has a "Kill image generation" button (`/studio/<pid>/images/stop`;
  see the KILL switch section at the top).
  Render mode `flow` (stored per channel/production) now means "all shots on
  FlowBatch": `studio.effective_engine(engine, mode)` maps engine renderly +
  mode flow to flowbatch; `auto` means api.
- Still external: ImgToVideo (.NET) only.

## Shot-type caps and host-in-frame (briefs.normalize_types)

- Per channel only (My Channels > Planning brief; there is deliberately no
  global setting): a max share (%) for each shot type (SCN, CU, INF, CMP, PROC,
  HYB, OVR) plus one combined cap for all diagram/infographic types
  (INF+CMP+PROC+HYB+OVR). Empty = no limit, 0 = never used.
- **Host in frame** rules exist only when the channel names its host's ref
  (e.g. CH_REIKO); then each type has a share of ITS shots that must show the
  host (0 never, 100 always, +/-15 points otherwise). A shot shows the host when
  its image entry lists that ref in `refs`.
- Stored as JSON in `own_channels.brief_types`; `settings.for_production` puts
  it in `eff["brief_types"]`; `briefs.resolve_profile(types=)` carries it on the
  MotionProfile. Nothing set = today's brief, byte for byte.
- Enforced as hard faults in `studio.shotlist_type_faults` (called from
  `shotlist_pacing`) and written into the planner/judge prompts. The brief's
  visual->motion table follows the caps (`briefs.adapt_motion_table`): rows of a
  banned type are dropped and PU/PD, PV, PL/PR get a plain-scene row instead.


## Phone access (branch mobile-access)

- `whisperradar/remote.py` - the password gate. A request straight from this PC
  (loopback, no proxy headers) works as always; anything else needs the
  `WR_PASSWORD` environment variable (login page + signed session cookie,
  `data/.web_secret` holds the signing key, 5 wrong tries lock a client for a
  minute). No password set => remote requests get 403, so binding to a network
  address by mistake exposes nothing. Forwarding headers (`X-Forwarded-For`,
  `Tailscale-User-Login`, ...) on a loopback request count as remote, so
  `tailscale serve` / a reverse proxy cannot skip the password.
- `python wr.py serve --remote` (or `start_dashboard_phone.cmd`): waitress on
  127.0.0.1 AND this PC's Tailscale IPv4 (`remote.tailscale_addresses()`, from
  the tailscale CLI), no debugger, no auto-reload. A non-loopback `--host` in the
  normal reloading mode now runs with the debugger OFF (it executes code).
- `static/mobile.css` is linked last by every page and acts only under 760px:
  stacked fields, 16px inputs (no iOS zoom), 42px touch targets, scrolling
  tables/stepper, Copy button above its box, folder-picker buttons hidden (the
  chooser opens on the PC). The POST host check also accepts this PC's
  `<pc>.<tailnet>.ts.net` name.
- Phone setup: Tailscale on the PC and the phone (same tailnet), `setx
  WR_PASSWORD "..."`, run `start_dashboard_phone.cmd`, open the printed
  `http://100.x.y.z:8540`. Do not forward the port to the internet.


## Shotlist fault-fix loop (branch shotlist-fix-loop, 2026-10-05)

The built-in shots stage now works like an external LLM chat: the checker
reports exactly what is wrong, the writer fixes ONLY that, the checker looks
again. Before, a plan with faults was thrown away for a full re-plan (which
regenerates everything and does not reliably keep what passed); only weak
prompts were patched.

- `_run_shots` (autorun): a plan WITH faults -> `_attempt_shotlist_fix` (new)
  sends `studio.shotlist_fix_prompt` (faults + weak prompts + a one-line-per-shot
  overview + the full entries and narration of just the shots concerned, picked
  by `shotlist_fix_scope`). The reply is a JSON DELTA, never the whole plan:
  `{"shots": [...], "images": [...]}`. `parse_shotlist_fix` reads it (a cut-off
  reply is continued like the planner's), `apply_shotlist_fix` merges it: reply
  shots REPLACE every existing shot whose cues overlap (split a long shot, fill
  a gap), reply images replace by file name or are appended, images only the
  replaced shots used are dropped. Then `review_shotlist_patch` re-judges just
  the changed assets and recomputes all faults (they are free).
- Weak-prompt-only plans still use the older prompt patch (`shotlist_patch_prompt`).
  An unusable or empty fix reply falls back to the old full re-plan with the
  fault list, so nothing is worse than before. Attempt cap, stall stop and
  best-ever are unchanged.
- The external flow (`external_prompts.py` and the studio prompt functions it
  uses) is untouched on purpose: its prompts are byte-identical to master
  (checked before/after; only the random VARIATION number in the revise prompt
  differs run to run, as on master). All new code is additive.
- Tests: tests/test_shotlist_fix_loop.py; test_shotlist_budget_and_stall switches
  the fix round off because it exercises the stall counter on the re-plan path.

## Reveal shots (branch reveal-effect, 2026-10-05)

One image with 2-4 items in a left-to-right row; the merged video shows slice 1,
then adds slice 2 when the narrator reaches item 2, and so on (a mask-style
reveal, built from plain stills so Premiere, CapCut and the ffmpeg preview all
handle it). Static (ST) only.

- Shotlist: a shot may carry `"reveal": [41, 42, 43]` - the cue at which each
  item appears (also `{"cues": [...]}`). `studio.shot_reveal(shot)` reads it,
  `studio.shotlist_reveal_faults(shots)` checks it (2-4 items, increasing, first
  cue = the shot's first cue, inside its cues, ST motion and `_ST` file name) and
  is part of `shotlist_structural_faults`.
- Per channel opt-in: `own_channels.brief_reveal` (NULL = off, 1 = allowed), a
  select in the channel's Planning brief; `MotionProfile.reveal` is set by
  `briefs.resolve_profile(..., reveal=eff["brief_reveal"])` and
  `briefs.render_brief` then adds SECTION 7B (`briefs.reveal_block()`) before
  SECTION 8. The field is exported/imported with the channel settings.
- Pacing: a reveal shot counts as the pseudo-motion `REVEAL` in
  `shotlist_pacing` - not as ST (no long-static fault, no ST share/hold cap),
  not against the motion-code share caps, and not rejected on a static-only
  channel. It still obeys the maximum hold.
- Judge: `alignment_prompt` adds per-item lines and `REVEAL_JUDGE_RULES` (item
  count, narrated order left to right, one equal slice per item, nothing
  crossing a slice line, one background) for chunks that contain a reveal shot;
  weak entries carry `reveal` so `shotlist_patch_prompt` keeps the layout.
- Merge side (ImgToVideo repo, branch reveal-effect): the planner expands the
  shot into consecutive stills (slices 1..k, rest black, short fade) and
  `RevealImageWriter` cuts them with ffmpeg into `out\reveal\`. The merge needs
  that ImgToVideo branch (or master once merged) checked out.
- Variants (2026-10-05, same branch):
  - Grid: `"reveal": {"cues": [4 cues], "layout": "grid"}` - one image, four
    quadrants revealed TL, TR, BL, BR (exactly 4 items).
  - Separate images, build-up: `"reveal": {"cues": [...], "assets": [files],
    "layout": "row"|"grid"}` - one image per item, each its own `images` entry,
    all `_ST`, the first = the shot's `asset`; earlier images stay on screen
    (the merge composites them cover-cropped into slots). "Replace" mode needs no
    field: ordinary consecutive shots.
  - `studio.shot_reveal_layout/shot_reveal_assets` read them;
    `shotlist_reveal_faults(shots, image_names)` checks layout, grid=4, one
    asset per cue, first asset = shot asset, `_ST`, present in images.
  - Judge: `judge_shots(data, prompt_by_file)` turns a build-up shot into one
    pseudo-shot per image (its own cues, `reveal_item`), so each image is graded
    against its own narration and patched by file name;
    `REVEAL_BUILDUP_JUDGE_RULES` asks for ONE centred subject with empty side
    margins (only the centre survives the crop). Grid wording is in
    `REVEAL_JUDGE_RULES`.
- Sound effects: any shot may carry `"sfx": "pop"` (plays at the shot start) or,
  on a reveal shot, a name for every item or a list (one per item, last repeats).
  `studio.shot_sfx` / `shotlist_sfx_faults` (name charset only, normal shot = ONE
  name, reveal list <= items). Built-ins: `studio.BUILTIN_SFX` = pop, ding, click,
  tick, whoosh, swipe (ImgToVideo generates them with ffmpeg; a file in the
  project's `sfx\` folder with the same stem wins). The text lives in the same
  SECTION 7B, so it is gated by the same channel setting.
- Tests: tests/test_reveal_shots.py.

## QUEUE for the next session (2026-10-06)

- **Flow "throttling" is usually not throttling (observed 2026-10-05).** When FlowBatch
  reports "stopped after too many cards failed in a row", Flow often HAS generated the
  images but FlowBatch could not retrieve them. Flow shows an error at first, then the
  images appear in the gallery once FlowBatch disconnects. Recovering them with the
  recover command and then continuing generation worked immediately, with no 60-minute
  wait. Already done: one gallery recovery before the first pause (`_recover_after_stop`).
  To do: use the same recover-then-continue step on the later pauses/rounds too, so the
  long wait is only a last resort (when recovery finds nothing and generation still fails).
- Flow window size: FlowBatch opens at 1512x950 (`config/settings.json`, `browser.viewport`);
  user to pick a size.
- Reveal "required" level: check that a re-plan now yields `reveal` fields; if models still
  skip lists, add a post-plan check.
- Replace the built-in `pop` sound (ImgToVideo, see its docs/next-session.md).
- Slide-in reveal (queued in ImgToVideo docs), after the Premiere Position fix is confirmed.
- Presentation override of Section 5/8 (ask first: touches the external prompt).
