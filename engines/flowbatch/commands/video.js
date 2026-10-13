import fs from 'node:fs';
import path from 'node:path';

import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { log } from '../src/lib/log.js';
import { ROOT, slugify } from '../src/lib/paths.js';
import { timestampSlug, sleep } from '../src/lib/time.js';
import { intFlag } from '../src/lib/args.js';
import { pauseForEnter } from '../src/lib/prompt.js';

/**
 * Image-to-video spike: one start frame + one motion prompt -> one mp4.
 *
 *   npm run video -- --project-url <url> --image refs/shot.png \
 *       --prompt "slow push in, dust drifting" --out output/video-test/shot.mp4
 *
 * Verified controls (discover, 2026-10-12): prompt-box "Settings trigger" opens
 * flow-prompt-box-settings with Mode (Image/Video), "Video type" (Frames /
 * Ingredients), aspect ratio, "Select model family" menu (Omni 1.1 Flash,
 * Veo 3.1 Lite/Fast/Quality), resolution / length toggles (Omni) and output count.
 * In Frames mode the prompt box has "Start" and "End" slots.
 *
 * NOT verified yet: what the Start slot opens, and where the finished clip
 * appears. Every stage writes a screenshot to debug/ so the first runs can be
 * calibrated; use --keep-open to inspect the browser at the end.
 */

const STAGE_DIR = 'debug';

function escapeRe(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

async function shot(page, tag) {
  const file = path.join(ROOT, STAGE_DIR, `video-${slugify(tag)}-${timestampSlug()}.png`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  await page.screenshot({ path: file }).catch(() => {});
  log.debug(`screenshot: ${path.relative(ROOT, file)}`);
}

async function clickRadio(page, label) {
  // Accessible names can carry an icon word ("360p info"), so match the label as a whole word.
  const radio = page.getByRole('radio', { name: new RegExp(`(^|\\s)${label}(\\s|$)`, 'i') }).first();
  if ((await radio.count().catch(() => 0)) === 0) return false;
  if ((await radio.getAttribute('aria-checked').catch(() => null)) === 'true') return true;
  await radio.click({ timeout: 5000 }).catch(() => {});
  await sleep(400);
  return true;
}

async function selectVideoModel(page, model) {
  const trigger = page.locator("button[aria-label='Select model family']").first();
  const current = ((await trigger.innerText().catch(() => '')) || '').replace(/\s+/g, ' ');
  if (current.toLowerCase().includes(model.toLowerCase())) return;
  await trigger.click({ timeout: 5000 });
  await sleep(500);
  const item = page.getByRole('menuitem').filter({ hasText: model }).first();
  if ((await item.count().catch(() => 0)) === 0) {
    await page.keyboard.press('Escape').catch(() => {});
    throw new Error(`Video model "${model}" is not in Flow's model menu.`);
  }
  await item.click({ timeout: 5000 });
  await sleep(600);
}

async function configure(driver, page, opts) {
  await driver.ensureAgentMode(false);
  await driver.openSettingsOverlay();
  await driver.ensureMode('video');
  await sleep(400);
  if (!(await clickRadio(page, 'Frames'))) log.warn('No "Frames" toggle found; continuing in the current video type.');
  await selectVideoModel(page, opts.model);
  if (opts.resolution && !(await clickRadio(page, opts.resolution))) {
    log.warn(`Model has no "${opts.resolution}" option; keeping its default.`);
  }
  if (opts.duration && !(await clickRadio(page, opts.duration))) {
    log.warn(`Model has no "${opts.duration}" option; keeping its default.`);
  }
  await clickRadio(page, opts.aspect);
  await clickRadio(page, 'x1');
  const credits = await page
    .locator('flow-prompt-box-settings')
    .first()
    .innerText()
    .then((text) => (text.match(/(\d+)\s*credits/i) || [])[1])
    .catch(() => null);
  if (credits) log.info(`Flow says this clip will use ${credits} credits.`);
  await shot(page, 'settings');
  await driver.closeSettingsOverlay();
  const summary = await driver.settingsSummary();
  if (summary) log.info(`Prompt-box settings: ${summary}`);
}

/** Put the image into the Start slot. Handles a direct file chooser or Flow's asset library. */
async function attachStartFrame(driver, page, imagePath) {
  const start = page.getByRole('button', { name: /^\s*Start\s*$/ }).first();
  if ((await start.count().catch(() => 0)) === 0) {
    throw new Error('No "Start" slot in the prompt box. Is Video > Frames mode on?');
  }
  const chooserPromise = page.waitForEvent('filechooser', { timeout: 4000 }).catch(() => null);
  await start.click({ timeout: 8000 });
  let chooser = await chooserPromise;
  await sleep(800);
  await shot(page, 'start-clicked');

  const base = path.basename(imagePath);
  const stem = base.replace(/\.[a-z0-9]+$/i, '');
  const rowFor = () =>
    page
      .locator("button.asset-item[role='option']")
      .filter({ has: page.locator('span.asset-title', { hasText: new RegExp(`^\\s*(${escapeRe(base)}|${escapeRe(stem)})\\s*$`, 'i') }) })
      .first();
  const search = async (text) => {
    const box = page.locator("input[aria-label='Search assets']").first();
    if ((await box.count().catch(() => 0)) === 0) return;
    await box.fill(text).catch(() => {});
    await sleep(1200);
  };

  // The picker keeps whatever was selected last time (the previous run's frame!), so
  // "Add to prompt" alone would attach the OLD image. Always select the wanted file by name.
  if (!chooser) {
    await search(stem);
    if ((await rowFor().count().catch(() => 0)) === 0) {
      log.info(`"${base}" is not in the project yet; uploading it.`);
      const upload = page.getByRole('button', { name: /upload\s*media/i }).first();
      if ((await upload.count().catch(() => 0)) === 0) {
        throw new Error('The frame picker has no "Upload media" button. See debug/video-start-clicked-*.png.');
      }
      const second = page.waitForEvent('filechooser', { timeout: 8000 }).catch(() => null);
      await upload.click({ timeout: 8000 });
      chooser = await second;
    }
  }
  if (chooser) {
    await chooser.setFiles(imagePath);
    const until = Date.now() + 120000;
    await sleep(2500);
    while (Date.now() < until && (await rowFor().count().catch(() => 0)) === 0) {
      await search(stem);
    }
  }
  let row = rowFor();
  if ((await row.count().catch(() => 0)) === 0) {
    await shot(page, 'frame-not-found');
    throw new Error(`Could not find "${base}" in the frame picker after upload.`);
  }
  // The sheet can reopen with the last pick still selected; clicking that row
  // then applies it and closes the sheet in one step, so aria-selected reads
  // null on the now-gone element. Re-locate the row on every attempt and treat
  // "sheet closed right after our click" as the frame being attached.
  const sheetGone = async () =>
    (await page.locator("button.asset-item[role='option']").count().catch(() => 0)) === 0;
  let selected = await row.getAttribute('aria-selected').catch(() => null);
  for (let attempt = 1; attempt <= 3 && selected !== 'true'; attempt += 1) {
    await row.click({ timeout: 8000 }).catch(() => {});
    await sleep(900);
    if (await sheetGone()) {
      log.info(`"${base}" was applied and the frame picker closed.`);
      await shot(page, 'picked');
      return;
    }
    row = rowFor();
    if ((await row.count().catch(() => 0)) === 0) break;
    selected = await row.getAttribute('aria-selected').catch(() => null);
    if (selected !== 'true') {
      log.warn(`"${base}" not selected yet (aria-selected=${selected}) - re-searching and clicking again.`);
      await search(stem);
    }
  }
  log.info(`Picked "${base}" in the frame picker (aria-selected=${selected}).`);
  await shot(page, 'picked');
  // The preview loads for a while after an upload; "Add to prompt" stays disabled until it does.
  const addButton = page.getByRole('button', { name: /add to prompt/i }).first();
  const addDeadline = Date.now() + 90000;
  while (Date.now() < addDeadline) {
    if ((await addButton.count().catch(() => 0)) > 0 && (await addButton.isEnabled().catch(() => false))) break;
    await sleep(700);
  }
  if (!(await addButton.isEnabled().catch(() => false))) {
    await shot(page, 'add-disabled');
    throw new Error('"Add to prompt" never became enabled (the preview did not load).');
  }
  await shot(page, 'picked-ready');
  await addButton.click({ timeout: 8000 });
  await sleep(1500);
  await shot(page, 'start-attached');
}

/** Flow's video id (the uuid in /video/<uuid>) - the same clip gets a new signed URL each time. */
function mediaId(url) {
  const m = /\/video\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(String(url || ''));
  return m ? m[1].toLowerCase() : String(url || '');
}

// The signed query is part of the URL; the match may run through JSON escape
// backslashes (Flow writes "=" as \u003d or \u003D, sometimes double-escaped)
// because decodeFlowVideoUrl undoes them before the URL is ever fetched.
const FLOW_VIDEO_URL = /https:\/\/flow-content\.google\/video\/[0-9a-f-]{36}[^\s"'<>]*/gi;

/** Undo JSON string escaping (any case, up to two levels deep) on a URL scraped
 * from raw response text. Without the full ?Expires=...&Signature=... query the
 * CDN refuses the download, so this must never stop at a backslash. */
export function decodeFlowVideoUrl(raw) {
  let url = String(raw);
  for (let pass = 0; pass < 3 && url.includes('\\'); pass += 1) {
    url = url
      .replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
      .replace(/\\([^0-9a-fA-F]|$)/g, '$1');
  }
  return url;
}

/** Every complete flow video URL in a raw JSON/protobuf response body. */
export function extractFlowVideoUrls(text) {
  const urls = [];
  for (const found of String(text).match(FLOW_VIDEO_URL) ?? []) {
    const url = decodeFlowVideoUrl(found);
    if (url) urls.push(url);
  }
  return urls;
}

/**
 * Collect every video the page learns about, keyed by video id: a response that
 * IS a video, and - because Flow only loads a tile's <video> once it is visible -
 * video URLs inside the JSON the app polls for generation status.
 */
function watchVideoResponses(page) {
  const seen = new Map();
  const add = (url, via, status) => {
    const id = mediaId(url);
    if (!seen.has(id)) seen.set(id, { url, via, status, at: Date.now() });
  };
  page.on('response', async (response) => {
    try {
      const type = response.headers()['content-type'] || '';
      const url = response.url();
      if (/^video\//i.test(type) || /\.mp4(\?|$)/i.test(url)) {
        add(url, 'video', response.status());
        return;
      }
      if (!/json|text\/plain|protobuf/i.test(type) || !/google|labs/i.test(new URL(url).host)) return;
      for (const found of extractFlowVideoUrls(await response.text())) add(found, 'json', response.status());
    } catch {
      /* body not available (redirect, aborted, streamed) */
    }
  });
  return seen;
}

async function pageVideoSources(page) {
  return page
    .evaluate(() =>
      Array.from(document.querySelectorAll('video'))
        .map((v) => v.currentSrc || v.src || v.querySelector('source')?.src || '')
        .filter(Boolean),
    )
    .catch(() => []);
}

/** Video ids already saved (this process, plus the ids file in the clips folder). */
const saved = new Set();

function idsFileFor(out) {
  return path.join(path.dirname(out), '.flow-clip-ids.json');
}

function loadSavedIds(out) {
  try {
    for (const id of Object.keys(JSON.parse(fs.readFileSync(idsFileFor(out), 'utf8')))) saved.add(id);
  } catch {
    /* none yet */
  }
}

function rememberSavedId(out, id) {
  saved.add(id);
  const file = idsFileFor(out);
  let map = {};
  try {
    map = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    /* new file */
  }
  map[id] = path.basename(out);
  try {
    fs.writeFileSync(file, JSON.stringify(map, null, 2));
  } catch {
    /* best effort */
  }
}

/** Every video the page or its network traffic knows right now: [{ id, url }]. */
async function videoCandidates(page, videoResponses) {
  const out = new Map();
  for (const url of await pageVideoSources(page)) if (/^https?:/i.test(url)) out.set(mediaId(url), { id: mediaId(url), url });
  for (const [id, info] of videoResponses) if (/^https?:/i.test(info.url)) out.set(id, { id, url: info.url });
  return [...out.values()];
}

/**
 * Flow lists a finished video as a thumbnail with a play badge and only fetches
 * the file once its tile is hovered or opened - so automation sees nothing
 * until it does the same. Hover the newest video tile; when asked, open it.
 * Returns true when the page was navigated (the caller must reopen the project).
 */
async function pokeNewestVideoTile(page, { open }) {
  const tiles = page.locator('flow-grid-tile-container, flow-tile-container');
  if ((await tiles.count().catch(() => 0)) === 0) return false;
  const withBadge = tiles.filter({
    has: page.locator('video, [class*="play" i], mat-icon:text-matches("play", "i"), [aria-label*="play" i]'),
  });
  const tile = (await withBadge.count().catch(() => 0)) > 0 ? withBadge.first() : tiles.first();
  await tile.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
  await tile.hover({ timeout: 4000 }).catch(() => {});
  if (!open) return false;
  const before = page.url();
  await tile.click({ timeout: 4000 }).catch(() => {});
  await sleep(3500);
  return page.url() !== before || (await page.locator('video').count().catch(() => 0)) > 0;
}

async function dumpFirstTile(page) {
  try {
    const html = await page
      .locator('flow-grid-tile-container, flow-tile-container')
      .first()
      .evaluate((node) => node.outerHTML.slice(0, 6000));
    const file = path.join(ROOT, STAGE_DIR, `video-tile-${timestampSlug()}.html`);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, html);
  } catch {
    /* no tile to dump */
  }
}

/** Wait for one new clip, save it, return its size. Throws on a Flow alert / timeout.
 * A clip URL can appear in the status JSON before Flow has finished writing the
 * file, so a failed download is retried until the deadline instead of stopping. */
async function waitAndSave({ driver, page, videoResponses, knownIds, timeoutMs, out, reopen }) {
  const started = Date.now();
  const deadline = started + timeoutMs;
  let clip = null;
  let bytes = null;
  let loggedId = null;
  let fetchFails = 0;
  let pokes = 0;
  let nextPoke = started + 45000;
  let navigated = false;
  const findFresh = async () => {
    const fresh = (await videoCandidates(page, videoResponses)).filter((c) => !knownIds.has(c.id) && !saved.has(c.id));
    return fresh.length ? fresh[fresh.length - 1] : null;
  };
  const tryFetch = async () => {
    if (!clip) return;
    if (loggedId !== clip.id) {
      loggedId = clip.id;
      log.info(`Clip found (${clip.id}): ${clip.url.slice(0, 100)}`);
    }
    bytes = await driver.fetchBytes(clip.url);
    if (!bytes) {
      fetchFails += 1;
      if (fetchFails === 1 || fetchFails % 5 === 0) {
        log.warn(`Clip ${clip.id} is not downloadable yet (attempt ${fetchFails}) - keep waiting for Flow to finish it.`);
      }
    }
  };
  while (Date.now() < deadline && !bytes) {
    await sleep(4000);
    const alert = await driver.freshAlert().catch(() => null);
    if (alert) {
      await shot(page, 'refused');
      throw new Error(`Flow reported a problem: ${JSON.stringify(alert).slice(0, 300)}`);
    }
    clip = await findFresh();
    if (clip) await tryFetch();
    if (!bytes && Date.now() >= nextPoke) {
      nextPoke = Date.now() + 25000;
      pokes += 1;
      if (pokes === 1) await dumpFirstTile(page);
      log.info(`Waiting for the clip (${Math.round((Date.now() - started) / 1000)}s) - opening the newest video tile to make Flow load it.`);
      navigated = (await pokeNewestVideoTile(page, { open: pokes % 2 === 0 })) || navigated;
      await sleep(2500);
      clip = (await findFresh()) || clip;
      await tryFetch();
    }
  }
  if (!clip) {
    await shot(page, 'timeout');
    throw new Error('The clip did not finish or could not be found before the timeout. See debug/video-timeout-*.png.');
  }
  if (!bytes) {
    await shot(page, 'undownloadable');
    throw new Error(
      `Found the clip URL (${clip.id}) but could not download it with the browser session (${fetchFails} attempt(s)). See debug/video-undownloadable-*.png.`,
    );
  }
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, bytes);
  rememberSavedId(out, clip.id);
  log.ok(`Saved ${path.relative(ROOT, out)} (${(bytes.length / 1048576).toFixed(1)} MB)`);
  if (navigated && reopen) await reopen().catch((error) => log.warn(`reopen: ${String(error.message ?? error)}`));
  return bytes.length;
}

/**
 * Adopt a clip a stopped run generated but never downloaded - the video
 * equivalent of image recovery - instead of paying for a fresh generation.
 * When several videos are unsaved, each tile's own "Reuse prompt" text is
 * matched against the item's motion prompt (same rules as image recovery);
 * only an unambiguous single match is adopted, never a guess.
 */
async function adoptOrphanClip(ctx, { out, prompt }) {
  loadSavedIds(out);
  const unsaved = async () =>
    (await videoCandidates(ctx.page, ctx.videoResponses))
      .filter((c) => /^https?:/i.test(c.url) && !saved.has(c.id));
  let candidates = await unsaved();
  if (candidates.length === 0) {
    // Flow only exposes a finished clip's URL once its tile is hovered, so
    // nudge the newest tile before concluding there is nothing to adopt.
    await pokeNewestVideoTile(ctx.page, { open: false });
    await sleep(2500);
    candidates = await unsaved();
  }
  if (candidates.length === 0) return false;
  if (candidates.length > 1) {
    // More than one orphan: read each tile's own prompt and only adopt the
    // tile whose prompt matches THIS item, and matches nothing else pending.
    const tiles = await ctx.driver.listGeneratedResults().catch(() => []);
    const videoTiles = tiles.filter((t) => t.src && mediaId(t.src) !== String(t.src));
    const itemPrompt = normalizeMotionPrompt(prompt);
    const hits = [];
    for (const tile of videoTiles) {
      const tilePrompt = await ctx.driver.readTilePrompt(tile.index, { selector: null }).catch(() => null);
      if (!tilePrompt) continue;
      const tileId = mediaId(tile.src);
      if (motionPromptsMatch(itemPrompt, tilePrompt)) {
        hits.push({ tile, clip: candidates.find((c) => c.id === tileId) });
      }
    }
    // Adopt only an unambiguous single match with a resolvable URL.
    if (hits.length !== 1 || !hits[0].clip) {
      log.info(`${candidates.length} unsaved clip(s) in the project, none matching "${path.basename(out)}" unambiguously - generating instead.`);
      return false;
    }
    const clip = hits[0].clip;
    const bytes = await ctx.driver.fetchBytes(clip.url);
    if (!bytes || bytes.length <= 10000) return false;
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, bytes);
    rememberSavedId(out, clip.id);
    log.ok(`Adopted the clip a previous run generated but never downloaded: ${path.basename(out)} (${(bytes.length / 1048576).toFixed(1)} MB, matched by its tile prompt)`);
    return true;
  }
  const clip = candidates[0];
  const bytes = await ctx.driver.fetchBytes(clip.url);
  if (!bytes || bytes.length <= 10000) return false;
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, bytes);
  rememberSavedId(out, clip.id);
  log.ok(`Adopted the clip a previous run generated but never downloaded: ${path.basename(out)} (${(bytes.length / 1048576).toFixed(1)} MB, ${clip.id.slice(0, 8)})`);
  return true;
}

function normalizeMotionPrompt(text) {
  return String(text ?? '').toLowerCase().replace(/\s+/g, ' ').trim();
}

/** Same rule as image recovery: a shared prefix of >=20 chars that covers the
 * shorter of the two prompts identifies the tile; shorter prompts match only
 * exactly. A tile ambiguous between items is never a match. */
function motionPromptsMatch(itemPrompt, tilePrompt) {
  const a = normalizeMotionPrompt(itemPrompt);
  const b = normalizeMotionPrompt(tilePrompt);
  if (!a || !b) return false;
  if (a === b) return true;
  let shared = 0;
  while (shared < Math.min(a.length, b.length) && a[shared] === b[shared]) shared += 1;
  return shared >= 20 && shared >= Math.min(a.length, b.length);
}

/** One clip: attach the start frame, set the prompt, generate, save. */
async function makeClip(ctx, { image, prompt, out, timeoutMs }) {
  const { driver, page, videoResponses } = ctx;
  await attachStartFrame(driver, page, image);
  await driver.setPrompt(prompt);
  await shot(page, 'ready');
  loadSavedIds(out);
  const knownIds = new Set((await videoCandidates(page, videoResponses)).map((c) => c.id));
  await driver.generate();
  // A refusal banner left by an earlier clip reads as THIS one's outcome (a
  // stale "Failed" killed a clip that was still at 22%), so everything already
  // on the page is marked stale; only a fresh alert may fail the clip.
  await driver.markStaleAlerts().catch(() => {});
  log.info(`Generating - waiting up to ${timeoutMs / 1000}s...`);
  await shot(page, 'generating');
  return waitAndSave({ driver, page, videoResponses, knownIds, timeoutMs, out, reopen: ctx.reopen });
}

/** Veo clips have a fixed length/size; only Omni takes resolution and duration. */
function modelOptions(flags) {
  const model = typeof flags.model === 'string' ? flags.model : 'Veo 3.1 - Lite';
  const omni = /omni/i.test(model);
  return {
    model,
    resolution: typeof flags.resolution === 'string' ? flags.resolution : omni ? '360p' : null,
    duration: typeof flags.duration === 'string' ? flags.duration : omni ? '4s' : null,
    aspect: typeof flags.aspect === 'string' ? flags.aspect : '16:9',
  };
}

const OUT_OF_CREDITS = /credit|quota|limit|insufficient|upgrade/i;

/** After a long generation Flow can leave the prompt box in another mode (no
 * "Start" slot). Re-apply the settings; if that is not enough, reload the project. */
async function ensureFramesMode({ driver, page, opts, projectUrl }) {
  const hasStart = async () =>
    (await page.getByRole('button', { name: /^\s*Start\s*$/ }).count().catch(() => 0)) > 0;
  if (await hasStart()) return;
  log.warn('No "Start" slot - re-applying the video settings.');
  await configure(driver, page, opts).catch((error) => log.warn(`configure: ${String(error.message ?? error)}`));
  if (await hasStart()) return;
  log.warn('Still no "Start" slot - reloading the project page.');
  await shot(page, 'no-start');
  await driver.openProject(projectUrl || page.url());
  await configure(driver, page, opts);
}

/** --job <json>: many clips in one browser session. Writes --report when asked. */
async function runJob(flags, context) {
  const { settings } = context;
  const job = JSON.parse(fs.readFileSync(path.resolve(flags.job), 'utf8'));
  const items = Array.isArray(job.items) ? job.items : [];
  const reportPath = typeof flags.report === 'string' ? path.resolve(flags.report) : null;
  const projectUrl = (typeof flags['project-url'] === 'string' ? flags['project-url'] : job.projectUrl || settings.projectUrl || '').trim();
  const opts = modelOptions({ ...flags, model: flags.model ?? job.model, aspect: flags.aspect ?? job.aspect });
  const timeoutMs = intFlag(flags, 'timeout', 900) * 1000;
  const report = { done: [], failed: [], stopped: false, reason: '' };
  const save = () => reportPath && fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({ page, context: browserContext, selectors: context.selectors, settings });
  const videoResponses = watchVideoResponses(page);
  const ctx = { driver, page, videoResponses };
  try {
    if (projectUrl) await driver.openProject(projectUrl);
    else await driver.ensureProject('WhisperRadar motion');
    await configure(driver, page, opts);
    const homeUrl = projectUrl || page.url();
    const reopen = async () => {
      await driver.openProject(homeUrl);
      await configure(driver, page, opts);
    };
    let madeThisRun = 0;
    for (const [i, item] of items.entries()) {
      if (fs.existsSync(item.out) && fs.statSync(item.out).size > 10000) {
        report.done.push(item.out);
        continue;
      }
      log.info(`Clip ${i + 1}/${items.length}: ${path.basename(item.image)}`);
      // Flow's audio pass intermittently fails a freshly started generation
      // ("Failed"); back-to-back generations make it worse, so settle after
      // each clip and retry a failing one a couple of times before moving on.
      const RETRIES = 2;
      for (let attempt = 1; attempt <= 1 + RETRIES; attempt += 1) {
        if (madeThisRun > 0) {
          log.info(`Settling 30s so the previous clip fully finishes before the next generation starts.`);
          await sleep(30000);
        }
        if (attempt > 1) {
          log.warn(`Retrying clip ${i + 1} (attempt ${attempt}/${1 + RETRIES}) after Flow's failure.`);
          await sleep(20000);
        }
        try {
          if (await adoptOrphanClip(ctx, { out: path.resolve(item.out), prompt: item.prompt })) break;
          await ensureFramesMode({ driver, page, opts, projectUrl });
          await makeClip({ driver, page, videoResponses, reopen }, { image: path.resolve(item.image), prompt: item.prompt, out: path.resolve(item.out), timeoutMs });
          madeThisRun += 1;
          break;
        } catch (error) {
          const message = String(error.message ?? error);
          if (OUT_OF_CREDITS.test(message) || attempt >= 1 + RETRIES) {
            log.error(message);
            await shot(page, 'clip-error');
            report.failed.push({ out: item.out, error: message });
            // one clip at a time: if this one did not finish, the rest do not start
            report.stopped = true;
            report.reason = OUT_OF_CREDITS.test(message)
              ? `Flow stopped the batch: ${message.slice(0, 200)}`
              : `clip ${i + 1} of ${items.length} did not finish, so the rest were not started: ${message.slice(0, 200)}`;
            break;
          }
          log.warn(`Clip ${i + 1} failed (${message.slice(0, 160)}) - will retry.`);
          await shot(page, 'clip-retry');
        }
      }
      if (report.stopped) break;
      report.done.push(item.out);
      save();
    }
    if (report.failed.length && !report.stopped) report.reason = `${report.failed.length} clip(s) failed`;
    save();
    return report.failed.length ? 1 : 0;
  } finally {
    save();
    await closeSession(browserContext);
  }
}

export async function videoCommand({ flags, context }) {
  if (typeof flags.job === 'string') return runJob(flags, context);
  const { settings } = context;
  const image = typeof flags.image === 'string' ? path.resolve(flags.image) : null;
  const prompt = typeof flags.prompt === 'string' ? flags.prompt.trim() : '';
  const projectUrl = typeof flags['project-url'] === 'string' ? flags['project-url'].trim() : settings.projectUrl;
  if (!image || !fs.existsSync(image)) throw new Error('Missing or unreadable --image <file>.');
  if (!prompt) throw new Error('Missing --prompt "<motion description>".');

  const opts = modelOptions(flags);
  const timeoutMs = intFlag(flags, 'timeout', 600) * 1000;
  const out = path.resolve(
    typeof flags.out === 'string' ? flags.out : path.join('output', 'video-test', `${slugify(path.basename(image))}.mp4`),
  );

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({ page, context: browserContext, selectors: context.selectors, settings });
  const videoResponses = watchVideoResponses(page);
  try {
    if (projectUrl) await driver.openProject(projectUrl);
    else await driver.ensureProject('WhisperRadar motion tests');
    await configure(driver, page, opts);
    if (flags.generate === false) {
      await attachStartFrame(driver, page, image);
      await driver.setPrompt(prompt);
      await shot(page, 'ready');
      log.info('--no-generate: stopping before Generate.');
    } else {
      await makeClip({ driver, page, videoResponses }, { image, prompt, out, timeoutMs });
    }
    if (flags['keep-open'] === true) await pauseForEnter('Browser left open (--keep-open).');
    return 0;
  } catch (error) {
    await shot(page, 'error');
    if (flags['keep-open'] === true) {
      log.error(String(error.message ?? error));
      await pauseForEnter('Error - browser left open (--keep-open).');
    }
    throw error;
  } finally {
    await closeSession(browserContext);
  }
}
