# Production folder layout (phase 1) - 2026-10-24

`whisperradar/prodfiles.py` is the one place that says where a generated file lives.
`path(pdir, name)` returns the new place when the file is there, the old (root) place when
only that exists, and the new place otherwise - so old productions keep working and new
files land in the grouped folders. `wpath()` does the same and creates the folder.

Stays in the root (the renderer reads these there): `audio/`, `subtitles.srt`, `shotlist.json`,
`images/`, `refs/`, `imgtovideo.json`, `visual_manifest.json`, `motion_gate.json`, `out/`, `clips/`,
and the text files (`script.md`, `style.md`, `bible.md`, `writing_style.md`, research notes, ...).

Grouped:
- `flow/` - flowbatch.json, flow_prepare.json, flowbatch_refs.json, flowbatch_recover.json, refs_generated.json, refs_project.json, refs_titles.json (rebuilt from the shotlist when Flow runs)
- `motion/` - motion_job.json, motion_report.json
- `packaging/` - packaging_plan.json, publish_kit.json, title.txt, titles.txt, description.txt, thumbnails/
- `_work/` - webchat_chats.json, webchat_debug/ (safe to delete); `_work/old/` holds leftovers

## Migrating an old production
"Tidy folder" on the production page (shown only when the folder has the old layout) and
"Tidy all production folders" on the In production page call `prodfiles.migrate()`:
moves the files above, rewrites the thumbnail paths stored in thumbs.json / publish_kit.json,
moves leftovers (out1/out2.., *.bak, shotlist_baseline.json, elements_v*.md, refs/Thumbs.db) to
`_work/old/` (nothing is deleted), and turns byte-identical duplicates (images vs flow_images,
audio/narration vs audio.mp3) into hard links, which saves the disk space. Refused while a job runs.
New copies of those two are also made as hard links (`prodfiles.link_or_copy`).

## Phase 2 (not done)
Move the text files into `script/` and the plan files into `plan/`; needs the many readers of
script.md / style.md / bible.md / elements.* switched to `prodfiles.path` first.

## Next tasks
- Qwen web chat: DONE 2026-10-24 (checked live on chat.qwen.ai). Model picker (`div[aria-label="Select Model"]`, 3 models) still matches the saved site. Thinking mode (Auto / Thinking / Fast) is a menu in `.qwen-thinking-selector`; the "Select Mode" button is the + tools menu, not thinking. The Qwen site definition (settings `webchat_sites`) got those three as levels; old value saved in `data/webchat_sites.before-qwen-levels.json`. Test: tests/test_webchat_qwen.py. Note: the site has `paid: false`, so the model choice is skipped; tick "paid" on Qwen if the model should be picked.
- Production export/import (no images), channel brief tidy-up, hand-over decision: see the
  recommendations given on 2026-10-24.
