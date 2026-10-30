# Script-first planning: plan and handover (2026-10-22)

For the next developer / LLM. Read "Working rules" first.

## 1. Goal (the point of all of this)

Make shotlist (image) planning simpler and more reliable by planning **from the story, not from subtitle cue numbers**.

Today the writer chat gets the narration as numbered subtitle cues and must cut the video into shots by cue ranges. Then the app's code checks and a judge chat send it back many times (8-9 rounds on one production; "judge not called" confusion; stuck DeepSeek/ChatGPT runs). Competitors (LTX Studio, Katalist, Golpo, judged from marketing pages only) go script -> scenes/shots, and LTX reuses fixed "Elements" (characters/places/objects) across shots.

The idea, in the owner's words: "is there any other way without the cues? maybe the story itself". **The core is the beat plan** (script -> beats -> image prompts -> app maps beats to cues). Elements are the smaller, consistency part. Do not let elements or side issues replace the beat plan again.

Constraints from the owner: no API, **web chat only** (browser-driven chats: DeepSeek, z.ai, ChatGPT); same database; old way must stay available; everything optional; must be revertable.

## 2. Pipeline (new way)

1. **Step 1, Elements (optional).** A chat reads the script and lists up to 12 recurring characters/places/objects, one fixed look each -> `elements.json` / `elements.md` in the production folder.
2. **Step 2, Beats.** A chat gets: narration (from the SUBTITLES, one line per cue, no numbers), `elements.md`, channel style, bible (or "none"), allowed motion codes, hold limit. It returns JSON:
   `{"style": "<compact style <=1400 chars>", "beats": [{"starts_with": "<first 8 words of the narration where the beat begins, exact>", "scene": 1, "type": "SCN|CU|INF|CMP|PROC|HYB|OVR", "motion": "<allowed code>", "prompt": "<image prompt 40-90 words>"}]}`
3. **The app maps beats to cues** (`beats.find_starts` / `cue_ranges`): each `starts_with` is found (forward only, word-normalised, 8 down to 3 words) in the subtitle word stream; a beat runs from its start cue to the next beat's start cue - 1; beat 1 always starts at cue 1. Every cue is covered exactly once by construction. Faults: quote not found; two beats in one cue.
4. **App checks** (`beats.check`): quote faults, motion codes, holds over `hold_limit(cap, tol)` (12s + 20% = 14.4s). Then it builds a normal `shotlist.json` (`build_shotlist`; asset names `S{scene:02d}_{n:02d}_{TYPE}_{MOTION}.png`) and runs the existing code checks (`external_prompts.shotlist_judge_prompt(cfg,pid,text,[])` second return value).
5. **Faults go back as small per-beat fixes**: `{"replace":[{"beat":N,"with":[beats...]}],"style":"..."}` (`fix_prompt`, `parse_fix`, `apply_fix`; numbers refer to the list sent). Up to 12 rounds; stops after the same faults 3 times.
6. **Pass** = no faults. Then `shotlist_before_beats.json` is written (copy of the old plan), `shotlist.json` saved, next stage unlocked. On failure `shotlist.json` is NOT touched (an earlier failed run overwrote a production's shotlist - never again). `beats.json` holds the last try. **No judge round yet** (decision pending, see 5).

The old way (`webstages.run_shotlist`, writer+judge) is unchanged and still selectable on the same page ("Or the old way").

## 3. State of the code

Repo `D:\Repos\WhisperRadar` (Flask app, `whisperradar/`), master. Test copy: git worktree `D:\Repos\WhisperRadar-beats`, branch `exp/script-first-plan`, dashboard on port **8541** via `start_beats.cmd` (own DB copy, master venv); master dashboard is 8540.

**On master (committed and merged):**
- `1f7017a`, `c90c1ee`: join cut-off chat replies, no "Missing: narration.txt", rule 4 "JSON in the chat text, no file", hold-fault wording naming 12s+tolerance, "no reviewer was called".
- `7f5279a`: style-too-long becomes one "style" fault; fault lists name up to 40 prompts / 80 holds; in-place fix helpers (`shotlist_fix_prompt`, `parse_shotlist_fix`, `apply_shotlist_fix`).
- `68b343c`: ChatGPT inline only, bible-question auto-answer, `_fix_in_place` (delta fix) in `run_shotlist`.
- `33324b2` + merge `a1c6aba`: the elements step (`elements.py`, `webstages.run_elements`, route `/studio/<id>/webchat/elements`, planner INPUT 4 block, Step 1 box on the shotlist stage page). Revert: `git revert -m 1 a1c6aba`.

**In the worktree only, UNCOMMITTED (the beats step):**
- `whisperradar/beats.py` (new): prompt, parse, mapping, checks, build, fix prompt/parse/apply, save.
- `whisperradar/webstages.py`: `_collect_beats`, `run_beats`, `beats_job`, `BEATS_CONTINUE`; import `beats as beatsmod`.
- `whisperradar/webapp.py`: stage `beats` allowed in `studio_webchat_run`, worker/label.
- `whisperradar/templates/studio_detail.html`: Step 2 box + "Or the old way" heading.
- `tests/test_beats.py` (18 tests; with elements/webstages tests 96 pass).
- Also untracked and NOT to be committed: `start_beats.cmd`, a stray folder `E:\YOUTUBE\Ink Explainer Replica\...` in the worktree root created by a test run (delete it).

**Next git step:** the owner will say "merge"; then commit beats on the branch (exclude `start_beats.cmd` and the stray folder) and merge into master with `--no-ff` so `git revert -m 1 <merge>` undoes it. Master currently also has other people's untracked files (docs, `config.skeleton.yaml`, `engines/flowbatch/profile.old-account/`, `E:\...` folders): leave them alone.

## 4. Test data and what is known

Production id 40: `E:\YOUTUBE\Ink Explainer Replica\Why Humans Never Eat Crocodile Meat` (644 cues, ~2,750 words). Files: `subtitles.srt`, `script.md`, `elements.md`, `shotlist_baseline.json` (master, old way, 122 shots, 9 rounds), `shotlist.json` (elements run, 120 shots, 8 rounds, delta fix).

Elements-only comparison (one run each, DeepSeek): subject mentions in prompts up clearly (crocodile 62 -> 91, meat 24 -> 44, chicken 12 -> 28); rounds 9 -> 8 (partly the delta fix, not elements); holds/faults equal; 4 of 12 elements almost unused. **Caveat found:** in this production `script.md` (19 Oct) is a newer, different text than `subtitles.srt`/`audio.mp3` (17 Oct), so elements came from the new script while the shotlist used the old narration. Beats therefore plan from the subtitle text. Consider a guard that warns when `script.md` is newer than the subtitles/audio.

**The beats step has NOT run in a real chat yet.** Unit tests and a mapping check with synthetic beats on this production's real subtitles pass. The real risks: the writer paraphrasing `starts_with`; replies too long for one message (continue logic exists, untested live); beat-number drift in fixes; style over the Flow limit (style + one prompt <= 2420 chars, `FLOWBATCH_MAX_PROMPT_CHARS`).

## 5. What is left (in this order)

1. **Run the beats step for real** on production 40 (and then 2-3 other productions), writer DeepSeek / z.ai / ChatGPT. Judge it against the goal: rounds, number of faults, did the writer never need cue numbers, are prompts usable, did it pass. Fix only what blocks that.
2. Merge beats to master when the owner says "merge".
3. Decide whether a judge round (quality, not mechanics) is added after the code checks; the old `shotlist_judge_prompt` expects cue-based plans, which the built `shotlist.json` is, so it can be reused.
4. **Image comparison:** generate images from the old plan and the new plan (Flow, `flowbatch`, 1376x768) and compare side by side. So far only prompt text was compared.
5. Elements tuning: cap list at ~8, tell the planner to drop elements that would appear in fewer than 3 beats.
6. Log the actual code-fault text per round in the studio log (today only the count is logged), so rounds can be diagnosed.
7. Small open items: treat motion `0`/no static shots properly (`ST` cannot be 0, falls back to 10%); warn on bad `land` (judge-only check today); detector fixes in `auto_values.py` ("1000 yrs", "9 out of 10 people"); font picker; bare-number noise in text overlays.
8. Later ideas only if the above works: per-shot variations (Katalist), one-shot edits.

## 6. Working rules (important)

- The owner's skill `change-only-when-told`: **no edits, commits or setting changes unless he says so**; questions get answers, not edits. Commit only on "commit"; use separate commits where possible; commit messages end with the Co-Authored-By / Claude-Session lines when Claude commits.
- **When something was agreed, do exactly that.** Do not swap in another task, and do not ask again. When reporting, say first whether the goal (beat plan) works, then side issues. Do not drift into fixing side problems without telling him; the previous session lost time on chat/fault-wording fixes while the beat plan stayed unbuilt.
- He double-checks results and gets uneasy with many issues: short, concrete replies.
- Web chat only, no API. Channel image engine is Flow in the browser (`flowbatch`).
- Practical: the Write/Edit tools of the cloud assistant write into the cloud container, not his PC - edit through the device shell. Git in his folders leaves `.lock` / `tmp_obj_*` files unless delete permission is granted. The full test suite takes more than 3 minutes; run the relevant files (`test_beats`, `test_elements`, `test_webstages`, `test_shotlist_faults`, `test_shotlist_pacing`, `test_motion_mix`). `webstages.py` has CRLF line endings (git warns, harmless). Each dashboard has to be restarted to pick up code changes; the 8541 copy has its own DB copy and its own web-chat sign-ins.
