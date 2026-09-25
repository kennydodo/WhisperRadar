# RETEST RUNBOOK — full pipeline, stage by stage

Prepared 2026-09-24 for a next-day run. Two productions are created and ready;
run them one stage at a time and stop wherever something looks wrong.

## Prepared productions

| pid | channel | engine | refs mode | source video | provider |
| --- | --- | --- | --- | --- | --- |
| 10 | Kenny Invest (Personal Finance) | flowbatch | SUPPLIED (27 seeded refs) | "Why You're Always Broke: 10 Silent Money Killers" (~3920 words, 1537s) | glm-flash (channel/global) |
| 11 | To Live and More (Lifestyle) | renderly/flow | ON-THE-FLY | "Japanese Women Don't Clean Hard…" (~3531 words, 1203s) | deepseek |

Both were built like `/studio/new`: production row + `seed_production` (bible.md,
style.md, and for pid 10 the 27 ref images) + `source_transcript.txt`.
`style.md` is seeded, so the **style stage will report "skip"** — that is expected.

pid 10 deliberately keeps **glm-flash** so the new stall guard fires live:
expect `glm-flash sent no content for 150s - retrying on 'deepseek'`.
pid 11 is pinned to **deepseek** for a fast, reliable script+judge run.

## Before starting

- Dashboard: `python wr.py serve` (http://127.0.0.1:8540).
- Renderly backend :8022 and Flow Driver :8030 — the images stage starts them.
- FlowBatch uses `profile-renderly` (koogunyemi); the Renderly driver uses
  `profile-b` (kogunyemi75). **Agent mode stays OFF.**
- Test suite (must be green before you start):
  - `python wr.py test` (or `test.bat`) -> 22 WhisperRadar unit tests.
  - `python wr.py test --renderly D:\Repos\Renderly` -> also runs the Renderly
    driver + backend suite (49 tests).

## Runner

One command per stage, same code path as auto-run:

```
python scripts/run_stage.py <pid> <stage> [--provider X] [--plan]
```

`--plan` prints what the stage would do without spending anything.
Exit 0 = ok, 1 = paused/failed (stop there).

## Steps (run 1 by 1)

```
# pid 10 — Kenny Invest, flowbatch, supplied refs, glm-flash
python scripts/run_stage.py 10 style                 # skip (seeded)
python scripts/run_stage.py 10 script                # expect glm stall -> deepseek fallback
python scripts/run_stage.py 10 audio                 # ai33 TTS
python scripts/run_stage.py 10 srt                   # local whisper
python scripts/run_stage.py 10 shots
python scripts/run_stage.py 10 refs
python scripts/run_stage.py 10 images
python scripts/run_stage.py 10 merge

# pid 11 — To Live and More, renderly, on-the-fly refs, deepseek
python scripts/run_stage.py 11 script
python scripts/run_stage.py 11 audio
python scripts/run_stage.py 11 srt
python scripts/run_stage.py 11 shots
python scripts/run_stage.py 11 refs
python scripts/run_stage.py 11 images
python scripts/run_stage.py 11 merge
```

## What to verify (the previously unverified parts)

**Script + judge (the real unknown)**
- Log line per attempt: `attempt N: rating <score>, overlap <pct>, ~<words> words`.
- Step detail now reads `target <N> words (wrote ~<W>)` — pid 10 target ≈ 3920,
  pid 11 ≈ 3531. A script must not be materially longer than its source.
- `data/studio/<pid>/versions/script/review.json` records per attempt:
  `score`, `criteria`, `feedback`, `words`, `too_long`, `judge_error`.
- Acceptance: `score >= script_min_rating` and `overlap <= script_max_overlap`.

**glm stall guard (pid 10)**
- Expect `glm-flash sent no content for 150s - retrying on 'deepseek'`, then the
  stage completing. It must NOT hang for the full 30 min.
- If it does not fall back, no other provider was `ready` — check Settings.

**max_tokens cap**
- Generation should stop near the target instead of rambling:
  pid 10 cap ≈ 6472 tokens, pid 11 ≈ 5849 (`1.6 x words + 200`).

**refs**
- pid 10: `all 3 supplied...` / "supplied, nothing to generate" (files already in
  `refs/`).
- pid 11: N on-the-fly refs generated; registry paths filled in `shotlist.json`,
  `refs_generated.json` written.

**images**
- pid 10 (flowbatch): refs attached, no "could not confirm"/"not in assets".
  Dead project → expect `the stored Flow project is not usable - asking prepare to
  create a new one`.
- pid 11 (renderly): ownership detection, **no** `ingredient echo` / timeouts.
- Every `shotlist.json` image must exist in `data/studio/<pid>/images/`.

**audio / srt**
- Audio is the ai33 TTS hook (`studio.studio_tts_command`) with the channel voice;
  if the API is down the audio stage pauses — that is a dependency, not a bug.
- srt is local whisper (`small`).

## If something breaks

- Flow refusal/throttle: check `D:\Repos\FlowBatch\debug\error-*.png`; do not
  grind (per the notes it lowers the reCAPTCHA score).
- Dead/foreign Flow project: the stage creates a new one automatically.
- LLM stall with no fallback: check Settings > LLM providers has another ready one.
- Capture the log tail + the step detail; both are persisted (`production_steps`,
  `productions.warning`).

## Where the findings go

- Renderly: `D:\Repos\Renderly\NEXT_SESSION.md`
- FlowBatch: `D:\Repos\FlowBatch\NEXT_SESSION.md`
- WhisperRadar: `AGENTS.md` (NEXT SESSION sections)

## Already green (from 2026-09-24)

- pid 7 (renderly) images 4/4; pid 8 (flowbatch, no refs) 6/6; pid 9
  (flowbatch, supplied refs) 6/6.
- Renderly handshake (`/api/prepare`) wired into `run_imagegen_flow`; project
  create-on-dead/404 verified.
- WhisperRadar: `tests/test_llm_limits.py` (9) covering the length caps, the
  max_tokens plumbing and the stall→fallback; Renderly: 49 tests green.
