# FlowBatch

Batch image generation for **Google Flow** (`flow.google.com`) driven by reference images, using
Playwright to automate the real Flow web UI in a persistent Chrome profile.

Give it a list of prompts and a set of reference images, and it will attach the references, submit
the prompt in Flow's **Image** mode, wait for the results, and save the generated stills to disk.

```
refs/character-a.png ─┐
refs/street-style.png ─┴─► Google Flow (Image mode) ─► output/<job>/<item-id>.jpg
```

## Status

Verified end to end against the live Flow UI on **2026-09-21/22**: Agent mode on, project settings
(model / aspect ratio / output count), reference upload through the asset picker, generation, and
full-resolution download (1376×768 at 16:9) all work.

Two things to know before you run it at volume:

- **Flow throttles automation.** After roughly eight generations in ten minutes it starts refusing
  with *"We noticed some unusual activity."* The runner detects this, does not retry, and stops the
  batch. Space runs out and keep batches small.
- **Every reference upload becomes a project asset.** Uploading the same file in repeated runs
  creates duplicates in your Flow project. Clean them up in the Flow UI periodically.

## How it works

- A dedicated Chromium profile lives in `profile/`. You sign in to Google **once, by hand**; the
  session is reused on every later run.
- **Generation is gated by reCAPTCHA Enterprise**, and the score belongs to the signed-in
  **account and browser session**, not to a single request. A session Flow distrusts is refused with
  *"We noticed some unusual activity"* in about three seconds — no generation is attempted, so
  waiting cannot help, and each refusal lowers the standing further.
- **Agent mode OFF is what this project uses** (it keeps the per-item model, aspect-ratio and
  output-count controls available). It works on a healthy account. On a session that Flow already
  distrusts, Agent OFF is what tips a generation over into a refusal — in that situation Agent ON
  (`--agent on`, or `"agent": true`) is the way to keep generating. Agent ON hides the prompt-box
  settings, so the defaults then come from the project settings panel (gear icon → *Agent settings*).
- References are attached through the prompt box's Add menu → *Upload media* → the project asset
  picker → *Add to prompt*. This is also why each reference becomes a project asset.
- Results are fetched from the tile's signed CDN URL (`https://flow-content.google/...` or
  `/asb/...`), which serves the full-resolution still without any UI interaction. Flow's own export
  menu is the fallback.
- Flow serves stills as **JPEG**, so file extensions are sniffed from the content, not assumed.
- `config/selectors.json` maps semantic names (`promptBox`, `generateButton`, `assetTile`, …) to
  ordered lists of Playwright selectors, so a Flow UI change is a config edit rather than a code
  change.
- **The Flow page is loaded once per batch, not once per item.** Between items the composer is
  cleared in place — the text is selected and deleted, and each attached reference is detached via
  its own remove control. Reloading the whole app before every item is far more activity than a
  human produces and was the largest behavioural difference from a comparable driver that ran the
  same job without being rate limited. A reload is still used as a fallback when the composer will
  not come clean, and `generation.resetBetweenItems: "reload"` restores the old behaviour.

### Clearing the composer

Two details that are easy to get wrong:

- The ingredient chips live in `flow-ingredient-bar`, **not inside the ProseMirror editable**, so
  select-all in the editor does not remove them. Each chip has its own remove control
  (`flow-image-ingredient-chip div.hover-icon-overlay`) that must be clicked, and it is transparent
  until hovered, so the click is forced.
- The prompt-box settings (model, aspect ratio, output count) are **project defaults and survive an
  in-place clear**, so they are applied once per run rather than per item. Re-applying means opening
  the settings overlay for nothing, and clicking its trigger while it is already open just closes it
  again.

## Requirements

- Node.js 20 or newer
- Google Chrome installed (Google is far more likely to allow sign-in from real Chrome than from
  Playwright's bundled Chromium)
- A Google account with access to Google Flow

## Install

```powershell
cd D:\Repos\FlowBatch
npm install
```

Only one dependency is used: `playwright`.

## Quick start

**Double-click `FlowBatch.bat`** to start the tool. It checks Node is present and new enough,
installs dependencies on first run, starts the UI and opens your browser. If the UI is already
running it just opens it. Leave the window open while you work; close it to stop.

(It is not called `start.bat` on purpose — `start` is a cmd builtin, which makes that name ambiguous
from a terminal.)

Or drive it from a terminal:

```powershell
# 1. Sign in once. A browser window opens; finish the Google sign-in flow and it closes itself.
npm run login

# 2. Check config, and resolve every selector against the live page.
npm run doctor -- --live --project-url "<your project url>"

# 3. See the plan without spending anything.
npm run generate -- --job config/jobs.smoke.json --dry-run

# 4. Generate. Start with one item.
npm run generate -- --job config/jobs.smoke.json
```

`refs/` ships with generated 512×512 placeholder images so the examples run immediately. Replace
them with real reference images.

### Pointing a job at a project

Flow's landing page redirects into your most recent project after a few seconds, which is slow and
not always deterministic. Set `projectUrl` on a job to open a project directly:

```json
{ "projectUrl": "https://flow.google.com/project/<project-id>" }
```

You can also set `projectUrl` in `config/settings.json` to make it the default for `doctor --live`.

## Job files

A job is a JSON file. The recommended shape maps **asset names** to local files and lists the images
to generate:

```json
{
  "name": "scenes-example",
  "projectUrl": "https://flow.google.com/project/<project-id>",
  "outputsDir": "output/scenes-example",

  "refs": {
    "Maya": "E:/YOUTUBE/PERSONAL FINANCE/Refs/Maya.png",
    "Dana": "E:/YOUTUBE/PERSONAL FINANCE/Refs/Dana.png",
    "BG_LIVING_ROOM_01": "E:/YOUTUBE/PERSONAL FINANCE/Refs/BG_LIVING_ROOM_01.png"
  },

  "style": "Polished modern 2D editorial explainer illustration ... applied to every prompt",

  "defaults": {
    "mode": "image",
    "agent": false,
    "model": "Nano Banana 2 Lite",
    "aspectRatio": "16:9",
    "outputs": 1,
    "refMode": "reuse"
  },

  "images": [
    {
      "file": "S01_01_HYB_PR.png",
      "prompt": "Maya stands at the open glass door of her grandmother's display cabinet...",
      "refs": ["Maya", "BG_LIVING_ROOM_01"]
    },
    {
      "file": "S01_02_CU_ZI.png",
      "prompt": "Close-up of Maya's hands holding an ornate floral porcelain plate...",
      "refs": ["Maya"]
    }
  ]
}
```

**`refs` is a name → path map.** The name is what the asset is called inside the Flow project; the
path is only used to upload the file when that name is not found there. An item's `refs` are those
names. A name absent from the map is still usable — it is attached from the project as-is, which is
why `BG_LIVING_ROOM_01` would work even if it were omitted from the map.

Local paths are **optional**. If a mapped file is missing, the job still loads; the tool simply
cannot upload it, and says so if the name is also absent from the project.

### `style`

A top-level `style` string is applied to **every** prompt — the usual pattern for editorial shot
lists where one art direction covers the whole batch. It is prepended by default; set
`stylePosition: "suffix"` to append instead.

### Unrecognised keys are reported, not ignored

Any top-level or `defaults` key the loader does not understand produces a warning, and so does any
extra array (an editorial `shots` list, for example). This is deliberate: a field that looks like it
was applied but silently was not is worse than a noisy warning. If a job defines both `images` and
`items`, the one that was used is named in the warning.

### Encoding

- A **UTF-8 BOM** at the start of the file is stripped automatically. Without that, `JSON.parse`
  fails and the job looks corrupt when it is fine.
- **Mojibake** — text saved as UTF-8 but read back as Windows-1252, so an em dash `—` becomes `â€”` —
  is detected and reported with a count. Pass `--repair-encoding` to send corrected text at run time,
  or fix the file itself with `npm run repair -- <file>`.

### Repairing a job file

```powershell
npm run repair -- "<path to job.json>" --dry-run   # preview every change
npm run repair -- "<path to job.json>"             # apply, writing a .bak backup first
```

`repair` strips a UTF-8 BOM and reverses mojibake in every string, preserving the file's existing
indentation. It is idempotent — running it twice reports "Nothing to repair".

### Matrix (many prompts × many reference sets)

`matrix.prompts` is crossed with every entry of `matrix.refSets`, producing
`prompts.length × refSets.length` items with ids like `p2-alice`.

```json
{
  "name": "example-matrix",
  "outputsDir": "output/example-matrix",
  "defaults": { "aspectRatio": "16:9", "outputs": 2 },
  "matrix": {
    "prompts": ["Full-body studio shot of the character.", "Close-up portrait."],
    "refSets": {
      "alice": ["refs/alice-front.png", "refs/alice-side.png"],
      "bob": ["refs/bob-front.png"]
    }
  }
}
```

### Item fields

| Field | Default | Notes |
| --- | --- | --- |
| `file` | — | Output filename. The extension is replaced with the real format after download. |
| `id` | `file` stem, else `item-N` | State key for resume. Must be unique. |
| `prompt` | — | Required. |
| `refs` | `defaults.refs` | Asset names, resolved through the top-level `refs` map. `[]` means no references. |
| `mode` | `image` | `image` or `video`. |
| `agent` | `false` | OFF keeps per-item model/ratio/output control. ON is the fallback if a session is being refused. |
| `model` | `defaults.model` | As shown in Flow, e.g. `Nano Banana 2 Lite`, `Nano Banana 2`, `Nano Banana Pro`. |
| `aspectRatio` | `defaults.aspectRatio` | Image mode offers `16:9`, `4:3`, `1:1`, `3:4`, `9:16`. |
| `outputs` | `defaults.outputs` | Positive integer (`x1`–`x4`). |
| `refMode` | `reuse` | `reuse`, `upload`, `assets` or `mention` (see below). |
| `retries` | `defaults.retries` | Extra attempts after the first failure. |
| `timeoutMs` | `settings.timeouts.generationMs` | Per-item generation timeout. |

### Reference handling modes

The mode only decides how a named reference reaches Flow.

| Mode | Behaviour | Verified |
| --- | --- | --- |
| `reuse` (default) | Attach the project asset **by name**; upload the local file only when the name is missing. Self-healing, and creates no duplicates once seeded. | yes |
| `assets` | Attach by name only; never upload. | yes |
| `upload` | Always upload the local file. Duplicates project assets. | yes |
| `mention` | Types `@<name>` into the prompt. Unverified against the current UI. | no |

`reuse` is right for this workflow: the first run seeds the project from the local files, and every
later run attaches by name with **zero new assets** (measured: 31 tiles before and after attaching
`Maya` + `BG_LIVING_ROOM_01`).

### Multiple references per prompt

Flow supports several ingredients at once — four were attached simultaneously during testing. Each
reference is attached in its own picker session, because uploading several files in one call only
ever attaches the first one. Expect roughly 5–10 seconds per reference, plus upload time for new
files (the `BG_*.png` set is 14–18 MB each).

When searching, an exact title match wins over an exact filename match over the first fuzzy hit, so
`Maya` never attaches `Maya_alt.png` by accident.

### What actually happens when references are attached

The prompt box's **`+` button opens Flow's asset library inline** — search, category navigation and
the project's asset list. It does **not** create a file input, so nothing resembling an upload dialog
is involved:

```
+ sign only   ->  library open, asset list visible, fileInputs: 0
click an asset ->  attached to prompt, library closes, fileInputs: 0
```

**"Upload media" is a separate button *inside* that library.** Its only job is to spawn the hidden
`input[type=file]`, so it is clicked only when a file genuinely has to be uploaded. That means:

- `reuse` and `assets` modes **never touch "Upload media"** and never open a file dialog.
- Only `upload` mode — or a `reuse` run whose name is missing from the project — does.

Attaching an existing asset and uploading a new one behave differently; both were measured:

| Action | Result |
| --- | --- |
| Click an asset in the library | Attaches immediately and closes the library. No *Add to prompt*. |
| Upload via "Upload media" | Leaves the asset selected; needs an explicit *Add to prompt*. |

A synthetic drag-and-drop onto the prompt box was also tried and is rejected by Flow, so the library
is the only route — but for references already in the project it is a single click.

## Upscaling

Flow returns roughly 720p (its 16:9 master measures 1376×768). Every result can be upscaled on the
way out to a **resolution tier**, named for what it delivers rather than a multiplier — from a 720p
master a "3x" is really 4K, which was misleading.

| Tier | 16:9 target | Other ratios (long side) |
| --- | --- | --- |
| `off` | 1376×768, byte-for-byte copy | — |
| `1k` | 1920 × 1080 (Full HD) | 1920 long side |
| `2k` (default) | 2560 × 1440 | 2560 long side |
| `4k` | 3840 × 2160 | 3840 long side |

**Default is 2K**, and the choice is remembered — the web UI writes it as you change it, and
`upscale --set-tier 4k` does the same from the terminal. It is stored in
`config/upscale.local.json` (gitignored) so a local choice never dirties the repo.

### `fit`: exact vs aspect

Flow's masters are only *close* to standard ratios — its "9:16" is 768×1376 (0.5581, not 0.5625).

- **`fit: "exact"`** (default) snaps to the standard ratio, so a 9:16 shot at 1K becomes exactly
  **1080×1920** and drops into a timeline with no further scaling.
- **`fit: "aspect"`** keeps the master's own ratio and matches the tier's long side, giving
  1072×1920 for the same shot. Nothing is resampled non-uniformly, but the sizes are non-standard.

Every tier is exactly 16:9, so a 16:9 source is never stretched, and the sizes match the numbers
WhisperRadar shows for its render resolutions (1920×1080 / 2560×1440 / 3840×2160) — a tier means the
same thing on both sides of that integration.

### Supersampling

The engine runs one native scale **above** what the target needs and the result is Lanczos-downscaled
(for example 4K runs at the model's 4x, then resamples to 3840×2160). The GAN synthesises at the
larger size and the downscale removes its artifacts, which is cleaner than resampling up from the
smaller native scale. This is the approach Renderly used. It costs time and VRAM; set
`supersample: false` for the smallest scale that fits.

### GPU first, CPU fallback

1. **GPU** — Real-ESRGAN via ncnn + Vulkan (`tools/realesrgan/realesrgan-ncnn-vulkan.exe`).
   Device indices 0–5 are probed once with a generated test image; NVIDIA is preferred, and the
   working index is cached in `tools/realesrgan/device_cache.json`.
2. **CPU** — Lanczos-3 resampling, implemented in `src/upscale/png.js`. No Pillow, no image
   library, no build step.

Every GPU result is **content-checked** against its input before being accepted. A driver fault or
VRAM overrun makes the engine emit output that looks nothing like the source rather than an error,
so a mismatch discards that device, clears the cache, and moves on. This is why the fallback exists
even on a machine with a working GPU.

### Where files land

The batch pipeline writes both, so the 720p master survives for a later re-upscale:

```
output/<job>/S02_02_MET_ZO.png       720p master from Flow
output/<job>/S02_02_MET_ZO_2k.png    upscaled
```

With `tier: "off"` only the master is written. An upscale failure warns and keeps the master — it
never fails the item.

### Standalone use

```powershell
npm run upscale                                        # engine, device, tiers and current setting
npm run upscale -- <file-or-folder> --tier 4k          # upscale, writing <name>_4k.png
npm run upscale -- <folder> --tier 2k --out D:\big     # write elsewhere
npm run upscale -- <file> --tier 2k --fit aspect       # keep the master's exact ratio
npm run upscale -- --set-tier 4k                       # remember 4K as the default
```

Input must be **PNG** — which is what the pipeline produces. The engine and its models are vendored
in `tools/realesrgan/` (~10 MB); `README_windows.md` there carries the upstream attribution and
licence. `realesr-animevideov3` is the default model because it ships native x2/x3/x4 and suits
illustration; `realesrgan-x4plus` is x4-only and is downscaled for 2x and 3x.

## Web UI

```powershell
npm run ui                 # then open http://127.0.0.1:8787
npm run ui -- --open       # opens the browser once the server is listening
```

`FlowBatch.bat` in the project root does the same thing with the browser step included.

The page drives the same CLI as a child process — it is a front end, not a second implementation.

| Control | What it does |
| --- | --- |
| **Job JSON** | Dropdown of every job in `config/`, plus **Choose…** for any JSON file elsewhere. |
| **Save images to** | Native folder picker. Empty uses the job's own `outputsDir`. Passed as `--output`. |
| **Upscale tier** | Off / 1K / 2K / 4K, default 2K. Saved immediately and remembered between runs. |
| **Dry run** | Prints the plan and spends nothing. Worth ticking first. |
| **Start Batch** | Spawns `generate`. Refused with a clear message if a batch is already running. |
| **End Process** | Kills the whole process tree, so Playwright's Chrome does not survive the stop. |
| **Console** | Live stdout/stderr streamed over SSE, with the command line echoed at the top. |

Notes:

- The server binds to `127.0.0.1` only. Override with `--port` / `--host`.
- Folder and file pickers are shown **by PowerShell on the desktop**, because a browser cannot hand a
  real filesystem path to the server. A dialog appearing outside the browser window is expected.
- The console keeps the last 3000 lines and replays them to a page that connects mid-run, so
  refreshing does not lose the output.
- Stopping a batch can leave the browser profile flagged; Chrome is terminated with the tree.

## CLI reference

| Command | Purpose |
| --- | --- |
| `login` | Open the persistent profile and wait for a manual Google sign-in. |
| `discover` | Dump the Flow DOM to `discover/` to calibrate selectors. |
| `doctor` | Check environment and config; with `--live`, resolve every selector. |
| `generate` | Run a batch job. |
| `serve` | Start the local web UI. |
| `repair` | Fix a job JSON in place: strip a UTF-8 BOM and repair mojibake. |
| `upscale` | Upscale PNGs 1x/2x/3x/4x on the GPU, with a CPU fallback. |

`generate` options:

| Flag | Meaning |
| --- | --- |
| `--job <file>` | Job file to run (required). |
| `--only <id,id>` | Run only these item ids. |
| `--limit <n>` | Run at most `n` items. |
| `--no-resume` | Re-run items already marked `done`. |
| `--reset-state` | Clear stored state first. |
| `--dry-run` | Print the plan without launching a browser. |
| `--fail-fast` | Stop at the first failed item. |
| `--pause-on-error` | Keep the browser open and wait for Enter after a failure. |
| `--no-dump-on-error` | Skip the screenshot + HTML capture in `debug/`. |
| `--keep-open` | Leave the browser open when the run finishes. |

`discover` options: `--wait <s>`, `--navigate <url>`, `--click <selector>` (repeatable),
`--dump <selector>` (repeatable), `--agent <on|off>`, `--upload <file>` (repeatable), `--html`.

Global: `--log-level <debug|info|warn|error>`, `--no-color`, `--channel <chrome|msedge>`,
`--headless`, `--url <url>`.

## Outputs, state and resume

- Generated stills are saved under **exactly the name in the item's `file`**, for example
  `output/<job>/S02_02_MET_ZO.png`.
- Flow only exports **JPEG**. When `file` asks for `.png`, the still is re-encoded to a real PNG
  using the browser's own canvas — no image library is required — so the name and the contents agree.
  If that conversion fails the file is written with its true extension and a warning is logged.
- Multiple results for one item become `<stem>-2.png`, `<stem>-3.png`.
- If an item has no `file`, the output is `<id>.png`.
- Progress: `state/<job>.json`, written after every item. Re-running a job skips completed items;
  use `--no-resume` or `--reset-state` to force a re-run.
- Failure captures: `debug/error-<item-id>-<timestamp>.png` and `.html`.

## Rate limits and throttling

Google actively detects this kind of automation. The observed refusal is:

```
Failed
We noticed some unusual activity. Please visit the Help Center for more information.
You have not been charged for this generation.
```

A rate limit is temporary, so the runner **waits and retries the same item** rather than abandoning
the rest of the batch:

```
Flow is rate limiting. Waiting 180s, then retrying "S02_01_HOST_ZI" (cooldown 1/10).
```

It waits `generation.cooldownSeconds` (default **180**), retries the item from scratch, and resets
the counter whenever an item succeeds. After `generation.maxCooldowns` consecutive waits (default
**10**) it gives up and stops the batch, leaving the rest `pending`.

```powershell
npm run generate -- --job "<job>" --cooldown 300 --max-cooldowns 20
```

An over-long prompt is refused with the same wording but waiting never fixes it, so that item is
**skipped** and the batch continues instead.

Other mitigations, in order of effect:

1. Raise `generation.delayBetweenItemsMs` (default 20000) so generations are spaced out and the
   limit is hit less often.
2. Raise `--cooldown` if a single wait is not enough to clear it.
3. Keep batches small and avoid repeated back-to-back runs.
4. Use `--limit` to run a few items at a time.

Observed recovery: about **4 hours** after the first episode, and about **2h40m** after a later one.
It appears to scale with how hard the account was pushed, so tripping it less often means shorter
waits.

> Before assuming throttling, rule out an over-long prompt — see the next section. Flow returns the
> identical message for both, and the fix is completely different.

## Prompt length limit

Flow refuses **over-long prompts** with the *exact same* "unusual activity" message it uses for rate
limiting. The two are easy to confuse, and the difference matters: one needs a wait, the other needs
an edit.

Measured boundary, with identical references and in the same sessions:

| Combined prompt length | Result |
| --- | --- |
| 2367 characters | accepted |
| 2427 characters | accepted |
| **2510 characters** | **refused — three separate attempts** |

The ceiling is therefore roughly **2450 characters**. It is server-side: the prompt box itself
accepts 5000+ characters without truncating, and there is no `maxlength` or counter in the DOM.

The limit applies to the **whole prompt**, which is the job-wide `style` plus the item's `prompt`, so
a long `style` consumes most of the budget for every item:

```
style    1858 chars   <- shared by every prompt
scene     414-677 chars
join          1 char
total    2273-2536 chars
```

That works out at roughly **78% style, 22% scene** — so trimming the `style` is far cheaper than
editing every prompt. Removing about 20 words from a 258-word style brought the longest prompt from
2536 down to ~2400 and fixed 26 items in one edit.

### The tool warns before spending anything

Every prompt is checked at load time against `generation.maxPromptChars` (default **2420**) and the
offending items are named:

```
WARN  26 of 85 prompts exceed 2420 characters (longest 2536) and will be refused by Flow as
      "unusual activity". The job-wide "style" contributes 1858 characters to every prompt.
      Shorten: S01_01_HYB_PR, S01_03_SCN_PL, S04_02_HYB_PR, +23 more
```

Set it in `config/settings.json`, or per job in `defaults`:

```json
{ "defaults": { "maxPromptChars": 2420 } }
```

This is a warning threshold, not an enforced truncation — nothing is ever silently cut from a
prompt. Lower it to leave headroom.

An over-long prompt is refused with the same wording as a rate limit, but it is distinguished by
length: if the item's prompt exceeds `maxPromptChars` the item is **skipped** and the batch
continues, rather than waiting for something that will never succeed.

## Calibrating selectors

The current selectors are verified against Flow as of 2026-09-21. When Flow's UI changes:

1. Run `npm run discover -- --agent off` and read `discover/flow-<timestamp>.md` — a table of every
   visible interactive element with suggested selectors.
2. To see inside a popover, click into it first:
   `npm run discover -- --agent off --click 'button.settings-trigger-button' --dump 'div.settings-content-overlay'`
3. Put the working selector at the **front** of the matching array in `config/selectors.json`.
4. Run `npm run doctor -- --live --project-url "<project url>"` to confirm what matched.
5. For machine-local tweaks use `config/selectors.local.json` (gitignored); its entries replace the
   base entries for the same key.

Keys verified to resolve on a freshly loaded project: `promptBox`, `agentToggle`,
`settingsTriggerButton`, `generateButton`, `assetTile`, `assetMenuButton`, `addIngredientsButton`,
`projectTitle`, `projectCard`, `signedIn`.

Keys that only exist inside an open popover or after a reference is attached — `doctor --live`
reports these as `[SKIP]` rather than failing: `settingsOverlay`, `modeImageOption`,
`aspectRatioGroup`, `outputCountGroup`, `modelFamilyButton`, `modelMenu`, `addMediaOption`,
`addToPromptButton`, `promptReferenceChip`, `generatingIndicator`, `errorBanner`.

## Getting good results with reference images

From Google's own Flow guidance:

- Use clean references: a subject or product on a plain or segmented background.
- Keep location and style references free of unintended extra subjects.
- Keep references consistent with each other (same wardrobe, lighting, viewpoint) for a character.
- Make the prompt complement the references rather than contradict them.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Google refuses sign-in | Use real Chrome (`browser.channel: "chrome"`) and avoid `--headless` for login. |
| `Flow refused the generation: ... unusual activity` | Two different causes, same message. Check the load-time prompt-length warning first; if the prompt is short, you are throttled — wait, then raise `delayBetweenItemsMs`. |
| `N of 85 prompts exceed ... characters` at load | The job-wide `style` is usually most of it. Trim the style, not every prompt. |
| `Could not locate the Flow UI element "x"` | Re-run `npm run discover` and update that key. |
| `Generated the asset but could not save it` | Calibrate `assetTile`; check `debug/` for the grid state. |
| `Flow refused ... unusual activity` within ~3s | Not a rate limit — a reCAPTCHA standing problem on that account/session. Try `--agent on`, or sign in with a different Google account. Waiting and retrying make it worse. |
| Model / ratio / outputs not applied | With Agent ON these come from the project settings panel (gear icon), not the prompt box. |
| Project fills with duplicate uploads | Expected with `refMode: "upload"`. Delete the extras in Flow. |

## Limitations

- It drives the web UI, so it is only as stable as Flow's markup. Calibration is ongoing
  maintenance.
- Generation is sequential, and Google throttles rapid automated runs.
- **Combined prompts (style + scene) must stay under roughly 2450 characters.** Over-long prompts are
  refused with the same message as throttling; they are detected by length and skipped.
- Rate limits are waited out automatically, so a long batch can take hours. It is bounded by
  `maxCooldowns`; if that is exhausted the run stops and the rest stay `pending`.
- The observed tolerance is low — as few as **3 generations** between refusals after heavy use.
- Automating a Google product may conflict with its terms of service. Use your own account, keep
  volumes reasonable, and review Flow's terms before running large batches.
