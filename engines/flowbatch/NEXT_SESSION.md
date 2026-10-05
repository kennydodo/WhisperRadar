# Next session

Handover notes. Delete this file once the list is clear.

## 2026-10-01 — `upscale --in-place` (offline post-download upscale)

`node src/cli.js upscale <file-or-folder> --tier 2k --in-place` now replaces
each PNG under its own name: it upscales to a temp file and renames over the
master only on success, and it SKIPS any file already at the tier (IHDR size
check, no pixel decode), so the pass is idempotent and safe to re-run.
`--in-place` and `--out` are mutually exclusive; the default (no `--in-place`)
behaviour is unchanged. WhisperRadar's new "Upscale after download (local)"
mode uses this for BOTH engines: after the batch downloads masters it runs
one pass over the images folder, and the images stage has a manual
"Upscale images (local)" button. Tests: test/upscale/inplace.test.js.

## 2026-10-01 — `recover` command added (WhisperRadar's gallery-adoption contract)

`node src/cli.js recover --job <job.json> --report <path> --output <dir>
--project-url <url>` adopts a stopped batch's already-generated results from
the project gallery WITHOUT generating: it unions the virtualized grid
(`driver.scanAssets`), keeps finished result tiles (canRedo + final host),
matches them to still-missing items by label prefix (>=20 chars, never
ambiguously), then by reading each tile's own "Reuse prompt" control
(`driver.readTilePrompt` - composer cleared again after EVERY tile), then by
submission order only when counts agree and prompts are distinct, downloads
via the same CDN-first path, upscales per config, saves under the job's exact
`file` names, marks the state item done, and writes an atomic report
{schemaVersion:1, recovered:[{id,file,how}], alreadyPresent, stillMissing}.
WhisperRadar calls it from the IMAGES stage's manual button
(`studio.run_flowbatch_recover`).

**Live-calibrated 2026-10-01** against production wr-20 (77 stuck stills). A
reloaded project gallery does NOT look like a fresh one: every tile is served
through the signed same-origin proxy `https://flow.google.com/asb/...=s1600-rw`
(only a just-rendered tile has the `flow-content.google/image/...` CDN URL the
generation path insists on) and each tile is named with a Flow caption
("Woman auctioning vintage camera"), not the prompt. The first live run found
0 results because of the CDN-only filter; `isRecoverableAssetSrc` now accepts
both URL shapes for recovery only, and snapshot entries carry a `label` field.
The redo read was verified live: it repopulates the composer with exactly
`item.prompt` (loadJob folds job.style into it), so `how:"prompt-read"` is the
reliable matcher for reloaded galleries, not the caption. **That project's
orphaned stills can now be adopted this way.**

## FROZEN CONTRACT with WhisperRadar (do not change silently)

WhisperRadar spawns this CLI and needs the Flow project URL back. Agreed
2026-09-23. Changing the marker or the report schema means updating BOTH sides
and this note.

**1. Marker (convenience, manual runs).** One un-prefixed stdout line as soon
as the project is known:

```
FLOW_PROJECT_URL=https://flow.google.com/project/<uuid>
```

**2. Report file (the actual contract).** WhisperRadar passes a path it owns;
we write it ATOMICALLY (temp + rename) as soon as the project exists - NOT at
exit, so it survives a later crash:

```
node src/cli.js prepare --job <job.json> --report D:\...\data\studio\5\flow_prepare.json
```

```json
{
  "schemaVersion": 1,
  "projectUrl": "https://flow.google.com/project/<uuid>",
  "projectId": "<uuid>",
  "project": "<name>",
  "created": true,
  "jobName": "wr-5",
  "preparedAt": "2026-09-23T16:46:55Z",
  "refs": [
    { "name": "CH_MAYA", "kind": "character", "status": "uploaded", "path": "..." },
    { "name": "BG_BATHROOM_01", "kind": "environment", "status": "reused", "path": null }
  ]
}
```

`refs[].status` is one of `uploaded | reused | generated | missing`.

**3. Ownership.** The report is the INTERFACE. WhisperRadar persists
`projectUrl`/`projectId` on the production row and mirrors `projectUrl` into
the job it owns. The job's `projectUrl` is regenerated from the DB, so it is
never the source of truth and a job rewrite cannot lose the URL.

**4. Ref mode.** When every ref reports present, the generation stage runs
`refMode: "assets"` (attach by name, never upload); otherwise `reuse`.

**5. Do not** write `projectUrl` into the job ourselves (`--write-url` stays
optional) - that would make two writers on a WhisperRadar-owned file.

## TL;DR

The pipeline works end to end. **30 of 85 shots are done**, 55 pending, 0 failed. The last
session fixed the two things that made batches slow and unreliable (see "What changed"), so a
clean cycle is now ~35-50s instead of ~86s, and a miss no longer costs ~10 minutes.

Batch runs use the `profile` profile — the default, so
no flags needed.

**New (2026-09-23, late):** a single-item run took **7m33s** from `Generating` to `Saved` with no
retry, no salvage warning and a correct 1376x768 master — far past the 300s generation timeout,
which should have fired. Unexplained; this is Remaining work **#1**.

The download/refusal misreport from the earlier notes is **fixed and merged** — see "What changed".

## Current state

| | |
| --- | --- |
| Branch | `main` at `9b6c266` (prepare + the #4 fix merged); `origin/main` is behind at `26f372c` |
| Done / pending | 30 done, 55 pending, 0 failed |
| Prepare command | `node src/cli.js prepare --job <job> --report <path>` — the WhisperRadar contract |
| Default profile | `profile` (change it on the web UI Settings page) |
| Upscale tier | **off** locally → masters only, no `_1k` |
| Outputs | `output/shotlist/<file>.png` master (+ `_1k.png` when the tier is on) |

The shotlist: `E:\YOUTUBE\PERSONAL FINANCE\These 10 Things At Home Worth Serious Money\shotlist.json`
Project: `https://flow.google.com/project/772a62aa-c204-4473-a27b-5e106a7f0b06`

## What changed last session

1. **`f17dc95` — result tiles are identified by their redo control, not text.** `innerText` is
   empty on `flow-grid-tile-container`, so the `textContent` fallback concatenates the hotbar
   icons into `favoriteredomore_vert` with no word boundary; the old `/\bredo\b/` test matched
   only by luck, so ~1 result in 3 was never accepted. The retry no longer reloads Flow, and
   `delayBetweenItemsMs` dropped 20s → 5s.
2. **`c704830` — byte ownership.** Every image on the page when a generation starts is hashed
   (sha1); a result is the newest non-reference tile whose bytes were never seen, served from
   `flow-content.google/image` (not the `flow.google.com/asb` placeholder host). This is what
   Renderly does, and it is what makes a reference tile impossible to save as a result.
3. **`26f372c` — the OS file chooser is intercepted.** Clicking "Upload media" opened the native
   Windows file dialog and `setInputFiles` never dismissed it, so it sat on screen for the rest
   of the run. `generate()` also dismisses any CDK popover, and ownership is seeded **only** from
   the pre-generation baseline.
4. **`ddd2f56` — the `prepare` command.** Opens or creates a job's Flow project, prints
   `FLOW_PROJECT_URL=`, writes the report atomically as soon as the project exists (before the
   reference work), then gets every reference into the gallery (`uploaded`/`reused`/`missing`) and
   clears the chips it leaves behind. It never generates. This is the FlowImagesGen half of the
   frozen contract at the top of this file.
5. **`2436332` — refusals are scoped to the attempt.** `detectRefusal()` and the `errorBanner`
   check both matched anywhere in the app (including `main :text-is('Failed')`), so one stale
   "You have not been charged for this generation" failed 16 consecutive WhisperRadar items twice
   each while Flow was generating every one of them. `generate()` now tags every alert already on
   screen (`markStaleAlerts`) and only a banner appearing AFTER the click counts (`freshAlert`).
6. **`2436332`/`9b6c266` — salvage and download retry.** A result that cannot be claimed before
   the deadline is salvaged instead of discarded (it still requires the redo control, so a
   reference can never be salvaged), and the download is retried up to 3x, re-resolving the tile
   each time because it moves as the grid changes. The timeout message now says "detection
   timeout, not a refusal".

## Remaining work

1. **INVESTIGATE: a 7m33s item that should have timed out.** On 2026-09-23 a single-item run
   (`state/merged-check.json`, item `MERGED_01`, throwaway project) went `Generating` 20:16:16 →
   `Saved` 20:23:49 = **7m33s**, with `attempts=1`, no retry, no salvage warning and a correct
   1376x768 master. `timeouts.generationMs` is 300000, so `waitForNewAssets` should have broken at
   300s and then either salvaged (logs a warning) or thrown (triggers a retry). Neither happened,
   so **the deadline was not honoured** — that is the puzzle, more than the slow generation.

   Measured on the same page (40 tiles): `snapshotAssets` 0.3s, `markStaleAlerts` 0.1s,
   `freshAlert` 0.1s, `noteSeenAssets` baseline 10.7s — none of the new code explains it. Every
   other run that day was 19-44s.

   Suspects, in order: (a) `fetchBytes` (src/flow/driver.js) has **no request timeout**, so a
   stalled CDN request blocks a poll indefinitely — add a timeout and a log line; (b) `generate()`
   is uninstrumented between the `Generating` log and `waitForNewAssets` — time
   `closeAssetLibrary`, `dismissOverlays`, the `requireEnabled` button wait and `markStaleAlerts`;
   (c) Flow genuinely queued the generation for ~7 minutes (the run had done ~15 test
   generations that day). Add per-poll timing to `waitForNewAssets` and re-run one item.
2. **Upscaler tuning.** It already ports Renderly's engine (device probe + cache, MAD content
   check, RGB flattening for alpha, GPU → auto → CPU Lanczos). Differences worth testing:
   probe with the configured model/scale instead of a fixed `realesr-animevideov3` x2; skip the
   engine when the source already meets the target; require output ≥ source outside the probe;
   A/B `realesr-animevideov3` + supersample against Renderly's `realesrgan-x4plus` at native 4x.
3. **FIXED, verify only: the download/refusal misreport.** The 156-item WhisperRadar batch
   (2026-09-23, `state/wr-5.json`, project `a84875f5-e27c-4bb2-a60c-2d558f24d92b`) had 16
   consecutive items fail twice each with
   `"error": "Flow reported a failed generation: \"You have not been charged for this generation\"."`
   while the project held **~24 more images than were saved**. Cause: the page-wide refusal and
   `errorBanner` checks attributed a stale banner to whichever item happened to be running — see
   "What changed" 5 and 6. Fixed by `2436332` + `9b6c266`: refusals are scoped to the attempt, an
   unclaimed result is salvaged, and the download is retried 3x.

   Verified by a live alert-scoping test (stale banner ignored, new banner still caught) and a
   2-item regression run. **Not yet observed triggering:** the salvage path and the download-retry
   path — watch for them in the next real batch. The 16 orphan images in that project are still
   there.
4. **DONE (2026-09-25, commit `518bf4b`): rename to FlowBatch.** The product is now FlowBatch:
    folder `D:\Repos\FlowBatch`, `package.json` name `flowbatch`, `FlowBatch.bat`, README, UI title,
    CLI help and log line, example-job `project` names. Kept by design: the `data-flow-imagesgen-stale`
    DOM attr, state-file names, the git remote (GitHub repo renamed by the user), and the WhisperRadar
    contract (`FLOW_PROJECT_URL` marker / `prepare --report` schema) — the contract marker and schema
    are unchanged. WhisperRadar config/shortcuts that point at the old folder or CLI path must be
    updated on that side.

## Known gotchas (do not re-derive)

- **Never identify a result by tile text or `src`.** Use the `Reuse prompt` control (present only
  on generated tiles) plus byte ownership. `src` changes when a thumbnail lazily loads.
- **A reference tile can mount after the generation and take grid index 0**, pushing the result
  to index 1. Do not require the result to be at index 0.
- **Seed ownership only from the pre-generation baseline.** Hashing during the wait marks the
  result itself as seen — that alone made both items of a run time out.
- **`files: []` plus a policy message means the DOWNLOAD failed, not the generation.** A stale
  page banner is no longer attributed to the item (see "What changed" 5), but if this shape shows
  up again, check Flow's grid for the image before re-running — a re-run leaves a duplicate.
- **A missed asset normally costs the full `timeouts.generationMs` (300s), then succeeds on
  retry.** Check `debug/error-*.png` before concluding anything about the Flow standing. Note the
  2026-09-23 run where that 300s deadline did **not** fire — Remaining work #1.
- **`fetchBytes` (src/flow/driver.js) has no request timeout.** A stalled CDN request can block a
  poll silently, which looks exactly like a hang with no retry — Remaining work #1.
- **Standing is the gate.** A healthy session generates normally; a distrusted one is refused every
  time in ~3s and each attempt lowers the score further — do not retry it.
  `--agent on` is the escape hatch on a distrusted session.
- **`maxCooldowns: 0` (and `cooldownSeconds: 0` locally) means a refusal STOPS the batch** rather
  than waiting — intentional, since the block is a reCAPTCHA score on the profile.
- **Agent mode OFF is the default and preferred** — it keeps per-item model/ratio/output control.
- **The Flow page loads once per batch.** Between items the composer is cleared in place; do not
  reintroduce a per-item reload.
- **Ingredient chips live in `flow-ingredient-bar`, not inside the editor.** Select-all in the
  editor does not remove them; each chip has its own remove control.
- **Model names are prefixes of one another** ("Nano Banana 2" vs "Nano Banana 2 Lite") — matching
  must be exact.
- **Prompt length ceiling is ~2450 characters** for style + scene combined.
- Deleting a `running` item's status is unnecessary — stale `running` resets to `pending` on load.

## Not wanted

- `shots` metadata (`cues`, `scene`, `motion`, `transition`) — parsed away on purpose.
- `refMode: "mention"` — implemented, never verified; `reuse` is what works.

## Housekeeping

- `profile-fresh` (58 MB) and `profile-test` (0 MB) are dead and can be deleted.
- `debug/` holds stale failure captures (screenshots + HTML) that can be cleared.
- The throwaway test project `358ac03f-de30-4726-9302-c57ce29572ce` holds ~17 refs and ~10 test
  images; delete it in Flow if it is not wanted.
- The FlowBatch `profile` is now a dedicated Google profile, separate from Renderly's own profile,
  so the two tools' reCAPTCHA signals are isolated. The previous profile is kept aside as
  `profile-old-backup`; the local sign-in details live in the git-ignored `PROFILE.local.md`.

## 2026-09-24 — WhisperRadar refs stage: four issues to fix

WhisperRadar now has a `refs` stage that invents ON-THE-FLY references (`refs` path `null` +
`refPrompts`) and passes SUPPLIED refs (`refs` path -> a file) straight to this CLI. Three test
productions (one per channel) exercised all three modes; the refs themselves came out right, but
the images stage failed in the ways below. Agent mode stayed OFF throughout. Projects/state named
so the runs can be reproduced.

### 1. `assetTile` detection misses results whenever refs are attached

- Job `wr-7`, project `7585c0da-f850-41f1-ad75-b175ab1ff20d`, 4 images, 2 refs each (refs were
  generated by this CLI in the same project, so `prepare` reported `all 4 reference(s) are in the
  project - refMode 'assets'`).
- Item `S01_01_SCN_ZI` failed BOTH attempts with
  `No new result tile appeared within 300s (detection timeout, not a refusal)`, `files: []`,
  status `failed`; `--fail-fast` stopped the run so `S02-S04` never generated.
- The renders DID happen: the project's grid shows 9 tiles (the 4 refs + the image results); see
  `debug/error-S01_01_SCN_ZI-2026-09-24T15-55-38.png`.
- Control: the same CLI rendered 6/6 cleanly with NO refs (job `wr-8`). So detection breaks
  specifically with refs attached — most likely the reference tiles / ingredient chips change the
  grid, or the `Reuse prompt` + byte-ownership identification is confused by the ref tiles.
  Re-check against "Never identify a result by tile text or `src`" and the
  "a reference tile can mount after the generation and take grid index 0" gotcha.
- Net: refs attach and generation succeeds, but nothing is claimable/downloadable.

### 2. `prepare` and `generate` disagree on whether a ref is in the project

- Job `wr-9`, project `4e8cbaa4-f6f7-4df9-9a16-71e40cb52330`.
- `prepare` printed `all 3 reference(s) are in the project - refMode 'assets' (no uploads)` and its
  report listed `BG_LIVING_ROOM_01` with status `uploaded`.
- `generate` then logged `Reference "BG_LIVING_ROOM_01" is not in the project's assets, and refMode
  "assets" never uploads.` — same project, same job, opposite answers. Fix: one source of truth for
  per-ref presence, and only choose `refMode: "assets"` if every USED ref is actually attachable.

### 3. `promptReferenceChip` confirmation fails after an upload

- Same run: `Uploaded 2 reference image(s) but could not confirm them in the prompt box. Calibrate
  "promptReferenceChip" if generations ignore the references.` followed by
  `Attached 2 reference(s) but could not confirm them in the prompt box; refusing to generate
  without them.` (both attempts; `--fail-fast` stops).
- Either the `promptReferenceChip` selector is stale, or the confirmation reads the wrong node.
  Run `npm run discover` with refs attached and recapture.

### 4. The generated job declares every seeded ref, and mixes relative/absolute paths

- `prepare_flowimagesgen_job` copies EVERY file in the production's `refs\` folder into the job's
  top-level `refs` map: 26 entries for a channel seeded with 27 files, when the images use only 3.
- The used refs keep the shotlist's RELATIVE path (`refs/MAYA.jpeg`); the auto-added ones are
  ABSOLUTE (`D:\...\refs\BG_BACKYARD_01.png`).
- `prepare` only acted on the used refs, so this is not a proven cause of #2/#3, but 20+ unused
  declarations and mixed path shapes are noise that can confuse presence checks and `refMode`
  selection. Trim the registry to names actually used by `images[]`, and resolve all paths absolute.

### 5. A stale "done" state item whose files were deleted is not re-generated

- `state/wr-7-refs.json` still listed `CH_HOST` as done from an earlier run, so the regenerate
  skipped it and the adoption reported `1 could not be generated: CH_HOST` (the output had been
  deleted with the production's refs/). Deleting `state/wr-7-refs.json` regenerated it.
- Consider treating a `done` item whose `files` no longer exist on disk as `pending`.

### Outcome (fixed 2026-09-24, later session)

1. **FIXED — `6a8d1ac`.** The size guard was the whole cause. In a refs run every tile renders
   **1376x768**, the `.png`-labelled reference tiles included, so `echoesReference` rejected every
   real result as "the size of a reference" and each item burned the full 300s. Measured on the
   wr-7 project (`7585c0da`): all nine tiles, refs included, at 1376x768. Verified on wr-7's own
   failing item (`S01_01_SCN_ZI`, 2 refs): saved on the first attempt in 27s.
2. **FIXED — `9f1d8c2`.** Not two disagreeing matchers: `prepare` reported `uploaded` while Flow
   had only *shown* the upload. A fresh upload is not committed to the project straight away — a
   15.3 MB background looked present right after the upload and 15s later, was gone in a new
   session, and turned up minutes after that. `prepare` now uploads through the same path
   `generate` uses and proves durability by reloading the project, reporting `missing` when the
   upload did not survive. Its old helper dismissed the picker with Escape, which discards an
   upload outright, and is deleted. So `uploaded` in the report now means "verified after a
   reload", and the contract's `refMode: assets` rule is safe. Verified on wr-9's project: both
   refs attached, saved in 22s.
3. **NOT A BUG — same cause as #2.** `promptReferenceChip` is fine: three candidates, all
   counting correctly, and the refusal to generate was correct because a reference really was
   missing. No selector change needed.
4. **WhisperRadar side.** FlowImagesGen resolves the mixed shapes correctly — `locatePath` tries
   the job's own directory first, then the repo root — and only ever acts on refs an item
   actually uses (`prepare`'s report listed 3, not 26), so the extra declarations are noise only
   there. Trim the registry to used names in `prepare_flowimagesgen_job`.
5. **FIXED — `f1956c4`.** `RunState.clearMissingFiles()` reopens a `done` item whose recorded
   files are gone, and the run logs which items it reopened.

Operational note: run refs jobs **without `--fail-fast`** until a batch is clean — a single
failed item currently stops the run, which hid the rest of wr-7 and wr-9.

### WhisperRadar-side (not this repo, noted for context)

- WhisperRadar's `renderly_upscale: 4` made the refs job upscale to 4K (`_4k` variants), not the
  2K the tier doc implies.
- `glm-5.3-flash` hung on the ~38.5k-char shotlist prompt (brief + a big channel bible);
  `deepseek` handled it in ~100s. Unrelated to this repo but it blocks the same pipeline.

### Housekeeping from this session

- Throwaway Flow projects created 2026-09-24 (delete if unwanted):
  `8958444e-8651-4867-91e7-11f2114f233c`, `e4945f80-5e53-43fa-90aa-4dfce93361dd`,
  `7585c0da-f850-41f1-ad75-b175ab1ff20d` (wr-7), `b7567302-44d7-4597-b8ee-fa040a187627` (wr-8),
  `085a027e-2efc-4d00-9e12-95172714c3cc`, `dcc88db6-5d37-4669-8713-0bb2a7ef7568` (wr-9),
  `4e8cbaa4-f6f7-4df9-9a16-71e40cb52330` (holds the 3 uploaded wr-9 refs).
- `ed861db8-2c03-4c8b-afd7-78852751017c` was created on a flagged second profile by mistake
  (the CLI cannot delete projects); remove it in Flow if reachable.
- A temporary screenshot helper `shot-tmp.mjs` was created in the repo root and deleted; nothing
  left behind.

## 2026-09-25 — 10-12 image runs: what held and what did not

Two productions went through this CLI end to end today (both flowimagesgen):
pid 13 (The Nature Made Us, 12 images, NO refs) → **12/12**; pid 14 (Kenny Invest,
11 images, 4 SUPPLIED refs) → **11/11** with 2 refs attached per image. So the
2026-09-24 fixes all held: no `assetTile` false rejection (no "result matches a
ref's size"), the durable-ref verification no longer produces the old
prepare-vs-attach disagreement, and `state` resume worked.

Two things still bit, both in `prepare`:

1. **A dead/foreign stored project makes prepare fail with a misleading error.**
   With the channel's old project (`89e82620`) the run reported
   `Could not locate the Flow UI element "promptBox" … The Flow UI may have
   changed. Run npm run discover`, which reads like a selector regression - the
   project simply does not open (deleted / signed in elsewhere). Flow's 404
   page has no composer. Consider detecting that (`…/404?reason=project`, or the
   page title) and either reporting it as "project gone" or falling back to
   `ensureProject` and creating a new one, as WhisperRadar now does on its side.
2. **prepare's reference step fails on a fresh project: `element is not enabled`.**
   After creating a new project it printed `FLOW_PROJECT_URL=…/784a8bc0` and then
   exited 1:
   `19 … waiting for element to be visible, enabled and stable - element is not
   enabled - retrying click action - waiting 500ms`.
   Generation then attached the refs itself and produced 11/11, so `generate`'s
   ref path is fine while `prepare`'s is fragile. Because the report is written
   BEFORE the ref work, the project URL is still valid - WhisperRadar was changed
   to keep that report instead of discarding it, but prepare should not fail the
   whole command on a ref-attach click timeout.

   Likely related to the registry bloat already noted above: the job declares all
   27 seeded refs, so prepare may be walking far more references than the 4 the
   images use. Trimming the registry to the used names is probably the real fix.

**Update 2026-09-25 — both fixed in `47be8dc`.** Re-tested on pid 14 with a dead
stored project: prepare now reports it as an unavailable project (naming the
404/`reason=project` signal and the likely causes) instead of the misleading
promptBox/selector error,
and a ref-step failure no longer costs the project - the report keeps a usable
`projectUrl`. A direct `prepare` on the created project then opened it and logged
`Reference MAYA: reused`, `BG_LIVING_ROOM_01/BG_HOME_OFFICE_01/BG_KITCHEN_01:
reused`, exit 0. (The ref step can still flake once - `element is not enabled` -
but it is no longer fatal.) The registry-bloat trim is still open.

