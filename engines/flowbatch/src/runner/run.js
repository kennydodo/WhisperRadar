import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline/promises';

import { log } from '../lib/log.js';
import { ROOT } from '../lib/paths.js';
import { sleep } from '../lib/time.js';
import { GenerationError } from '../lib/errors.js';
import { sniffImageExtension } from '../lib/image.js';
import { engineLabel, loadUpscaleSettings, upscaleImage } from '../upscale/index.js';
import { STATUS } from './state.js';

function resolveGenSettings(item, settings) {
  const gen = settings.generation ?? {};
  return {
    mode: item.mode ?? gen.mode ?? 'image',
    model: item.model ?? gen.model ?? null,
    aspectRatio: item.aspectRatio ?? gen.aspectRatio ?? null,
    outputs: Math.max(1, Number(item.outputs ?? gen.outputs ?? 1) || 1),
    agent: item.agent ?? gen.agent === true,
  };
}

/**
 * Identity of the prompt-box settings that must be in effect to generate an
 * item. A job can vary mode/model/aspectRatio/outputs per shot (e.g. PU/PD ask
 * for 1:1 while the rest stay 16:9), so the runner re-applies the settings
 * whenever this key changes between items, not just once per run.
 */
function settingsKeyOf(genSettings) {
  return [
    genSettings.mode,
    genSettings.model ?? '',
    genSettings.aspectRatio ?? '',
    genSettings.outputs,
    genSettings.agent === true ? 'agent' : 'standard',
  ].join('|');
}

export function selectItems(items, { only, limit, resume, state }) {
  let selected = items;
  if (only && only.length > 0) {
    const wanted = new Set(only);
    const missing = [...wanted].filter((id) => !items.some((item) => item.id === id));
    if (missing.length > 0) throw new Error(`--only referenced unknown item id(s): ${missing.join(', ')}`);
    selected = selected.filter((item) => wanted.has(item.id));
  }
  // Skip finished items BEFORE applying the limit, so `--limit 15` means fifteen
  // items actually run rather than fifteen selected minus whatever is already done.
  if (resume && state) selected = selected.filter((item) => !state.isDone(item.id));
  if (limit && limit > 0) selected = selected.slice(0, limit);
  return selected;
}

async function pauseForInspection(message) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    await rl.question(`${message}\nPress Enter to continue...`);
  } finally {
    rl.close();
  }
}

export function printPlan(job, items, settings) {
  const upscale = loadUpscaleSettings();
  log.heading(`Plan for job "${job.name}"`);
  log.raw(`  project     : ${job.projectUrl ?? job.project ?? '(new project each run)'}`);
  log.raw(`  outputs dir : ${path.relative(ROOT, job.outputsDir)}`);
  log.raw(
    `  upscale     : ${upscale.tier === 'off' ? 'off' : `${upscale.tier.toUpperCase()} via ${upscale.model}`}` +
      `${upscale.supersample && upscale.tier !== 'off' ? ' (supersampled)' : ''}`,
  );
  log.raw(`  items       : ${items.length}`);
  for (const item of items) {
    const gen = resolveGenSettings(item, settings);
    log.raw(
      `  - ${item.outputName.padEnd(24)} ${gen.mode}/${gen.aspectRatio ?? 'default'} x${gen.outputs} ` +
        `refs=[${item.refNames.join(', ')}] :: ${item.prompt.slice(0, 60)}${item.prompt.length > 60 ? '…' : ''}`,
    );
  }
}

export async function runJob({ job, driver, state, settings, options }) {
  fs.mkdirSync(job.outputsDir, { recursive: true });

  const items = selectItems(job.items, {
    only: options.only,
    limit: options.limit,
    resume: options.resume,
    state,
  });

  if (items.length === 0) {
    log.info('Nothing to do — every selected item is already complete. Use --no-resume to re-run.');
    return { ok: 0, failed: 0, skipped: 0, results: [] };
  }

  if (options.dryRun) {
    printPlan(job, items, settings);
    log.info('Dry run complete; nothing was generated.');
    return { ok: 0, failed: 0, skipped: 0, results: [] };
  }

  if (job.projectUrl) {
    await driver.openProject(job.projectUrl);
  } else {
    await driver.goto();
  }

  const signIn = await driver.looksSignedIn();
  if (signIn === 'out') {
    throw new Error(
      'Flow is showing a signed-out page. Run `npm run login` first so the persistent profile holds a Google session.',
    );
  }
  if (signIn === 'challenge') {
    throw new Error(
      'Google is asking for extra verification (accounts.google.com). Run `npm run login -- --confirm` and ' +
        'complete the "Verify it\'s you" step, then retry.',
    );
  }
  if (signIn === 'unknown') {
    log.warn('Could not confirm the signed-in state (calibrate "signedIn"/"signedOut"). Continuing anyway.');
  }

  if (!job.projectUrl) {
    await driver.ensureProject(job.project);
  }

  const upscale = loadUpscaleSettings();
  const upscaleEnabled = upscale.tier !== 'off';
  if (upscaleEnabled) log.info(`Upscaling every result to ${upscale.tier.toUpperCase()} (${upscale.model}).`);

  const gen = settings.generation ?? {};
  const cooldownMs = Number(options.cooldownSeconds ?? gen.cooldownSeconds ?? 180) * 1000;
  const maxCooldowns = Number(options.maxCooldowns ?? gen.maxCooldowns ?? 10);
  // Stop the WHOLE batch after this many item failures in a row. Consecutive
  // failures usually mean the Flow session/UI broke (the composer gone, signed
  // out) - grinding through the rest just burns the account and leaves the
  // remaining items "failed" for no reason. Stop and let a later run resume
  // (state is kept). 0 disables the guard; default 3.
  const maxConsecutiveFailures = Number(
    options.maxConsecutiveFailures ?? gen.maxConsecutiveFailures ?? 3,
  );
  const maxPromptChars = Number(gen.maxPromptChars ?? 2420);
  // Consecutive rate-limit waits; reset whenever an item succeeds.
  let cooldownsUsed = 0;
  let consecutiveFailures = 0;
  const globalRetries = Number(gen.retries ?? 0);
  const retryDelayMs = Number(gen.retryDelayMs ?? 5000);
  const delayBetweenItemsMs = Number(options.delayBetweenItemsMs ?? gen.delayBetweenItemsMs ?? 0);
  const resetMode = gen.resetBetweenItems ?? 'reload';
  const reapply = gen.reapplySettingsAfterReset !== false;

  let ok = 0;
  let failed = 0;
  let aborted = false;
  const results = [];
  // The prompt-box settings are configured as needed and re-checked per item:
  // a job can carry a different aspect ratio (or model/outputs) per shot, so
  // "configured once" is not enough - the settings in effect must match the
  // item about to generate. A cooldown retry of the same item must not
  // reconfigure them, which is why this tracks the applied key rather than a
  // `firstItem` flag flipped at the end of the loop body.
  let settingsApplied = false;
  let appliedSettingsKey = null;

  for (const item of items) {
    log.heading(`Item ${item.id} (${item.index + 1}/${job.items.length})`);
    const genSettings = resolveGenSettings(item, settings);
    const settingsKey = settingsKeyOf(genSettings);
    const expected = genSettings.outputs;
    const refMode = item.refMode ?? job.refMode ?? gen.refMode ?? 'upload';
    const retries = Number(item.retries ?? globalRetries);

    aborted = false;
    let attempt = 0;
    let lastError = null;
    let succeeded = false;

    while (attempt <= retries && !succeeded) {
      attempt += 1;
      state.update(item.id, { status: STATUS.running, attempts: attempt, error: null });
      state.save();

      try {
        if (!settingsApplied) {
          await driver.applyGenerationSettings(genSettings);
          settingsApplied = true;
          appliedSettingsKey = settingsKey;
        } else {
          if (resetMode === 'reload') {
            log.debug('Reloading the project to reset the prompt box.');
            await driver.reload();
          } else {
            // Clear the composer in place instead of reloading the whole app.
            // A reload is the fallback if the composer will not come clean.
            const cleared = await driver.clearComposerForNextItem();
            if (!cleared) {
              log.warn('Composer could not be cleared in place; reloading the project.');
              await driver.reload();
            }
          }
          // The in-place clear leaves the PREVIOUS item's prompt-box settings in
          // place - it does not restore this item's - so when they differ (e.g.
          // PU/PD need 1:1 while the last shot was 16:9) they must be applied
          // again. applyGenerationSettings() no-ops when they already match, so
          // this stays cheap for runs with one ratio throughout.
          if (settingsKey !== appliedSettingsKey || reapply) {
            await driver.applyGenerationSettings(genSettings);
            appliedSettingsKey = settingsKey;
          }
        }

        if (refMode === 'mention') {
          await driver.clearPrompt();
          await driver.typePrompt(item.prompt);
          await driver.mentionReferences(item.refNames);
        } else {
          await driver.setPrompt(item.prompt);
          await driver.addReferences(item.refs, { mode: refMode });
        }

        // Snapshot AFTER the references are attached and the grid has stopped
        // changing: uploading a reference also adds a tile, and a 15 MB upload
        // lands well after the attach step returns.
        const before = await driver.waitForGridToSettle();
        log.debug(`Assets before generation: ${before.entries.length}`);

        log.info(
          `Generating (${genSettings.mode}, ${genSettings.aspectRatio ?? 'default ratio'}, x${expected}, ${item.refs.length} ref(s))…`,
        );
        await driver.generate();

        const outcome = await driver.waitForNewAssets(before, expected, {
          timeout: item.timeoutMs ?? settings.timeouts.generationMs,
          excludeNames: item.refNames,
        });

        const saved = [];
        for (let index = 0; index < outcome.added.length; index += 1) {
          const entry = outcome.added[index];

          // The job's `file` field names the output exactly, extension included.
          // Several results for one item get a numeric suffix before the extension.
          const requested = item.outputFile ?? `${item.outputName}.png`;
          const requestedExt = path.extname(requested).toLowerCase();
          const requestedStem = path.basename(requested, path.extname(requested));
          const stem = outcome.added.length > 1 ? `${requestedStem}-${index + 1}` : requestedStem;
          let tempPath = path.join(job.outputsDir, `${stem}.download`);

          // The tile's signed CDN URL serves the full-resolution still and needs no
          // UI interaction, so try it first and fall back to the export menu.
          // Retried, because a tile moves as the grid changes: failing the item here
          // would abandon an image Flow has already generated and paid for, and the
          // retry would leave a duplicate behind. The tile is re-resolved each
          // attempt instead of trusting the index from the detection pass.
          let download = { method: null };
          for (let attempt = 1; attempt <= 3 && !download.method; attempt += 1) {
            const current = await driver.snapshotAssets();
            const located = current.entries.find((candidate) => candidate.key === entry.key);
            const tileIndex = located ? located.index : entry.index;
            const selector = current.selector ?? outcome.selector;
            const body = await driver.fetchAssetBytes(tileIndex, { selector });
            if (body) {
              fs.writeFileSync(tempPath, body);
              download = { method: 'cdn' };
              break;
            }
            download = await driver.downloadAsset(tileIndex, tempPath, { selector });
            if (!download.method && attempt < 3) {
              log.warn(`Could not save ${stem} (attempt ${attempt}/3); retrying.`);
              await sleep(1500);
            }
          }
          if (!download.method) {
            throw new GenerationError(
              'Generated the asset but could not save it after 3 attempts. Calibrate "assetMenuButton" ' +
                'and "downloadMenuItem".',
            );
          }

          // Flow only exports JPEG. When the job asked for .png, re-encode it so
          // the file's name and its contents agree.
          const sourceExt = sniffImageExtension(tempPath);
          let finalExt = sourceExt;
          if (requestedExt === '.png' && sourceExt !== '.png') {
            const pngPath = `${tempPath}.png`;
            const converted = await driver.convertToPng(tempPath, pngPath).catch(() => false);
            if (converted) {
              fs.rmSync(tempPath, { force: true });
              tempPath = pngPath;
              finalExt = '.png';
            } else {
              log.warn(`Could not convert ${stem} to PNG; saving as ${sourceExt} instead.`);
            }
          } else if (requestedExt && requestedExt !== sourceExt) {
            finalExt = sourceExt;
          }

          const destPath = path.join(job.outputsDir, `${stem}${finalExt}`);
          fs.renameSync(tempPath, destPath);
          saved.push(destPath);
          log.ok(`Saved ${path.relative(ROOT, destPath)} (via ${download.method})`);

          // Flow hands back roughly 720p. Upscale next to the original so the
          // 720p master survives for a different level later.
          if (upscaleEnabled) {
            const upscaledPath = path.join(job.outputsDir, `${stem}_${upscale.tier}.png`);
            try {
              const started = Date.now();
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
              log.ok(
                `Upscaled ${path.relative(ROOT, upscaledPath)} to ${upscaled.width}x${upscaled.height} ` +
                  `via ${engineLabel(upscaled)} (${((Date.now() - started) / 1000).toFixed(1)}s)`,
              );
            } catch (error) {
              // The 720p still is already saved, so this must not fail the item.
              log.warn(`Upscale failed for ${stem}: ${error.message}`);
            }
          }
        }

        state.update(item.id, { status: STATUS.done, files: saved, error: null });
        state.save();
        succeeded = true;
        ok += 1;
        cooldownsUsed = 0;
        consecutiveFailures = 0;
        results.push({ id: item.id, status: STATUS.done, files: saved });
      } catch (error) {
        lastError = error;
        const message = String(error.message ?? error);
        log.error(`Attempt ${attempt}/${retries + 1} failed for "${item.id}": ${message.split('\n')[0]}`);

        if (options.dumpOnError !== false) {
          const dump = await driver.dumpDebug(`error-${item.id}`, settings.dirs).catch(() => null);
          if (dump) log.info(`Debug capture: ${path.relative(ROOT, dump.screenshot)}`);
        }

        if (error.retryable === false) {
          const rateLimited = /unusual activity|wait a few moments/i.test(message);

          // An over-long prompt is refused with the same wording, but waiting
          // will never fix it, so that item is skipped instead of cooling down.
          if (item.prompt.length > maxPromptChars) {
            log.error(
              `"${item.id}" has a ${item.prompt.length}-character prompt (limit ${maxPromptChars}); ` +
                'skipping it rather than waiting.',
            );
            break;
          }

          // Retrying inside the SAME session is usually pointless: Flow gates
          // generation behind reCAPTCHA Enterprise, and the score belongs to the
          // browser session, so a refusal will not improve by waiting. Worse,
          // each refused attempt is another negative signal on that session, so
          // grinding here lowers the score further. Default is to stop and let
          // the next run (ideally a fresh profile) pick the item up.
          if (rateLimited && maxCooldowns > 0 && cooldownsUsed < maxCooldowns) {
            cooldownsUsed += 1;
            log.warn(
              `Flow is rate limiting. Waiting ${Math.round(cooldownMs / 1000)}s, then retrying ` +
                `"${item.id}" (cooldown ${cooldownsUsed}/${maxCooldowns}).`,
            );
            await sleep(cooldownMs);
            // A cooldown is not a failed attempt, so do not spend the retry budget.
            attempt -= 1;
            continue;
          }

          if (rateLimited) {
            log.error(
              maxCooldowns > 0
                ? `Still refused after ${maxCooldowns} cooldown(s); stopping the batch.`
                : 'Flow refused the generation. Stopping rather than retrying: the block is a reCAPTCHA ' +
                    'score on this browser profile, so retries in the same session only lower it. ' +
                    'Re-run later, ideally with a fresh profile (--profile <dir>).',
            );
          } else {
            log.error('This failure is not retryable; stopping the batch after this item.');
          }
          aborted = true;
          break;
        }

        const willRetry = attempt <= retries;
        if (willRetry) {
          log.warn(`Retrying "${item.id}" in ${Math.round(retryDelayMs / 1000)}s…`);
          await sleep(retryDelayMs);
          // Recover in place. A retry used to reload the whole Flow app, which is
          // slow and is exactly the per-item reload the batch is meant to avoid;
          // the composer survives an in-place clear. Reloading is a last resort
          // for when the composer will not come clean.
          if (resetMode !== 'reload') {
            const cleared = await driver.clearComposerForNextItem().catch(() => false);
            if (!cleared) {
              await driver.reload().catch(() => {});
              if (reapply) await driver.applyGenerationSettings(genSettings).catch(() => {});
            }
          }
        }
      }
    }

    if (!succeeded) {
      state.update(item.id, { status: STATUS.failed, error: String(lastError?.message ?? lastError) });
      state.save();
      failed += 1;
      consecutiveFailures += 1;
      results.push({ id: item.id, status: STATUS.failed, error: String(lastError?.message ?? lastError) });

      if (options.pauseOnError ?? gen.pauseOnError) {
        await pauseForInspection(`Paused after failure on "${item.id}".`);
      }
      if (options.failFast) {
        log.error('Stopping early because --fail-fast is set.');
        break;
      }
      if (maxConsecutiveFailures > 0 && consecutiveFailures >= maxConsecutiveFailures) {
        log.error(
          `Stopping: ${consecutiveFailures} item(s) failed in a row - the Flow ` +
            'session or UI is likely broken (composer gone / signed out). ' +
            'Re-run later; state is kept so it resumes the unrendered items.',
        );
        break;
      }
    }

    if (aborted) break;
    if (delayBetweenItemsMs > 0) await sleep(delayBetweenItemsMs);
  }

  const counts = state.counts();
  log.heading('Summary');
  log.raw(
    `  done=${counts.done} failed=${counts.failed} pending=${counts.pending} skipped=${counts.skipped} ` +
      `(this run: ok=${ok} failed=${failed})`,
  );
  log.raw(`  outputs: ${path.relative(ROOT, job.outputsDir)}`);
  log.raw(`  state  : ${path.relative(ROOT, state.filePath)}`);

  return { ok, failed, skipped: counts.skipped, results };
}
