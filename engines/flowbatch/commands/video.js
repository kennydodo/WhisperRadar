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
  const row = rowFor();
  if ((await row.count().catch(() => 0)) === 0) {
    await shot(page, 'frame-not-found');
    throw new Error(`Could not find "${base}" in the frame picker after upload.`);
  }
  await row.click({ timeout: 8000 });
  await sleep(900);
  const selected = await row.getAttribute('aria-selected').catch(() => null);
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

/** Collect every video URL the page has fetched (the finished clip arrives as one). */
function watchVideoResponses(page) {
  const seen = new Map();
  page.on('response', (response) => {
    const type = response.headers()['content-type'] || '';
    const url = response.url();
    if (/^video\//i.test(type) || /\.mp4(\?|$)/i.test(url)) {
      seen.set(url, { type, status: response.status(), at: Date.now() });
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

/** Videos already saved by this process (never taken twice). */
const saved = new Set();

/** Wait for one new clip, save it, return its size. Throws on a Flow alert / timeout. */
async function waitAndSave({ driver, page, videoResponses, knownSources, knownResponses, timeoutMs, out }) {
  const deadline = Date.now() + timeoutMs;
  let clip = null;
  while (Date.now() < deadline && !clip) {
    await sleep(4000);
    const alert = await driver.freshAlert().catch(() => null);
    if (alert) {
      await shot(page, 'refused');
      throw new Error(`Flow reported a problem: ${JSON.stringify(alert).slice(0, 300)}`);
    }
    const sources = (await pageVideoSources(page)).filter((src) => !knownSources.has(src));
    const fresh = [...videoResponses.keys()].filter((url) => !knownResponses.has(url));
    clip = [...sources, ...fresh].find((url) => /^https?:/i.test(url)) ?? null;
  }
  if (!clip) {
    await shot(page, 'timeout');
    // Last resort: the clip may have finished without looking "new" to the
    // checks above (cached response, recycled player). Take a video this batch
    // has not saved yet, newest first, and say so.
    const candidates = [...videoResponses.keys(), ...(await pageVideoSources(page))]
      .filter((url) => /^https?:/i.test(url) && !saved.has(url));
    log.warn(`Timeout: ${candidates.length} unsaved video URL(s) seen on the page.`);
    clip = candidates.length ? candidates[candidates.length - 1] : null;
    if (!clip) throw new Error('No new video appeared before the timeout. See debug/video-timeout-*.png.');
    log.warn('Using the newest unsaved video as this clip.');
  }
  saved.add(clip);
  log.info(`Clip URL found: ${clip.slice(0, 120)}...`);
  const bytes = await driver.fetchBytes(clip);
  if (!bytes) throw new Error('Found the clip URL but could not download it with the browser session.');
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, bytes);
  log.ok(`Saved ${path.relative(ROOT, out)} (${(bytes.length / 1048576).toFixed(1)} MB)`);
  return bytes.length;
}

/** One clip: attach the start frame, set the prompt, generate, save. */
async function makeClip(ctx, { image, prompt, out, timeoutMs }) {
  const { driver, page, videoResponses } = ctx;
  await attachStartFrame(driver, page, image);
  await driver.setPrompt(prompt);
  await shot(page, 'ready');
  const knownSources = new Set(await pageVideoSources(page));
  const knownResponses = new Set(videoResponses.keys());
  await driver.generate();
  log.info(`Generating - waiting up to ${timeoutMs / 1000}s...`);
  await shot(page, 'generating');
  return waitAndSave({ driver, page, videoResponses, knownSources, knownResponses, timeoutMs, out });
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

/** --job <json>: many clips in one browser session. Writes --report when asked. */
async function runJob(flags, context) {
  const { settings } = context;
  const job = JSON.parse(fs.readFileSync(path.resolve(flags.job), 'utf8'));
  const items = Array.isArray(job.items) ? job.items : [];
  const reportPath = typeof flags.report === 'string' ? path.resolve(flags.report) : null;
  const projectUrl = (typeof flags['project-url'] === 'string' ? flags['project-url'] : job.projectUrl || settings.projectUrl || '').trim();
  const opts = modelOptions({ ...flags, model: flags.model ?? job.model, aspect: flags.aspect ?? job.aspect });
  const timeoutMs = intFlag(flags, 'timeout', 360) * 1000;
  const report = { done: [], failed: [], stopped: false, reason: '' };
  const save = () => reportPath && fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({ page, context: browserContext, selectors: context.selectors, settings });
  const videoResponses = watchVideoResponses(page);
  try {
    if (projectUrl) await driver.openProject(projectUrl);
    else await driver.ensureProject('WhisperRadar motion');
    await configure(driver, page, opts);
    let streak = 0;
    for (const [i, item] of items.entries()) {
      if (fs.existsSync(item.out) && fs.statSync(item.out).size > 10000) {
        report.done.push(item.out);
        continue;
      }
      log.info(`Clip ${i + 1}/${items.length}: ${path.basename(item.image)}`);
      try {
        await makeClip({ driver, page, videoResponses }, { image: path.resolve(item.image), prompt: item.prompt, out: path.resolve(item.out), timeoutMs });
        report.done.push(item.out);
        streak = 0;
      } catch (error) {
        const message = String(error.message ?? error);
        log.error(message);
        report.failed.push({ out: item.out, error: message });
        streak += 1;
        if (OUT_OF_CREDITS.test(message) || streak >= 2) {
          report.stopped = true;
          report.reason = OUT_OF_CREDITS.test(message) ? `Flow stopped the batch: ${message.slice(0, 200)}` : `${streak} clips failed in a row: ${message.slice(0, 200)}`;
          break;
        }
      }
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
