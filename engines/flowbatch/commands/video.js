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

async function shot(page, tag) {
  const file = path.join(ROOT, STAGE_DIR, `video-${slugify(tag)}-${timestampSlug()}.png`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  await page.screenshot({ path: file }).catch(() => {});
  log.debug(`screenshot: ${path.relative(ROOT, file)}`);
}

async function clickRadio(page, label) {
  const radio = page.getByRole('radio', { name: new RegExp(`^\\s*${label}\\s*$`, 'i') }).first();
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

  if (!chooser) {
    // Library path: same "Upload media" button as the ingredient picker.
    const upload = page.getByRole('button', { name: /upload\s*media/i }).first();
    if ((await upload.count().catch(() => 0)) === 0) {
      throw new Error(
        'Clicking "Start" opened neither a file chooser nor an asset library with "Upload media". ' +
          'See debug/video-start-clicked-*.png and tell Claude what it shows.',
      );
    }
    const second = page.waitForEvent('filechooser', { timeout: 8000 }).catch(() => null);
    await upload.click({ timeout: 8000 });
    chooser = await second;
  }
  if (chooser) {
    await chooser.setFiles(imagePath);
  } else {
    const input = await driver.waitForFileInput(8000);
    if (!input) throw new Error('No file input appeared after "Upload media".');
    await input.setInputFiles(imagePath);
  }
  await driver.waitForUploadToSettle({ expected: 1 });
  if (await driver.pickerIsOpen()) {
    const add = await driver.findByText('Add to prompt', { timeout: 6000, required: false });
    if (add) await add.locator.click();
    await sleep(1200);
  }
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

export async function videoCommand({ flags, context }) {
  const { settings } = context;
  const image = typeof flags.image === 'string' ? path.resolve(flags.image) : null;
  const prompt = typeof flags.prompt === 'string' ? flags.prompt.trim() : '';
  const projectUrl = typeof flags['project-url'] === 'string' ? flags['project-url'].trim() : settings.projectUrl;
  if (!image || !fs.existsSync(image)) throw new Error('Missing or unreadable --image <file>.');
  if (!prompt) throw new Error('Missing --prompt "<motion description>".');
  if (!projectUrl) throw new Error('Missing --project-url <Flow project URL>.');

  const opts = {
    model: typeof flags.model === 'string' ? flags.model : 'Omni 1.1 Flash',
    resolution: typeof flags.resolution === 'string' ? flags.resolution : '360p',
    duration: typeof flags.duration === 'string' ? flags.duration : '4s',
    aspect: typeof flags.aspect === 'string' ? flags.aspect : '16:9',
  };
  const timeoutMs = intFlag(flags, 'timeout', 600) * 1000;
  const out = path.resolve(
    typeof flags.out === 'string' ? flags.out : path.join('output', 'video-test', `${slugify(path.basename(image))}.mp4`),
  );

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({ page, context: browserContext, selectors: context.selectors, settings });
  const videoResponses = watchVideoResponses(page);
  try {
    await driver.openProject(projectUrl);
    await configure(driver, page, opts);
    await attachStartFrame(driver, page, image);
    await driver.setPrompt(prompt);
    await shot(page, 'ready');

    const knownSources = new Set(await pageVideoSources(page));
    const knownResponses = new Set(videoResponses.keys());
    await driver.generate();
    log.info(`Generating (${opts.model}, ${opts.resolution}, ${opts.duration}) - waiting up to ${timeoutMs / 1000}s...`);
    await shot(page, 'generating');

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
      throw new Error('No new video appeared before the timeout. See debug/video-timeout-*.png.');
    }
    log.info(`Clip URL found: ${clip.slice(0, 120)}...`);
    const bytes = await driver.fetchBytes(clip);
    if (!bytes) throw new Error('Found the clip URL but could not download it with the browser session.');
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, bytes);
    log.ok(`Saved ${path.relative(ROOT, out)} (${(bytes.length / 1048576).toFixed(1)} MB)`);
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
