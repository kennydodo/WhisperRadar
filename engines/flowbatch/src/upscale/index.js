import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { ROOT } from '../lib/paths.js';
import { log } from '../lib/log.js';
import { readJson, writeJson } from '../lib/json.js';
import { decodePng, encodePng, meanAbsoluteDeviation, resizeLanczos, toRgb } from './png.js';
import {
  cachedGpu,
  deviceNameFromOutput,
  invalidateDeviceCache,
  isAvailable,
  loadDeviceCache,
  notInstalledMessage,
  runEngine,
  saveDeviceCache,
} from './engine.js';

/**
 * Upscale tiers, named for the resolution they deliver rather than a multiplier
 * (from a ~720p master, "3x" was really 4K, which was misleading).
 *
 * Every tier is exactly 16:9, so a 16:9 source lands on the intended broadcast
 * resolution. Any other aspect keeps its own ratio and matches the tier's long
 * side instead, so nothing is ever distorted.
 *
 * The sizes and labels match the ones WhisperRadar shows for its render
 * resolutions (1920x1080 / 2560x1440 / 3840x2160), so a tier means the same
 * thing on both sides of that integration.
 */
export const TIERS = {
  '1k': { label: '1920x1080 (Full HD)', longSide: 1920, exact16x9: [1920, 1080] },
  '2k': { label: '2560x1440 (2K)', longSide: 2560, exact16x9: [2560, 1440] },
  '4k': { label: '3840x2160 (4K)', longSide: 3840, exact16x9: [3840, 2160] },
};

export const TIER_NAMES = Object.keys(TIERS);

/** Which native scales each model ships with. */
const MODEL_SCALES = {
  'realesr-animevideov3': [2, 3, 4],
  'realesrgan-x4plus': [4],
  'realesrgan-x4plus-anime': [4],
};

const CONFIG_FILE = path.join(ROOT, 'config', 'upscale.json');
const LOCAL_FILE = path.join(ROOT, 'config', 'upscale.local.json');

export const UPSCALE_DEFAULTS = {
  tier: '2k',
  model: 'realesr-animevideov3',
  tile: 256,
  cpuFallback: true,
  supersample: true,
  fit: 'exact',
  enginePath: null,
};

/** Drop comment keys so they never leak into API responses. */
function stripComments(value) {
  const result = {};
  for (const [key, entry] of Object.entries(value ?? {})) {
    if (key.startsWith('_')) continue;
    result[key] = entry;
  }
  return result;
}

export function loadUpscaleSettings() {
  const base = stripComments(readJson(CONFIG_FILE, { required: false }));
  const local = stripComments(readJson(LOCAL_FILE, { required: false }));
  return { ...UPSCALE_DEFAULTS, ...base, ...local };
}

/** Persist a change so the choice survives restarts. */
export function saveUpscaleSettings(patch) {
  const local = stripComments(readJson(LOCAL_FILE, { required: false }));
  const next = { ...local, ...patch };
  writeJson(LOCAL_FILE, next);
  return { ...UPSCALE_DEFAULTS, ...stripComments(readJson(CONFIG_FILE, { required: false })), ...next };
}

/** Accepts "off", "1k".."4k", or the legacy numeric multiplier. */
export function normalizeTier(value) {
  if (value === undefined || value === null) return UPSCALE_DEFAULTS.tier;
  const text = String(value).trim().toLowerCase();
  if (text === 'off' || text === 'none' || text === '0' || text === 'false') return 'off';
  if (TIER_NAMES.includes(text)) return text;
  const legacy = { 1: '1k', 2: '2k', 4: '4k' }[Number(text)];
  if (legacy) return legacy;
  throw new Error(`Upscale tier must be one of off, ${TIER_NAMES.join(', ')} (got "${value}").`);
}

/**
 * Standard broadcast ratios. Flow's own masters are close to, but not exactly,
 * these — its "9:16" is 768x1376 (0.5581, not 0.5625) — so `fit: "exact"` snaps
 * to the nominal ratio and produces timeline-ready sizes.
 */
const NOMINAL_RATIOS = [
  { ratio: 16 / 9, size: (long) => [long, Math.round((long * 9) / 16)] },
  { ratio: 9 / 16, size: (long) => [Math.round((long * 9) / 16), long] },
  { ratio: 4 / 3, size: (long) => [long, Math.round((long * 3) / 4)] },
  { ratio: 3 / 4, size: (long) => [Math.round((long * 3) / 4), long] },
  { ratio: 1, size: (long) => [long, long] },
];

const RATIO_TOLERANCE = 0.02;

function ratioDistance(a, b) {
  return Math.abs(a - b) / b;
}

function isSixteenNine(width, height) {
  return ratioDistance(width / height, 16 / 9) < 0.01;
}

function nearestNominal(aspect) {
  let best = null;
  for (const candidate of NOMINAL_RATIOS) {
    const distance = ratioDistance(aspect, candidate.ratio);
    if (distance <= RATIO_TOLERANCE && (best === null || distance < best.distance)) {
      best = { ...candidate, distance };
    }
  }
  return best;
}

/**
 * The exact output size for a source at a given tier.
 *
 * `exact`  — snap to the standard ratio and use the tier's dimensions, so the
 *            result drops into a timeline without further scaling.
 * `aspect` — keep the master's own ratio and match the tier's long side, which
 *            never resamples non-uniformly but yields odd sizes.
 */
export function targetSizeFor(sourceWidth, sourceHeight, tier, fit = 'exact') {
  const spec = TIERS[tier];
  if (!spec) throw new Error(`Unknown tier "${tier}".`);

  const aspect = sourceWidth / sourceHeight;
  const long = spec.longSide;

  if (fit === 'aspect') {
    return sourceWidth >= sourceHeight
      ? { width: long, height: Math.round(long / aspect) }
      : { width: Math.round(long * aspect), height: long };
  }

  if (isSixteenNine(sourceWidth, sourceHeight)) {
    const size = spec.exact16x9;
    return { width: size[0], height: size[1] };
  }

  const nominal = nearestNominal(aspect);
  if (nominal) {
    const [width, height] = nominal.size(long);
    return { width, height };
  }

  // Unrecognised ratio: preserve it rather than distort.
  return sourceWidth >= sourceHeight
    ? { width: long, height: Math.round(long / aspect) }
    : { width: Math.round(long * aspect), height: long };
}

function writeFlattened(source, destination) {
  const decoded = decodePng(fs.readFileSync(source));
  const rgb = toRgb(decoded);
  fs.writeFileSync(destination, encodePng(rgb));
  return rgb;
}

/**
 * Upscale a PNG to a resolution tier.
 *
 * GPU (Real-ESRGAN ncnn-Vulkan) is tried first; the CPU Lanczos path is used
 * when no Vulkan device produces valid output. Every GPU result is compared
 * against the source, because a bad device yields garbage rather than an error.
 */
export function upscaleImage(source, destination, options = {}) {
  const settings = { ...loadUpscaleSettings(), ...options };
  const tier = normalizeTier(settings.tier);
  const model = settings.model ?? UPSCALE_DEFAULTS.model;

  if (!fs.existsSync(source)) throw new Error(`Image not found: ${source}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });

  const sourceImage = decodePng(fs.readFileSync(source));

  // "off" keeps the master exactly as Flow produced it.
  if (tier === 'off') {
    fs.copyFileSync(source, destination);
    return { width: sourceImage.width, height: sourceImage.height, method: 'none', device: null, tier };
  }

  const target = targetSizeFor(sourceImage.width, sourceImage.height, tier, settings.fit);

  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flow-upscale-'));
  const flatPath = path.join(workDir, 'input_rgb.png');
  const outPath = path.join(workDir, 'engine_out.png');

  try {
    const flat = writeFlattened(source, flatPath);

    // Supersample: run the engine one native scale above the smallest that
    // covers the target, then Lanczos-downscale. The GAN synthesises at the
    // larger size and the downscale removes its artifacts, which is cleaner
    // than resampling up from the smaller native scale.
    const needed = Math.max(target.width / flat.width, target.height / flat.height);
    const nativeScales = MODEL_SCALES[model] ?? [4];
    const fittingIndex = nativeScales.findIndex((scale) => scale >= needed - 1e-6);
    const baseIndex = fittingIndex >= 0 ? fittingIndex : nativeScales.length - 1;
    const engineScale =
      settings.supersample === false
        ? nativeScales[baseIndex]
        : nativeScales[Math.min(baseIndex + 1, nativeScales.length - 1)];

    if (isAvailable(settings.enginePath)) {
      const gpu = cachedGpu();
      const candidates = [];
      if (gpu !== null) candidates.push(gpu);
      candidates.push(null); // the engine's own device choice as a second chance

      for (const candidate of candidates) {
        fs.rmSync(outPath, { force: true });
        try {
          const result = runEngine({
            source: flatPath,
            destination: outPath,
            model,
            scale: engineScale,
            gpu: candidate,
            tile: settings.tile,
          });
          if (!fs.existsSync(outPath)) continue;

          const produced = decodePng(fs.readFileSync(outPath));
          if (!meanAbsoluteDeviation(flat, toRgb(produced)).ok) {
            // Corrupt tiles: this device cannot be trusted.
            log.debug(`Upscaler device ${candidate} produced output that does not match the source.`);
            invalidateDeviceCache();
            continue;
          }

          const finished =
            produced.width === target.width && produced.height === target.height
              ? produced
              : resizeLanczos(produced, target.width, target.height);
          fs.writeFileSync(destination, encodePng(finished));

          if (candidate !== null) {
            const cache = loadDeviceCache();
            if (cache.device !== candidate) {
              saveDeviceCache({
                device: candidate,
                name: deviceNameFromOutput(result, candidate),
                kind: cache.kind ?? 'vulkan',
                ts: Date.now() / 1000,
              });
            }
          }

          return {
            width: finished.width,
            height: finished.height,
            method: 'realesrgan',
            device: candidate === null ? 'auto' : deviceNameFromOutput(result, candidate),
            model,
            tier,
            engineScale,
          };
        } catch (error) {
          log.debug(`Upscaler attempt on device ${candidate} failed: ${error.message}`);
        }
        if (candidate !== null) invalidateDeviceCache();
      }
      log.warn('No Vulkan device produced valid output; falling back to CPU.');
    } else {
      log.debug(notInstalledMessage());
    }

    if (settings.cpuFallback === false) {
      throw new Error('GPU upscaling failed and the CPU fallback is disabled.');
    }

    const resized = resizeLanczos(flat, target.width, target.height);
    fs.writeFileSync(destination, encodePng(resized));
    return {
      width: resized.width,
      height: resized.height,
      method: 'lanczos',
      device: 'CPU',
      model: null,
      tier,
    };
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
  }
}

/** "Real-ESRGAN (NVIDIA GeForce RTX 3050 Laptop GPU, 4K)" — for logs and the UI. */
export function engineLabel(result) {
  if (!result) return 'unknown';
  if (result.method === 'none') return 'original (upscale off)';
  const tier = TIERS[result.tier]?.label ?? result.tier;
  if (result.method === 'lanczos') return `Lanczos CPU (${tier})`;
  return `Real-ESRGAN (${result.device}, ${tier})`;
}

export function describeUpscaler() {
  const settings = loadUpscaleSettings();
  const cache = loadDeviceCache();
  const spec = TIERS[settings.tier];
  return {
    ...settings,
    engineAvailable: isAvailable(settings.enginePath),
    deviceName: cache.name ?? null,
    deviceKind: cache.kind ?? null,
    tiers: Object.entries(TIERS).map(([id, value]) => ({
      id,
      label: value.label,
      sixteenNine: value.exact16x9.join('x'),
      aspect: `${value.longSide} long side`,
    })),
    target16x9: spec ? spec.exact16x9.join('×') : null,
  };
}
