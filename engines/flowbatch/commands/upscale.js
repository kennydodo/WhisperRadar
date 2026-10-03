import fs from 'node:fs';
import path from 'node:path';

import { fromRoot, ROOT } from '../src/lib/paths.js';
import { log } from '../src/lib/log.js';
import {
  TIERS,
  describeUpscaler,
  engineLabel,
  loadUpscaleSettings,
  normalizeTier,
  saveUpscaleSettings,
  targetSizeFor,
  upscaleImage,
} from '../src/upscale/index.js';

function collectInputs(targets) {
  const files = [];
  for (const target of targets) {
    const resolved = fromRoot(target);
    if (!resolved || !fs.existsSync(resolved)) {
      throw new Error(`Not found: ${target}`);
    }
    if (fs.statSync(resolved).isDirectory()) {
      for (const entry of fs.readdirSync(resolved)) {
        if (/\.png$/i.test(entry)) files.push(path.join(resolved, entry));
      }
    } else {
      files.push(resolved);
    }
  }
  return files;
}

/**
 * PNG IHDR width/height without decoding a single pixel. Returns null for
 * anything that is not a PNG, which simply disables the skip guard (the file
 * is then always processed, exactly as before).
 */
export function pngDimensions(file) {
  try {
    const header = Buffer.alloc(24);
    const fd = fs.openSync(file, 'r');
    try {
      if (fs.readSync(fd, header, 0, 24, 0) < 24) return null;
    } finally {
      fs.closeSync(fd);
    }
    if (header.readUInt32BE(0) !== 0x89504e47) return null;
    return { width: header.readUInt32BE(16), height: header.readUInt32BE(20) };
  } catch {
    return null;
  }
}

/**
 * Where one input goes and whether it can be skipped. Pure (dimensions come in
 * as data) so the rules are unit-testable:
 *
 * - `--in-place` replaces the file itself, and NEVER touches a file that
 *   already meets the tier - that makes the pass idempotent, so a batch's
 *   post-download upscale can be re-run without upscaling an upscale.
 * - otherwise today's behaviour is kept: `--out <dir>` writes the same name
 *   into that directory, else a `<stem>_<tier>.png` copy lands beside the
 *   source, and nothing is ever skipped.
 */
export function upscalePlanForFile(
  input,
  { tier, outDir = null, inPlace = false, dimensions = null, fit = 'exact' } = {},
) {
  if (inPlace) {
    if (tier === 'off') return { mode: 'in-place', destination: input, skip: true, reason: 'upscale is off' };
    if (dimensions) {
      const target = targetSizeFor(dimensions.width, dimensions.height, tier, fit);
      if (dimensions.width >= target.width && dimensions.height >= target.height) {
        return {
          mode: 'in-place',
          destination: input,
          skip: true,
          reason: `already ${dimensions.width}x${dimensions.height} (tier ${tier} wants ${target.width}x${target.height})`,
        };
      }
    }
    // Written beside the source and renamed over it only on success, so an
    // interrupted upscale can never leave a half-written master behind.
    return { mode: 'in-place', destination: `${input}.upscale-tmp.png`, skip: false };
  }
  const destination = outDir
    ? path.join(outDir, path.basename(input))
    : path.join(path.dirname(input), `${path.basename(input, path.extname(input))}_${tier}.png`);
  return { mode: outDir ? 'out' : 'beside', destination, skip: false };
}

function printInfo() {
  const info = describeUpscaler();
  log.heading('Upscaler');
  log.raw(`  engine      : ${info.engineAvailable ? 'realesrgan-ncnn-vulkan' : 'NOT INSTALLED'}`);
  log.raw(`  device      : ${info.deviceName ?? 'auto-detect on first use'}`);
  log.raw(`  tier        : ${info.tier}${TIERS[info.tier] ? ` - ${TIERS[info.tier].label}` : ''} (default)`);
  log.raw(`  model       : ${info.model}`);
  log.raw(`  supersample : ${info.supersample ? 'yes' : 'no'}`);
  log.raw(`  fit         : ${info.fit}`);
  log.raw(`  fallback    : ${info.cpuFallback ? 'CPU Lanczos' : 'disabled'}`);
  log.raw('');
  log.raw('  tier  target for 16:9   other ratios (long side)');
  for (const tier of info.tiers) {
    log.raw(`  ${tier.id.padEnd(4)}  ${tier.sixteenNine.padEnd(16)}  ${tier.aspect.padEnd(16)}  ${tier.label}`);
  }
  log.raw('');
  log.raw('Usage: node src/cli.js upscale <file-or-folder> [more...] [--tier 1k|2k|4k|off] [--in-place]');
  log.raw('       node src/cli.js upscale --set-tier 4k');
  log.raw('       --in-place  replace each PNG under its own name; files already at the tier are skipped');
}

export async function upscaleCommand({ flags, positionals }) {
  // `--set-tier` remembers the choice without processing anything.
  const requested = flags['set-tier'] ?? flags['set-scale'];
  if (requested !== undefined) {
    const saved = saveUpscaleSettings({ tier: normalizeTier(requested === true ? '2k' : requested) });
    log.ok(`Upscale tier saved as ${saved.tier}${TIERS[saved.tier] ? ` (${TIERS[saved.tier].label})` : ''}. Future runs will use it.`);
    return 0;
  }

  if (positionals.length === 0) {
    printInfo();
    return 0;
  }

  const settings = loadUpscaleSettings();
  const tier = normalizeTier(flags.tier ?? flags.scale ?? settings.tier);
  const model = typeof flags.model === 'string' ? flags.model : settings.model;
  const outDir = typeof flags.out === 'string' ? fromRoot(flags.out) : null;
  const fit = typeof flags.fit === 'string' ? flags.fit : settings.fit;
  const inPlace = flags['in-place'] === true;
  if (inPlace && outDir) {
    throw new Error('--in-place and --out are mutually exclusive: in-place replaces each file under its own name.');
  }

  const inputs = collectInputs(positionals);
  if (inputs.length === 0) {
    log.warn('No PNG files found to upscale.');
    return 0;
  }

  if (flags.save === true) {
    saveUpscaleSettings({ tier, model, fit });
    log.info(`Saved ${tier} / ${model} / fit=${fit} as the default.`);
  }

  log.heading(`Upscaling ${inputs.length} image(s) to ${tier}${TIERS[tier] ? ` (${TIERS[tier].label})` : ''}${inPlace ? ' in place' : ''}`);
  let failed = 0;
  let skipped = 0;

  for (const input of inputs) {
    const plan = upscalePlanForFile(input, {
      tier,
      outDir,
      inPlace,
      fit,
      dimensions: pngDimensions(input),
    });
    if (plan.skip) {
      skipped += 1;
      log.info(`${path.basename(input)}: skipped (${plan.reason})`);
      continue;
    }

    try {
      const started = Date.now();
      const result = upscaleImage(input, plan.destination, {
        tier,
        model,
        fit,
        tile: settings.tile,
        cpuFallback: settings.cpuFallback,
        supersample: settings.supersample,
        enginePath: settings.enginePath,
      });
      // In-place: swap the finished temp file over the master only now, so a
      // failed or interrupted pass leaves the original untouched.
      if (plan.mode === 'in-place') fs.renameSync(plan.destination, input);
      const seconds = ((Date.now() - started) / 1000).toFixed(1);
      log.ok(
        `${path.relative(ROOT, input)}` +
          `${plan.mode === 'in-place' ? ' (in place)' : ` -> ${path.relative(ROOT, plan.destination)}`} ` +
          `${result.width}x${result.height} via ${engineLabel(result)} (${seconds}s)`,
      );
    } catch (error) {
      if (plan.mode === 'in-place') fs.rmSync(plan.destination, { force: true });
      failed += 1;
      log.error(`${path.basename(input)}: ${error.message}`);
    }
  }

  if (skipped > 0) {
    log.raw(`  ${skipped} file(s) already at ${tier} - skipped, nothing was overwritten.`);
  }
  return failed > 0 ? 1 : 0;
}
