# AGENTS.md

Project notes for automated agents working in this repository.

> **Outstanding work is tracked in [`NEXT_SESSION.md`](./NEXT_SESSION.md)** — read it before
> starting, and delete it once the list is clear.

## What this is

A Playwright automation project that drives the Google Flow web UI to batch-generate images from
reference images. Plain Node ESM, no build step, no bundler. The only runtime dependency is
`playwright`; tests use the built-in `node:test` runner, so there is no test dependency either.

## Commands

```powershell
npm run login                                  # manual Google sign-in into the persistent profile
npm run discover                               # dump Flow DOM to discover/ for selector calibration
npm run doctor                                 # static config/env checks
npm run doctor -- --live --project-url "<url>" # resolve every selector inside a real project
npm run generate -- --job <file> --dry-run     # validate a job without a browser
npm run generate -- --job <file>               # run it
```

`doctor --live` must run **inside a project**; prompt-box controls do not exist on the landing page.

**Be careful with real runs.** They consume the user's Flow quota and Google throttles automation.
Use `--limit 1` and do not run repeated batches while developing. If you see
`Flow refused the generation: ... unusual activity`, stop and tell the user rather than retrying.

Verification after a change (no linter; the unit suite covers every non-browser module
(`src/lib`, `jobs/`, `src/runner`, `src/flow`, `src/upscale`, `src/server.js`) plus the example-job
dry runs and `config/selectors.json`, but not the live Playwright/browser paths — see `test/`):

```powershell
npm test
node -e "for (const f of ['src/lib/args.js','src/lib/config.js','src/lib/context.js','src/lib/errors.js','src/lib/json.js','src/lib/log.js','src/lib/paths.js','src/lib/prompt.js','src/lib/time.js','src/browser/session.js','src/flow/selectors.js','src/flow/driver.js','src/jobs/load.js','src/runner/state.js','src/runner/run.js','commands/login.js','commands/discover.js','commands/doctor.js','commands/generate.js']) await import('./' + f); console.log('ok')" --input-type=module
node src/cli.js doctor
node src/cli.js generate --job config/jobs.example.json --dry-run
node src/cli.js generate --job config/jobs.matrix.example.json --dry-run
```

## Architecture

```
src/cli.js              argv parsing, command dispatch, help text
commands/               one file per CLI command; each exports <name>Command({flags, context, positionals})
src/lib/                logging, config loading, paths, args, JSON, errors, timing, image sniffing
src/browser/session.js  launchPersistentContext + stealth init; returns {context, page}
src/flow/selectors.js   SelectorSet: ordered-candidate resolution with polling
src/flow/driver.js      FlowDriver: every Google Flow interaction lives here
src/jobs/load.js        job file validation and matrix expansion -> flat item list
src/runner/state.js     per-item progress persistence for --resume
src/runner/run.js       batch orchestration, retries, downloads, summary
src/server.js           local HTTP + SSE server that spawns the CLI for the web UI
commands/serve.js       `serve` command; keeps the process alive
ui/index.html           the whole UI: one self-contained file, no build step
config/                 settings.json, selectors.json, example job files
```

The UI is deliberately a thin shell over the CLI: `src/server.js` spawns
`node src/cli.js generate …` and streams its stdout/stderr. It must not grow a second copy of the
generation logic. Native folder/file pickers run through PowerShell because a browser cannot expose a
real filesystem path to the server.

## Verified Google Flow facts (2026-09-21)

These were established by probing the live signed-in UI. Do not "fix" them from memory of the docs:

- The app is Angular Material with **no `data-testid` attributes**. Controls are labelled with
  `aria-label`, and custom elements carry stable tag names (`flow-rich-text-editor`,
  `flow-grid-tile-container`, `flow-toggles`, `flow-image-ingredient-chip`).
- **Generation is gated by reCAPTCHA Enterprise** (requests to
  `www.google.com/recaptcha/enterprise/reload` and `/clr` fire when Generate is clicked). The score
  belongs to the **signed-in account and browser session**, so a refusal is *"We noticed some unusual
  activity"* in ~3 seconds with no generation attempted. It is not a rate limit: waiting does not
  help, and each refusal lowers the standing further. Measured 2026-09-22: `koogunyemi@gmail.com`
  generated normally while `japanliveshealthy@gmail.com` was refused on every attempt, with the same
  code, profile copy and settings — so when this appears, suspect the account before the code.
- **Agent mode OFF is the default** because it keeps the per-item model / aspect-ratio / output-count
  controls. It works on an account with good standing. On a session Flow already distrusts, Agent OFF
  is the difference between generating and being refused — `--agent on` was verified to turn three
  consecutive refusals into four consecutive successes on such a session. Agent ON hides the
  prompt-box settings trigger, so defaults then come from the project settings panel.
- Because Agent ON hides the prompt-box settings trigger, the **model / aspect ratio / output count
  are set in the project settings panel** — the gear icon (`button[aria-label='Settings']`), a
  right-hand sidebar titled *Agent settings* with `flow-toggles[aria-label='Image generation default
  aspect ratio' | '... output count']`, `button[aria-label='Image generation default model']` and a
  Save button that only renders when something changed. The sidebar covers the composer, so it must
  be closed (its Back button) before anything touches the prompt box.
- Agent mode is ON by default, persists per project, and hides the prompt-box settings trigger.
  `button.agent-mode-chip` exposes state via `aria-pressed`. Toggling must be idempotent — a blind
  click flips it back.
- With Agent off, `button.settings-trigger-button` opens a `flow-prompt-box-settings` overlay holding
  **all** of mode (`flow-toggles[aria-label='Mode']`), aspect ratio, output count and the model menu.
  Toggle selection is `aria-checked`, not `aria-pressed`.
- The prompt box is `flow-rich-text-editor .ProseMirror[contenteditable='true']`.
- **Ingredient chips are NOT inside the editable.** They live in `flow-ingredient-bar`, so a
  select-all in the editor leaves them attached. Each chip carries its own remove control
  (`flow-image-ingredient-chip div.hover-icon-overlay`) which is transparent until hovered and needs
  a forced click. There is **no "Clear prompt" button** in the current UI, so
  `clearPromptButton` never resolves — `detachAllReferences()` + `clearPrompt()` is the real path.
- **Do not reload the page between items.** `clearComposerForNextItem()` resets the composer in
  place; reloading the whole app before every item is far more activity than a human produces and
  correlates with rate limiting. Reload only as a fallback, or when
  `generation.resetBetweenItems: "reload"`.
- Prompt-box settings are project defaults that **survive an in-place clear**, so apply them once per
  run. Re-applying opens the settings overlay needlessly, and clicking the trigger while it is open
  closes it — which surfaces as `Could not locate the Flow UI element "modeImageOption"`.
- Clicking the prompt-box `+` (`button.add-menu-trigger`) opens Flow's **asset library inline**
  (`flow-add-menu-asset-list`, search `input[aria-label='Search assets']`, rows
  `button.asset-item[role='option']` with `span.asset-title`). It creates **no file input** — do not
  click "Upload media" just to browse.
- **"Upload media" lives inside that library** and is the only thing that spawns the hidden
  `input[type=file]`. Click it only when a file must actually be uploaded.
- Two different attach behaviours, both measured:
  - clicking an **existing** asset attaches it and closes the library immediately, no "Add to prompt";
  - **uploading** leaves the asset selected and requires clicking **"Add to prompt"**.
- Synthetic drag-and-drop of a `File` onto the prompt box is **rejected** by Flow, so the library is
  the only route. `refMode` only controls whether files are re-uploaded: `reuse` (attach by name,
  upload only when missing), `assets` (never upload), `upload` (always upload).
- Items with `refs: []` must never open the library at all.
- `reuse`/`assets` must produce **zero** new project tiles; that is the regression test for this
  area.
- `input[aria-label='Editable text']` is the **project title**, not the prompt box.
- The grid is a **virtual scroller**: only rendered tiles exist in the DOM.
- **Uploaded references also appear as grid tiles**, labelled with their filename. Generated stills
  are not. `snapshotAssets` records `uploaded` / `hasImage` / `failed` / `canRedo` so uploads and
  refused generations are not mistaken for results.
- **Never identify a result by image `src` alone.** A tile's `src` changes when its thumbnail lazily
  loads, so a 15 MB reference upload appeared as a "new asset" and was downloaded as the output —
  silently producing a copy of the reference under the shot's filename. Results are identified by:
  1. a new tile, not previously seen;
  2. `!uploaded` (label is not a filename) and not matching any of the item's reference names;
  3. preferring tiles that expose a **`redo`** control, which only generated results have.
  `waitForGridToSettle()` must run after references are attached and before the "before" snapshot,
  because a large upload lands well after the attach step returns.
- Flow exports stills as **JPEG**; the extension is sniffed from the bytes.
- Tile `<img>` srcs are signed CDN URLs that `context.request` can fetch, giving full resolution with
  no UI interaction. This is the primary download path.
- A cookie consent bar overlays the prompt-box controls and swallows clicks; `dismissConsent()` must
  run after every navigation.
- The landing page redirects into the last project only after ~15s. Prefer `projectUrl`.
- Google throttles automated runs with *"We noticed some unusual activity"* inside the tile. Two
  wordings appear and they are worth reading: *"Please wait a few moments before retrying"* is an
  explicit rate limit, while *"Please visit the Help Center ... You have not been charged"* appears
  for both rate limits and over-long prompts. A rate limit is handled by waiting
  `generation.cooldownSeconds` (default 180) and retrying the same item, up to `maxCooldowns`
  (default 10) consecutive waits; the counter resets on success. An item whose prompt exceeds
  `maxPromptChars` is skipped instead, because waiting cannot fix it. Observed tolerance after heavy
  use is as low as **3 generations** between refusals.
- **The same "unusual activity" message also means the prompt is too long.** Measured boundary: 2427
  characters accepted, 2510 refused three times with identical references in the same sessions. The
  ceiling is ~2450 characters for the **combined** prompt (job-wide `style` + item `prompt`), and it
  is server-side — the prompt box accepts 5000+ without truncating. `generation.maxPromptChars`
  (default 2420) drives a load-time warning. Do not diagnose a refusal as throttling without first
  checking the prompt length; and note a refusal currently aborts the whole batch.

## Job schema

The primary shape is `refs` (name → local path) plus `images` (items with `file`, `prompt`, `refs`
as **names**). `items` (with `id`) and `matrix` are also accepted, as is a legacy `refs` array of
paths. Item `refs` resolve through the top-level map; a name that is not in the map is treated as an
existing project asset. Missing local files are a warning, never an error — attach-by-name still
works. `item.outputName` comes from `file`; the final extension is always sniffed from the bytes.

A top-level `style` string is applied to every prompt (`stylePosition: "prefix" | "suffix"`).
Unrecognised top-level and `defaults` keys, and extra arrays such as an editorial `shots` list, must
produce a **warning** — never be dropped silently.

Real-world files need two encoding defences, both in place: strip a leading UTF-8 **BOM** in
`readJson` (else `JSON.parse` throws), and detect **mojibake** (UTF-8 read as CP1252, `—` → `â€”`)
via `src/lib/text.js`, warning with a count and offering `--repair-encoding` / `repairEncoding: true`.
`commands/repair.js` fixes the file itself (BOM + mojibake, `.bak` backup, idempotent).

Output naming: `item.outputFile` is the **exact** `file` value from the job, extension included, and
the file is written under that name. Flow only exports JPEG, so a `.png` request is re-encoded with
`FlowDriver.convertToPng`, which uses the browser's canvas — do not add an image library for this.

## Upscaler

`src/upscale/` is a port of the Renderly upscaler (the only component copied from that project,
with explicit permission). Structure:

- `png.js` — self-contained PNG codec (8-bit, non-interlaced) plus separable Lanczos-3 resampling.
  There is deliberately **no image library dependency**; do not add one.
- `engine.js` — Real-ESRGAN ncnn-Vulkan driver: device probing, cache, content validation.
- `index.js` — orchestration and the persisted setting.
- `tools/realesrgan/` — vendored engine (exe, its `vcomp140*.dll`, Vulkan ICD jsons, models,
  upstream readme). Keep the attribution readme with the binaries.

Rules that matter:

1. **Every GPU result is content-checked** (`meanAbsoluteDeviation`) before it is accepted. A bad
   device emits garbage, not an error. On mismatch: discard the device, clear the cache, continue.
2. **Flatten to RGB before the engine.** Alpha-channel input corrupts the engine's output.
3. Device choice is cached in `tools/realesrgan/device_cache.json` (gitignored) and re-probed hourly
   when it says CPU, in case a GPU appears.
4. The engine always runs at a scale the model actually ships (`MODEL_SCALES`); other scales run at
   4x and are Lanczos-downscaled.
5. Upscaling must never fail a batch item — the 720p master is already saved, so warn and continue.
6. The setting persists to `config/upscale.local.json` (gitignored), never to the tracked
   `config/upscale.json`. Comment keys starting with `_` must be stripped before API responses.
7. **Tiers, not multipliers.** The setting is `tier: off|1k|2k|4k` and names the delivered
   resolution — 1920x1080, 2560x1440, 3840x2160, the same numbers WhisperRadar shows for its render
   resolutions. From a 720p master a "3x" was really 4K, which is why the multiplier naming was
   dropped. `normalizeTier` still accepts the legacy numeric values 1, 2 and 4.
8. **Supersample by default.** Run the engine one native scale above what the target needs, then
   Lanczos-downscale — Renderly's approach, cleaner than resampling up.
9. `fit: "exact"` snaps to nominal broadcast ratios (16:9, 9:16, 4:3, 3:4, 1:1) because Flow's
   masters are only close to them (its 9:16 is 768x1376). `fit: "aspect"` preserves the master ratio.
   Every tier is exactly 16:9, so `exact` never stretches a 16:9 source.

## Hard rules

1. **Never hardcode a page selector in JavaScript.** Every selector belongs in
   `config/selectors.json` as an ordered candidate list, accessed through
   `FlowDriver.find(key)` / `Selectors.find()`. If you need a new element, add a new key.
2. **Fail with an actionable error.** Use `SelectorError(key, candidates, details)` so the message
   names the key, lists the candidates, and points at `npm run discover`.
3. **Keep `npm run discover` useful.** It is the primary calibration tool; new interaction points
   should be discoverable from its DOM inventory.
4. **No new dependencies** without a clear reason. Config, args, logging and JSON helpers are
   hand-rolled on purpose.
5. **No comments unless they explain non-obvious intent.** Match the existing sparse style.
6. **Do not commit** unless explicitly asked. `profile/`, `output/`, `state/`, `debug/`,
   `discover/`, `downloads/` and `refs/*` are gitignored runtime data.
7. Local overrides go in `config/settings.local.json` and `config/selectors.local.json` (gitignored);
   never edit committed config to hold machine-specific values.

## Conventions

- ESM only, `import`/`export`, two-space indent, single quotes, trailing commas, semicolons.
- Errors are classes in `src/lib/errors.js`; user-facing failures return a non-zero exit code from
  `commands/*`, never a bare `throw` from `src/cli.js` (it catches and logs).
- Logging goes through `src/lib/log.js`; never `console.log` directly.
- `FlowDriver` is the only place that touches Playwright page APIs. Runner and commands talk to it
  in semantic terms (`setPrompt`, `addReferences`, `generate`, `waitForNewAssets`, `downloadAsset`).
- Paths from config are resolved with `fromRoot()` so they may be absolute or project-relative.
