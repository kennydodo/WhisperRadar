import fs from 'node:fs';
import path from 'node:path';

import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { loadJob } from '../src/jobs/load.js';
import { RunState, STATUS } from '../src/runner/state.js';
import { log } from '../src/lib/log.js';
import { ROOT, fromRoot } from '../src/lib/paths.js';
import { writeJson } from '../src/lib/json.js';
import { sniffImageExtension } from '../src/lib/image.js';
import { engineLabel, loadUpscaleSettings, upscaleImage } from '../src/upscale/index.js';
import { sleep } from '../src/lib/time.js';

/**
 * Adopt images a stopped batch already generated into the Flow project's
 * gallery, without generating anything.
 *
 * When a batch dies on consecutive failures, the stills Flow DID render are
 * usually still sitting in the project gallery - never downloaded. Re-running
 * `generate` pays for a fresh copy of every one of them; this command opens the
 * gallery, matches finished result tiles to the job's still-missing items by
 * prompt, and saves the missing files. It never clicks "Start generation",
 * never types a prompt, and never uploads a reference.
 *
 * Matching, in order of confidence:
 *   1. label - a generated tile is auto-named after its prompt (truncated),
 *      so a long shared prefix is a positive identification, but only when it
 *      names exactly ONE item; a tile ambiguous between items is never guessed;
 *   2. prompt read - click the tile's own "Reuse prompt" control, read the
 *      composer, clear it again (one tile at a time, no pending generation);
 *   3. submission order - only when the remaining tile count equals the
 *      remaining item count and every remaining prompt is distinct.
 */

const SCHEMA_VERSION = 1;

export function normalizePrompt(text) {
  return String(text ?? '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

function commonPrefixLength(a, b) {
  const max = Math.min(a.length, b.length);
  let n = 0;
  while (n < max && a[n] === b[n]) n += 1;
  return n;
}

/**
 * A tile label is the prompt truncated; an item is identified when one
 * normalised string is a full prefix of the other and they agree on at least
 * 20 characters. Prompts shorter than 20 must match exactly - "Cat" must not
 * claim the tile of "Cats resting by the fire".
 */
export function labelMatches(prompt, label) {
  const a = normalizePrompt(prompt);
  const b = normalizePrompt(label);
  if (!a || !b) return false;
  if (a === b) return true;
  const shared = commonPrefixLength(a, b);
  return shared >= 20 && shared >= Math.min(a.length, b.length);
}

/** The exact output file name `loadJob` derives for this item. */
export function expectedFileOf(item) {
  return item.outputFile ?? `${item.outputName}.png`;
}

/** Items whose exact output (any extension, stem counts) is not on disk yet. */
export function missingItems(job, { listFiles = null } = {}) {
  let files = [];
  try {
    files = fs.readdirSync(job.outputsDir);
  } catch {
    /* missing output dir = everything missing */
  }
  if (typeof listFiles === 'function') files = listFiles(job.outputsDir);
  const stems = new Set(files.map((f) => path.basename(f, path.extname(f))));
  return job.items.filter((item) => !stems.has(path.basename(expectedFileOf(item), path.extname(expectedFileOf(item)))));
}

/**
 * Pass 1: identify tiles by their auto-prompt label. A tile that prefix-matches
 * more than one open item is left for the later passes rather than guessed.
 */
export function planMatches(items, tiles) {
  const matches = [];
  const claimedItems = new Set();
  const claimedTiles = new Set();
  for (const tile of tiles) {
    if (claimedTiles.has(tile.key)) continue;
    // A reloaded gallery labels tiles with Flow captions, not prompts, so this
    // pass only fires when the page still carries the prompt-derived name.
    const labels = [tile.label, tile.text].filter(Boolean);
    if (!labels.length) continue;
    const hits = items.filter(
      (item) => !claimedItems.has(item.id) && labels.some((label) => labelMatches(item.prompt, label)),
    );
    if (hits.length !== 1) continue;
    matches.push({ item: hits[0], tile, how: 'label' });
    claimedItems.add(hits[0].id);
    claimedTiles.add(tile.key);
  }
  return {
    matches,
    restItems: items.filter((item) => !claimedItems.has(item.id)),
    restTiles: tiles.filter((tile) => !claimedTiles.has(tile.key)),
  };
}

function promptsDistinct(items) {
  const seen = new Set(items.map((item) => normalizePrompt(item.prompt)));
  return seen.size === items.length;
}

async function saveTileAsset({ driver, tile, item, outputsDir, upscale }) {
  const requested = expectedFileOf(item);
  const requestedExt = path.extname(requested).toLowerCase();
  const stem = path.basename(requested, path.extname(requested));
  const tempPath = path.join(outputsDir, `${stem}.download`);
  fs.mkdirSync(outputsDir, { recursive: true });

  // Same path the runner uses: the tile's signed CDN URL first (no UI), then
  // the export menu by tile index, re-resolved every attempt because the grid
  // moves tiles under a virtual scroller.
  let download = { method: null };
  for (let attempt = 1; attempt <= 3 && !download.method; attempt += 1) {
    const body = await driver.fetchBytes(tile.src);
    if (body) {
      fs.writeFileSync(tempPath, body);
      download = { method: 'cdn' };
      break;
    }
    const current = await driver.snapshotAssets();
    const located = current.entries.find((candidate) => candidate.key === tile.key);
    if (located) {
      download = await driver.downloadAsset(located.index, tempPath, { selector: current.selector });
    }
    if (!download.method && attempt < 3) {
      log.warn(`Could not save ${stem} (attempt ${attempt}/3); retrying.`);
      await sleep(1500);
    }
  }
  if (!download.method) {
    log.warn(`Tile for "${item.id}" exists in the gallery but could not be downloaded.`);
    return null;
  }

  let temp = tempPath;
  let finalExt = sniffImageExtension(tempPath);
  if (requestedExt === '.png' && finalExt !== '.png') {
    const pngPath = `${tempPath}.png`;
    const converted = await driver.convertToPng(tempPath, pngPath).catch(() => false);
    if (converted) {
      fs.rmSync(tempPath, { force: true });
      temp = pngPath;
      finalExt = '.png';
    } else {
      log.warn(`Could not convert ${stem} to PNG; saving as ${finalExt} instead.`);
    }
  }

  const saved = [];
  const destPath = path.join(outputsDir, `${stem}${finalExt}`);
  fs.renameSync(temp, destPath);
  saved.push(destPath);
  log.ok(`Recovered ${path.relative(ROOT, destPath)} (via ${download.method}) for ${item.id}`);

  if (upscale && upscale.tier !== 'off') {
    const upscaledPath = path.join(outputsDir, `${stem}_${upscale.tier}.png`);
    try {
      const upscaled = upscaleImage(destPath, upscaledPath, {
        tier: upscale.tier,
        model: upscale.model,
        fit: upscale.fit,
        tile: upscale.tile,
        cpuFallback: upscale.cpuFallback,
        supersample: upscale.supersample,
        enginePath: upscale.enginePath,
      });
      saved.push(upscaledPath);
      log.ok(`Upscaled ${path.relative(ROOT, upscaledPath)} via ${engineLabel(upscaled)}`);
    } catch (error) {
      log.warn(`Upscale failed for ${stem}: ${error.message}`);
    }
  }
  return saved;
}

/**
 * The recover pass, driver injected so the whole matching/save pipeline is
 * testable without a browser. Never calls generate()/setPrompt()/addReferences().
 */
export async function recoverJob({ job, driver, state, reportPath, upscale = loadUpscaleSettings() }) {
  const missing = missingItems(job);
  const report = {
    schemaVersion: SCHEMA_VERSION,
    jobName: job.name ?? null,
    projectUrl: job.projectUrl ?? null,
    outputsDir: job.outputsDir,
    recoveredAt: new Date().toISOString(),
    alreadyPresent: job.items.filter((item) => !missing.includes(item)).map((item) => item.id),
    recovered: [],
    stillMissing: missing.map((item) => item.id),
  };
  const finish = () => {
    if (reportPath) writeJson(reportPath, report);
    return report;
  };

  if (missing.length === 0) {
    log.info('Every item already has its output on disk - nothing to recover.');
    return finish();
  }

  const tiles = await driver.listGeneratedResults();
  log.info(`Gallery holds ${tiles.length} generated result tile(s); ${missing.length} item(s) still missing.`);
  if (tiles.length === 0) return finish();

  const totalItems = missing.length;
  const open = missing.slice();
  const usedTileKeys = new Set();

  const adopt = async (item, tile, how) => {
    const saved = await saveTileAsset({
      driver,
      tile,
      item,
      outputsDir: job.outputsDir,
      upscale,
    });
    if (!saved) return false;
    report.recovered.push({ id: item.id, file: expectedFileOf(item), how });
    state.update(item.id, { status: STATUS.done, files: saved, error: null });
    state.save();
    const position = open.findIndex((candidate) => candidate.id === item.id);
    if (position >= 0) open.splice(position, 1);
    usedTileKeys.add(tile.key);
    return true;
  };

  const summarise = () => {
    const recoveredIds = new Set(report.recovered.map((entry) => entry.id));
    report.stillMissing = job.items
      .map((item) => item.id)
      .filter((id) => !report.alreadyPresent.includes(id) && !recoveredIds.has(id));
    log.heading('Recover summary');
    log.raw(`  recovered      : ${report.recovered.length} of ${totalItems}`);
    log.raw(`  already present: ${report.alreadyPresent.length}`);
    log.raw(`  still missing  : ${report.stillMissing.length}${report.stillMissing.length ? ` (${report.stillMissing.slice(0, 12).join(', ')})` : ''}`);
    log.raw('  nothing was generated; the gallery itself is untouched.');
    return finish();
  };

  // Pass 1: identify by label. A reloaded gallery captions tiles with Flow
  // text, so this usually claims nothing - but it is free, and it saves reads
  // whenever the page still carries prompt-derived names.
  const { matches, restTiles } = planMatches(open, tiles);
  for (const match of matches) {
    if (!open.some((item) => item.id === match.item.id)) continue;
    await adopt(match.item, match.tile, 'label');
  }
  if (open.length === 0) return summarise();
  log.info(
    `Recover plan: ${matches.length} tile(s) identified by name; reading the ` +
      `remaining ${open.length} item(s) one tile at a time. Each read opens the ` +
      `composer through that tile's own "Reuse prompt" and clears it again - ` +
      `nothing is generated. Files save as soon as an item is identified.`,
  );

  // Pass 2: read each candidate tile's own prompt and save the item the moment
  // it is identified. Tile indices drift as the virtual grid re-renders, so the
  // key->index map is refreshed every 20 reads and whenever a read finds no
  // tile at the remembered index.
  let gridSelector = null;
  let indexByKey = new Map();
  const refreshIndex = async () => {
    const fresh = await driver.snapshotAssets();
    if (fresh.selector) gridSelector = fresh.selector;
    indexByKey = new Map(fresh.entries.map((entry) => [entry.key, entry.index]));
    return fresh;
  };
  const readPromptByKey = async (tile) => {
    if (!gridSelector || !indexByKey.has(tile.key)) await refreshIndex();
    const index = indexByKey.get(tile.key);
    let prompt =
      typeof index === 'number' ? await driver.readTilePrompt(index, { selector: gridSelector }) : null;
    if (prompt === null) {
      const fresh = await refreshIndex();
      const located = fresh.entries.find((entry) => entry.key === tile.key);
      if (!located) return null;
      prompt = await driver.readTilePrompt(located.index, { selector: fresh.selector });
    }
    return prompt;
  };

  const readKeys = new Set();
  let reads = 0;
  for (const tile of restTiles) {
    if (open.length === 0) break;
    if (readKeys.has(tile.key)) continue;
    readKeys.add(tile.key);
    if (reads > 0 && reads % 20 === 0) await refreshIndex();
    reads += 1;
    const fullPrompt = await readPromptByKey(tile);
    if (reads % 10 === 0) {
      log.info(`  … ${reads} tile prompt(s) read; ${open.length} item(s) still open`);
    }
    if (!fullPrompt) continue;
    const norm = normalizePrompt(fullPrompt);
    const hits = open.filter((item) => labelMatches(item.prompt, norm));
    if (hits.length !== 1) continue;
    if (await adopt(hits[0], tile, 'prompt-read')) {
      log.info(`  matched ${hits[0].id} by its tile's own prompt (${open.length} left)`);
    }
  }

  // Pass 3: submission order, and only when it cannot be wrong by
  // construction - equal counts and every remaining prompt distinct. The grid
  // shows the newest first; items are oldest first.
  const spareTiles = tiles.filter((tile) => !usedTileKeys.has(tile.key));
  if (open.length > 0 && open.length === spareTiles.length && promptsDistinct(open)) {
    const byAge = spareTiles.slice().sort((a, b) => b.index - a.index);
    const ordered = open.slice().sort((a, b) => a.index - b.index);
    log.warn(`  ${open.length} item(s) matched by submission order - verify these images`);
    for (let position = 0; position < ordered.length; position += 1) {
      if (!byAge[position]) continue;
      await adopt(ordered[position], byAge[position], 'order');
    }
  }

  return summarise();
}

export async function recoverCommand({ flags, context, positionals }) {
  const jobPath = typeof flags.job === 'string' ? flags.job : positionals[0];
  if (!jobPath) {
    throw new Error('Missing job file. Usage: npm run recover -- --job <job.json> [--report <path>]');
  }

  const { settings } = context;
  const job = loadJob(jobPath, { settings, repairEncoding: flags['repair-encoding'] === true });
  if (typeof flags.output === 'string' && flags.output.trim()) {
    job.outputsDir = fromRoot(flags.output.trim());
  }
  if (typeof flags['project-url'] === 'string' && flags['project-url'].trim()) {
    job.projectUrl = flags['project-url'].trim();
  }
  const reportPath =
    typeof flags.report === 'string' && flags.report.trim() ? path.resolve(flags.report.trim()) : null;

  const missing = missingItems(job);
  if (flags['dry-run'] === true) {
    log.heading(`Recover plan for job "${job.name}"`);
    log.raw(`  project : ${job.projectUrl ?? job.project ?? '(the most recent project - pass --project-url)'}`);
    log.raw(`  outputs : ${path.relative(ROOT, job.outputsDir)}`);
    log.raw(`  missing : ${missing.length} of ${job.items.length} item(s)`);
    for (const item of missing) log.raw(`  - ${item.id}`);
    log.info('Dry run complete; nothing was downloaded.');
    return 0;
  }

  const state = RunState.open(RunState.pathFor(settings.dirs.stateDir, job.name), {
    jobName: job.name,
    jobPath: job.jobPath,
    items: job.items,
    projectUrl: job.projectUrl,
  });
  const stale = state.clearStaleRunning();
  if (stale.length > 0) state.save();
  // A `done` item whose files were deleted after adoption must count as missing
  // here exactly as the generate command reopens it there.
  const reopened = state.clearMissingFiles();
  if (reopened.length > 0) state.save();
  if (missing.length === 0) {
    return await recoverJob({ job, driver: null, state, reportPath, upscale: { tier: 'off' } });
  }
  if (!job.projectUrl) {
    log.warn(
      `No projectUrl for "${job.name}": recover reads the MOST RECENT project, which may ` +
        'be another video\'s. Pass --project-url <url> to be sure.',
    );
  }

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({
    page,
    context: browserContext,
    selectors: context.selectors,
    settings,
  });

  try {
    await driver.goto();
    const signIn = await driver.looksSignedIn();
    if (signIn === 'out') {
      throw new Error('Not signed in. Run `npm run login` with this profile first.');
    }
    if (signIn === 'challenge') {
      throw new Error(
        'Google is asking for extra verification (accounts.google.com). Run ' +
          '`npm run login -- --confirm` and complete the "Verify it\'s you" step, then retry.',
      );
    }
    if (job.projectUrl) {
      await driver.openProject(job.projectUrl);
    } else {
      await driver.ensureProject(job.project);
    }
    await recoverJob({ job, driver, state, reportPath });
    return 0;
  } finally {
    await closeSession(browserContext);
  }
}
