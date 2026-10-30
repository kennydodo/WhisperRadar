# Flow references: canonical names with caption compatibility

## Update (2026-10-30)

Reference jobs now implement `nameAssets: true`. After saving a generated image,
FlowBatch re-identifies that exact result, opens its detail editor, saves the
shotlist key (for example `CH_AYA`), returns to the gallery, and verifies the name
in the asset library. The recorded mapping then uses that canonical name.
Ordinary shot jobs do not rename assets. A rename failure logs a warning and
keeps the caption mapping without generating or uploading a second copy.
Existing caption-named references are not bulk-renamed.

The controls were inspected in Flow's live detail editor; automated coverage is
offline. Verify one newly generated reference on the next normal refs run.

## Original caption compatibility behaviour (2026-10-16)

Measured with `npm run assets` on production 39's project: Flow lists a generated
image under a caption it writes itself ("Two apes sitting hunched",
"Mammoth standing on plain backgr…" - long ones are truncated with an ellipsis),
while an uploaded file keeps its file name (`CH_EVERYMAN.png`). The refs stage originally
could not name its tiles, so the images stage never found them by ref name and
uploaded the local files into the project: every ref existed twice.

## How it works now
1. **Refs stage** (`run_flowbatch_refs`, job flag `recordAssetTitles`): FlowBatch
   reads the asset library's titles before and after each ref and records the ONE
   new title (never a guess: none new, several new, or a title shared with another
   asset gives no caption). Written to `refs_titles.json`, merged by Python into
   `refs_project.json` as `titles` {ref name: caption}, valid for that project only.
2. **Images stage** (`prepare_flowbatch_job` -> job `refAssets`): `prepare` only CHECKS
   each recorded caption in the project (`reused`, source `gallery`) - it never
   uploads a generated ref. A caption that is not there is reported `missing` with
   `generated: true`.
3. **Missing ones are remade**: `run_imagegen_flowbatch` then regenerates just those refs
   inside the same project (`run_flowbatch_refs(..., force=True)`, `--no-resume`),
   rebuilds the job with the new captions and checks again.
4. **Generation** attaches a generated ref by its caption (`ref.assetName`, strict: no
   "first search hit"), never by upload. Refs you supplied yourself still upload.
5. A ref with no recorded caption (older productions, caption unclear) keeps the older
   behaviour: trusted from the record, uploaded only if the name cannot be found.

Helpers: `engines/flowbatch/src/flow/asset-titles.js` (truncated-title matching, new-title diff).
Tests: `engines/flowbatch/test/flow/asset-titles.test.js`, `tests/test_refs_by_title.py`.

## To check live once
Run the refs stage and the images stage on a fresh production; the log should say
`Flow named CH_...: "<caption>"` per ref, then `Reference CH_...: reused`, and the
Flow project must hold ONE copy of each ref. `npm.cmd run assets -- --project-url <url>`
lists the library.
