# Aspect-ratio tests

Created 2026-09 for the `21:9 / 1:1 / 16:9` pipeline work across
**WhisperRadar** (`feat/aspect-ratio-pipeline`), **Renderly**
(`feat/wide-aspect-ratios`) and **ImgToVideo** (`feat/per-shot-aspect-ratio`).

> **STATUS: created, intentionally NOT run.** Nothing here has been executed.
> Run the steps below only when explicitly told to.

## What is covered

Core paths (motion code = last `_` token of `<name>_<MOTION>.png`):

| Test | Path | Ratios exercised |
| --- | --- | --- |
| 1 | WhisperRadar -> Renderly (engine `renderly`, mode `api`) | PL/PR -> paid API **21:9**; ST/ZI/ZO -> Flow Driver **16:9**; PU/PD -> Flow Driver **1:1**; PV -> Flow Driver **16:9** |
| 2 | WhisperRadar -> FlowBatch (engine `flowbatch`) | **1:1** (PU/PD), **16:9** (ST/ZI/ZO/PL/PR/PV) |
| 3 | Direct Renderly API (`renderly_direct_aspects.py`) | backend contract only: **9:16, 4:3, 3:4** |

On the renderly engine one batch is split across the engine's two halves -
PL/PR to the paid API, everything else to the free Flow Driver. If the API
hits a quota/billing wall (ImageGen exit 3) the remaining PL/PR fall through
to the Flow Driver as 16:9.

`aspect_matrix.py` is the single source of truth for the expected values:
`RENDERLY_API_ASPECT`, `FLOWBATCH_ASPECT_BY_MOTION`, `FLOW_DRIVER_MOTION_ASPECT`
and `FLOW_SUPPORTED_ASPECTS`.

## Step 0 - fast, free, no code was run to make these

These are the automated tests; run them first - they spend nothing:

```
cd D:\Repos\WhisperRadar
python -m unittest discover -s tests -v          # includes test_aspect_ratio_pipeline.py

cd D:\Repos\Renderly
node --test "tests/js/aspect-ratio.test.js"
backend\.venv\Scripts\python.exe -m unittest discover -s tests\py tests\py\test_aspects.py
rem (or: test.bat, which runs the whole Renderly suite)
```

## Step 1 - seed the two live test productions (no rendering)

```
cd D:\Repos\WhisperRadar
python scripts\aspect_tests\seed_test_projects.py --print-only   # preview
python scripts\aspect_tests\seed_test_projects.py                # create
```

It prints two pids: `ASPECT TEST Renderly` and `ASPECT TEST FlowBatch`, each
with a compact 10-image shotlist - one each of PL/PR/PV plus ordinary shots.
No image is generated.

## Step 2 - test 1: WhisperRadar -> Renderly (paid)

Make sure the Renderly backend is up (`:8022`), then:

```
python scripts\run_stage.py <renderly_pid> images
python scripts\aspect_tests\verify_rendered_aspects.py <renderly_pid>
```

Expect: **PL/PR -> 21:9** (the paid API, the only API traffic); ST/ZI/ZO ->
16:9, PU/PD -> 1:1 and PV -> 16:9, all through the Flow Driver. If the PL/PR
API call hits a quota wall, those fall through to the Flow Driver as 16:9 and
the step detail reads "quota-limited".

## Step 3 - test 2: WhisperRadar -> FlowBatch

```
python scripts\run_stage.py <flowbatch_pid> images
python scripts\aspect_tests\verify_rendered_aspects.py <flowbatch_pid>
```

Expect: 1:1 for PU/PD, 16:9 for everything else (FlowBatch has no 21:9, so
PL/PR/PV all render 16:9). Requires a signed-in Google session in FlowBatch's
`profile\`.

## Step 4 - test 3: backend contract for the unreachable ratios (paid)

The shotlist pipeline never selects 9:16, 4:3 or 3:4 (no motion code maps to
them), so this is a MANUAL Renderly-backend contract probe, not pipeline
behaviour:

```
python scripts\aspect_tests\renderly_direct_aspects.py
```

Calls Renderly's API once per ratio (9:16, 4:3, 3:4), downloads each result
and checks its pixel shape. `--all` also runs 16:9/21:9/1:1.

## Notes and known risk

- `extension-v2/flow.js setProjectAspectRatio()` was **not** verified against a
  live Flow session (its own comment says so). Test 1's Flow-Driver shots and
  test 2's PU/PD result are the first real checks of that selector path.
- The API/Flow split and the quota fallback both key off the "quota"/"billing"
  wording in Renderly's error text; a wording change in `gemini_client.py`
  would defeat the fallback. Test 1's log and step detail are where to confirm.
- `verify_rendered_aspects.py` tolerates a 4% deviation per ratio: 21:9 and
  PV's 2.96:1 differ by ~2% by design, and providers round canvas sizes.
