import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

import { ROOT } from '../lib/paths.js';
import { log } from '../lib/log.js';
import { decodePng, encodePng, meanAbsoluteDeviation, toRgb } from './png.js';

/**
 * Real-ESRGAN (ncnn + Vulkan) driver.
 *
 * GPU is preferred; the working device index is probed once and cached. Every
 * engine result is content-checked, because a driver fault or VRAM overrun
 * produces output that looks nothing like the input rather than an error.
 */

export const TOOLS_DIR = path.join(ROOT, 'tools', 'realesrgan');
const CACHE_FILE = path.join(TOOLS_DIR, 'device_cache.json');
const PROBE_FILE = path.join(TOOLS_DIR, '_probe.png');
const PROBE_OUT = path.join(TOOLS_DIR, '_probe_out.png');
const RECACHE_AFTER_SECONDS = 3600;
const PROBE_DEVICE_RANGE = 6;

export function enginePath(override) {
  if (override && String(override).trim()) return path.resolve(String(override).trim());
  return path.join(TOOLS_DIR, 'realesrgan-ncnn-vulkan.exe');
}

export function isAvailable(override) {
  return fs.existsSync(enginePath(override));
}

export function notInstalledMessage() {
  return (
    `Upscaler engine not found at ${enginePath()}. ` +
    'Expected realesrgan-ncnn-vulkan.exe plus its models/ folder in tools/realesrgan/.'
  );
}

/**
 * ICD files shipped next to the engine whose driver actually exists on this
 * machine. nv-vk64.json / igvk64.json carry absolute DriverStore paths, so a
 * checkout copied from another machine names drivers that are not installed;
 * forcing those into the loader makes vkCreateInstance fail and every GPU
 * probe look like a dead card.
 */
export function usableIcdFiles(dir = TOOLS_DIR) {
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .filter((name) => name.endsWith('.json') && name !== 'device_cache.json')
    .filter((name) => {
      try {
        const icd = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8'));
        const library = icd?.ICD?.library_path;
        return typeof library === 'string' && fs.existsSync(library);
      } catch {
        return false;
      }
    })
    .map((name) => path.join(dir, name));
}

/** Point the Vulkan loader at the ICD files shipped next to the engine. */
function icdEnvironment() {
  const files = usableIcdFiles();
  if (files.length === 0) return {};
  const value = files.join(';');
  return { VK_DRIVER_FILES: value, VK_ICD_FILENAMES: value };
}

export function loadDeviceCache() {
  try {
    return JSON.parse(fs.readFileSync(CACHE_FILE, 'utf8'));
  } catch {
    return {};
  }
}

export function saveDeviceCache(data) {
  try {
    fs.mkdirSync(TOOLS_DIR, { recursive: true });
    fs.writeFileSync(CACHE_FILE, JSON.stringify(data), 'utf8');
  } catch {
    /* caching is best effort */
  }
}

function clearDeviceCache() {
  try {
    fs.rmSync(CACHE_FILE, { force: true });
  } catch {
    /* ignore */
  }
}

/** A deterministic gradient, generated rather than shipped, used to test a device. */
function writeProbePng() {
  const size = 64;
  const data = Buffer.alloc(size * size * 3);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const i = (y * size + x) * 3;
      data[i] = (x * 4) & 0xff;
      data[i + 1] = (y * 4) & 0xff;
      data[i + 2] = ((x + y) * 2) & 0xff;
    }
  }
  fs.mkdirSync(TOOLS_DIR, { recursive: true });
  fs.writeFileSync(PROBE_FILE, encodePng({ width: size, height: size, channels: 3, data }));
  return size;
}

function runEngine({ source, destination, model, scale, gpu, tile = 256, timeoutMs = 1800000 }) {
  const args = [
    '-i',
    source,
    '-o',
    destination,
    '-n',
    model,
    '-s',
    String(scale),
    '-t',
    String(tile),
    '-f',
    'png',
  ];
  if (gpu !== null && gpu !== undefined) args.push('-g', String(gpu));

  return spawnSync(enginePath(), args, {
    encoding: 'utf8',
    timeout: timeoutMs,
    env: { ...process.env, ...icdEnvironment() },
    windowsHide: true,
  });
}

function deviceNameFromOutput(result, gpu) {
  const text = `${result?.stdout ?? ''}\n${result?.stderr ?? ''}`;
  if (gpu !== null && gpu !== undefined) {
    const matches = [...text.matchAll(new RegExp(`\\[${gpu}\\s+([^\\]]+)\\]`, 'g'))];
    if (matches.length > 0) return matches[0][1].trim();
  }
  const all = [...text.matchAll(/\[\d+\s+([^\]]+)\]/g)];
  return all.length > 0 ? all[all.length - 1][1].trim() : 'Vulkan device';
}

/** Probe device indices 0..5 and keep the ones that produce sane output. */
function probeDevices() {
  const size = writeProbePng();
  const source = decodePng(fs.readFileSync(PROBE_FILE));
  const devices = new Map();

  for (let index = 0; index < PROBE_DEVICE_RANGE; index += 1) {
    fs.rmSync(PROBE_OUT, { force: true });
    try {
      const result = runEngine({
        source: PROBE_FILE,
        destination: PROBE_OUT,
        model: 'realesr-animevideov3',
        scale: 2,
        gpu: index,
        timeoutMs: 120000,
      });
      if (!fs.existsSync(PROBE_OUT)) continue;
      const candidate = decodePng(fs.readFileSync(PROBE_OUT));
      if (candidate.width !== size * 2 || candidate.height !== size * 2) continue;
      if (!meanAbsoluteDeviation(source, toRgb(candidate)).ok) continue;
      devices.set(index, deviceNameFromOutput(result, index));
    } catch {
      /* device unusable */
    }
  }

  fs.rmSync(PROBE_OUT, { force: true });
  return devices;
}

/** Choose a GPU: NVIDIA first, then the lowest working index. Null means CPU. */
export function pickGpu() {
  const devices = probeDevices();
  if (devices.size === 0) {
    saveDeviceCache({ device: null, name: 'CPU (Lanczos)', kind: 'cpu', ts: Date.now() / 1000 });
    return null;
  }
  const nvidia = [...devices.keys()].find((index) => devices.get(index).toLowerCase().includes('nvidia'));
  const chosen = nvidia !== undefined ? nvidia : Math.min(...devices.keys());
  saveDeviceCache({
    device: chosen,
    name: devices.get(chosen),
    kind: nvidia !== undefined ? 'nvidia' : 'vulkan',
    ts: Date.now() / 1000,
  });
  return chosen;
}

/** Cached GPU index, re-probing occasionally when the cache says CPU. */
export function cachedGpu() {
  const cache = loadDeviceCache();
  if (!cache || Object.keys(cache).length === 0) return pickGpu();
  if (cache.device === null || cache.device === undefined) {
    const age = Date.now() / 1000 - (cache.ts ?? 0);
    return age > RECACHE_AFTER_SECONDS ? pickGpu() : null;
  }
  return Number(cache.device);
}

export function invalidateDeviceCache() {
  clearDeviceCache();
}

export { runEngine, deviceNameFromOutput };

export function engineInfo() {
  const cache = loadDeviceCache();
  return {
    available: isAvailable(),
    exe: enginePath(),
    device: cache.device ?? null,
    deviceName: cache.name ?? 'auto-detect on first use',
    kind: cache.kind ?? 'auto',
  };
}

export function logEngineInfo() {
  const info = engineInfo();
  log.raw(`  engine : ${info.available ? 'realesrgan-ncnn-vulkan' : 'NOT INSTALLED'}`);
  log.raw(`  device : ${info.deviceName}${info.device === null ? '' : ` (index ${info.device})`}`);
  return info;
}
