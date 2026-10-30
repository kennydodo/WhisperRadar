# Flow references: check they exist, never upload them again

Written 2026-10-15. STATUS: NOT FIXED. Nothing in the code stops the upload yet.
(The only thing committed so far is the read-only diagnostic `npm run assets`, commit 1467fc0.)

## What the user wants
- The refs stage generates the references (CH_/OBJ_/BG_) inside the production's Flow project.
- The images stage must only CHECK that those references are in the project, and attach
  them by name. It must NOT upload the local ref files again.

## What happens today (seen in the log, production 39, 2026-10-15)
- 16:04:41 `node src/cli.js prepare ...` starts, 16:06:17 it finishes: ~1.5 min for 7 refs,
  which is the time of 7 uploads.
- 16:06:17 "all 7 reference(s) are in the project - refMode 'assets' (no uploads)".
  That message is MISLEADING: it only means the images are attached by name afterwards
  ("assets" mode). The uploads already happened inside prepare.

## Root cause
1. The refs stage generates tiles in Flow, but Flow does not title a generated tile with its
   ref name (CH_EVERYMAN ...). `studio._write_refs_job` sets `"nameAssets": true`, but
   FlowBatch ignores that key (`engines/flowbatch/src/lib/load.js` never reads it).
2. `engines/flowbatch/commands/prepare.js` (around lines 60-80) asks
   `driver.galleryHasAsset(ref.name)` (exact title, or title minus extension). The generated
   tile has no such title, so the answer is false -> it uploads the local file
   -> status `uploaded` instead of `reused`.
3. Result: every ref exists twice in the project (generated tile + uploaded copy), and every
   video pays the upload time.

## Fix options (pick one)
A. Name the generated tiles after their refs in the refs stage (implement `nameAssets` in
   FlowBatch: after a ref job finishes, rename each new tile to its file stem). Then
   `galleryHasAsset` succeeds and prepare reports `reused`. Cleanest, if Flow lets us rename.
B. Keep tile titles as they are and have the refs stage RECORD which tile belongs to which ref
   (tile id or thumbnail src, saved in `refs_generated.json`); prepare then trusts that record
   instead of searching by title.
C. Make prepare check-only for refs the refs stage generated: if the refs stage finished OK for
   this production/project, skip the upload and report `reused` without looking at titles;
   a ref that is really not there is reported `missing` (and the refs stage is re-run), never
   uploaded. Simplest, least robust.
Whatever we pick: prepare must never call upload for a ref that has a local file AND was
generated in this project. Uploading stays only for refs the user supplied themselves.

## Step 1 (needs Flow free - it uses the same browser profile as a running production)
Run `npm run assets -- --project-url <project url>` in `engines/flowbatch` to see how Flow
titles the generated tiles (saves `debug/assets-<time>.json` + screenshot). That decides A, B or C.
Current projects: production 39 `https://flow.google.com/project/3d9bc9ed-8ae0-4523-a475-b1bad7eb1fae`,
production 37 `https://flow.google.com/project/4893cd88-2583-44c9-9be8-c5a9cc4fa5b5`.

## Also pending (same area)
- Reference prompts must describe ONLY the subject (a basket = the basket, empty, plain
  background; a living room = the empty room). The brief template already says so (affects new
  plans). Still to do in Python: append a subject-only suffix in `studio._write_refs_job` by
  CH_/OBJ_/BG_ prefix for refs already planned, and add the wording to the planner text
  near studio.py ~4596 (`give each a CH_/BG_/OBJ_ name and a detailed refPrompts entry`).
- Test: prepare must not upload when the refs stage generated the refs; a ref truly missing is
  reported `missing`, not uploaded.

## Constraint
Productions 38/39 are running. Do NOT edit any `.py` file (the dev server restarts) until
the user says nothing is running. FlowBatch `.js` edits do not restart the server but are
read by the next `node` call, so they must also wait for an idle moment.
