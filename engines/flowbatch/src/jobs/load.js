import fs from 'node:fs';
import path from 'node:path';

import { readJson } from '../lib/json.js';
import { ConfigError } from '../lib/errors.js';
import { log } from '../lib/log.js';
import { ROOT, fromRoot, slugify } from '../lib/paths.js';
import { countMojibake, repairMojibake } from '../lib/text.js';

const KNOWN_TOP_LEVEL = new Set([
  'name',
  'project',
  'projectUrl',
  'outputsDir',
  'refMode',
  'refs',
  'defaults',
  'style',
  'stylePosition',
  'repairEncoding',
  'images',
  'items',
  'matrix',
]);

const KNOWN_DEFAULTS = new Set([
  'mode',
  'agent',
  'model',
  'aspectRatio',
  'outputs',
  'refs',
  'refMode',
  'retries',
  'timeoutMs',
  'promptPrefix',
  'promptSuffix',
  'stylePosition',
  'maxPromptChars',
]);

function warnUnknownKeys(source, known, label, warnings) {
  for (const key of Object.keys(source ?? {})) {
    if (key.startsWith('_')) continue;
    if (!known.has(key)) warnings.push(`${label}: unrecognised key "${key}" is ignored.`);
  }
}

const MODES = new Set(['image', 'video']);
const REF_MODES = new Set(['reuse', 'upload', 'assets', 'mention']);
const IMAGE_EXT = /\.(png|jpe?g|webp|gif|heic?|bmp|tiff?)$/i;

function asArray(value) {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value : [value];
}

/**
 * The top-level `refs` block maps an asset name to a local file:
 *
 *   "refs": { "Maya": "E:/.../Maya.png", "Dana": "E:/.../Dana.png" }
 *
 * The name is what the asset is called inside the Flow project; the path is only
 * used to upload the file when that name is missing from the project.
 */
function buildRefMap(rawRefs, jobDir, warnings) {
  const map = new Map();
  if (rawRefs === undefined || rawRefs === null) return map;

  if (Array.isArray(rawRefs)) {
    // Legacy form: a plain list of paths. The asset name defaults to the filename stem.
    for (const entry of rawRefs) {
      const resolved = locatePath(String(entry), jobDir);
      if (!resolved) {
        warnings.push(`Reference file not found: "${entry}" (the asset can still be attached by name).`);
        continue;
      }
      map.set(path.basename(resolved, path.extname(resolved)), resolved);
    }
    return map;
  }

  if (typeof rawRefs !== 'object') {
    throw new ConfigError('Top-level "refs" must be an object of name -> path, or an array of paths.');
  }

  for (const [name, value] of Object.entries(rawRefs)) {
    const resolved = locatePath(String(value), jobDir);
    if (!resolved) {
      // Not fatal: the asset usually already exists in the Flow project, and the
      // local copy is only needed to upload it when the name is missing.
      warnings.push(`Reference "${name}": local file not found at "${value}" (attach-by-name still works).`);
      map.set(name, null);
      continue;
    }
    map.set(name, resolved);
  }
  return map;
}

function locatePath(refPath, jobDir) {
  const candidates = [];
  if (path.isAbsolute(refPath)) candidates.push(refPath);
  else {
    candidates.push(path.resolve(jobDir, refPath));
    candidates.push(path.resolve(ROOT, refPath));
  }
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return path.normalize(candidate);
  }
  return null;
}

function resolveExistingPath(refPath, jobDir) {
  const found = locatePath(refPath, jobDir);
  if (found) return found;
  throw new ConfigError(
    `Reference image not found: "${refPath}". It is not in the job's "refs" map either, so it cannot be ` +
      'attached by name or uploaded.',
  );
}

/**
 * Resolve one entry of an item's `refs` list. Entries are asset names; the local
 * path comes from the top-level map when the name is known.
 */
function resolveRefEntry(entry, refMap, jobDir, warnings) {
  if (entry && typeof entry === 'object') {
    const name = String(entry.name ?? path.basename(String(entry.path ?? ''), path.extname(String(entry.path ?? ''))));
    const refPath = entry.path ? resolveExistingPath(String(entry.path), jobDir) : (refMap.get(name) ?? null);
    return { name, path: refPath };
  }

  const value = String(entry).trim();
  if (!value) throw new ConfigError('Empty entry in an item "refs" list.');

  // A known asset name from the top-level "refs" map.
  if (refMap.has(value)) return { name: value, path: refMap.get(value) };

  // Not in the map: an absolute path, or a path-looking string.
  const looksLikePath = path.isAbsolute(value) || /[\\/]/.test(value) || IMAGE_EXT.test(value);
  if (looksLikePath) {
    const resolved = locatePath(value, jobDir);
    if (resolved) return { name: path.basename(resolved, path.extname(resolved)), path: resolved };
    const stem = path.basename(value, path.extname(value));
    warnings.push(`Reference "${value}" is not on disk; treating "${stem}" as a project asset name.`);
    return { name: stem, path: null };
  }

  // Otherwise it is an asset that must already exist in the Flow project.
  return { name: value, path: null };
}

function normalizeRefs(entries, refMap, jobDir, label, warnings) {
  const resolved = asArray(entries).map((entry) => resolveRefEntry(entry, refMap, jobDir, warnings));
  const seen = new Set();
  for (const ref of resolved) {
    const key = ref.name.toLowerCase();
    if (seen.has(key)) throw new ConfigError(`Duplicate reference "${ref.name}" in ${label}.`);
    seen.add(key);
  }
  return resolved;
}

function expandMatrix(matrix, defaults, refMap, jobDir, warnings) {
  const prompts = asArray(matrix.prompts);
  if (prompts.length === 0) throw new ConfigError('matrix.prompts must contain at least one prompt.');

  const refSets = matrix.refSets ?? {};
  let sets;
  if (Array.isArray(refSets)) {
    sets = refSets.map((refs, index) => ({ name: `r${index + 1}`, refs: asArray(refs) }));
  } else {
    sets = Object.entries(refSets).map(([name, refs]) => ({ name, refs: asArray(refs) }));
  }
  if (sets.length === 0) {
    throw new ConfigError('matrix.refSets must contain at least one reference set (or be omitted for prompt-only runs).');
  }

  const items = [];
  prompts.forEach((prompt, promptIndex) => {
    sets.forEach((set) => {
      items.push({
        id: `p${promptIndex + 1}-${slugify(set.name, 'set')}`,
        prompt,
        refs: set.refs,
      });
    });
  });
  return items;
}

/**
 * Load a job file and expand it into a flat, validated item list.
 *
 * Accepted item containers, in order of preference: `images`, `items`, `matrix`.
 */
export function loadJob(jobPath, { settings, repairEncoding = false } = {}) {
  const absoluteJobPath = fromRoot(jobPath);
  if (!absoluteJobPath || !fs.existsSync(absoluteJobPath)) {
    throw new ConfigError(`Job file not found: ${jobPath}`);
  }

  const jobDir = path.dirname(absoluteJobPath);
  const raw = readJson(absoluteJobPath);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new ConfigError(`${jobPath} must contain a JSON object.`);
  }

  const name = slugify(raw.name ?? path.basename(absoluteJobPath, '.json'), 'job');
  const defaults = raw.defaults ?? {};
  const warnings = [];

  warnUnknownKeys(raw, KNOWN_TOP_LEVEL, 'job', warnings);
  warnUnknownKeys(defaults, KNOWN_DEFAULTS, 'defaults', warnings);

  // Report extra arrays (e.g. an editorial "shots" list) rather than silently
  // dropping them, so nobody assumes a field was applied when it was not.
  for (const [key, value] of Object.entries(raw)) {
    if (key.startsWith('_')) continue;
    if (!Array.isArray(value)) continue;
    if (['images', 'items', 'matrix', 'refs'].includes(key)) continue;
    warnings.push(`top-level "${key}" (${value.length} entries) is not a generation list and was ignored.`);
  }

  const containers = ['images', 'items', 'matrix'].filter((key) => raw[key] !== undefined);
  if (containers.length > 1) {
    warnings.push(`job defines ${containers.map((k) => `"${k}"`).join(' and ')}; using "${containers[0]}".`);
  }

  // A job-wide style directive (very common in editorial shot lists) is applied
  // to every prompt.
  const style = typeof raw.style === 'string' ? raw.style.trim() : '';
  const stylePosition = raw.stylePosition ?? defaults.stylePosition ?? 'prefix';
  if (stylePosition !== 'prefix' && stylePosition !== 'suffix') {
    throw new ConfigError(`stylePosition must be "prefix" or "suffix", got "${stylePosition}".`);
  }
  const applyStyle = (text) => {
    if (!style) return text;
    return stylePosition === 'suffix' ? `${text} ${style}` : `${style} ${text}`;
  };

  const refMap = buildRefMap(raw.refs, jobDir, warnings);
  if (defaults.refs !== undefined) {
    for (const ref of normalizeRefs(defaults.refs, refMap, jobDir, 'defaults.refs', warnings)) {
      if (!refMap.has(ref.name)) refMap.set(ref.name, ref.path);
    }
  }

  let rawItems;
  if (Array.isArray(raw.images) && raw.images.length > 0) {
    rawItems = raw.images;
  } else if (Array.isArray(raw.items) && raw.items.length > 0) {
    rawItems = raw.items;
  } else if (raw.matrix) {
    rawItems = expandMatrix(raw.matrix, defaults, refMap, jobDir, warnings);
  } else {
    throw new ConfigError(`${jobPath} must define a non-empty "images" array, "items" array, or "matrix" block.`);
  }

  const seen = new Set();
  let repairedCount = 0;
  const items = rawItems.map((item, index) => {
    if (!item || typeof item !== 'object') {
      throw new ConfigError(`Item ${index + 1} in ${jobPath} must be an object.`);
    }

    // `file` names the output; `id` is the state key. Either can be omitted.
    const fileStem = item.file ? path.basename(String(item.file), path.extname(String(item.file))) : null;
    const id = slugify(item.id ?? fileStem ?? `item-${index + 1}`, `item-${index + 1}`);
    if (seen.has(id)) throw new ConfigError(`Duplicate item id "${id}" in ${jobPath}.`);
    seen.add(id);

    const prompt = String(item.prompt ?? '').trim();
    if (!prompt) throw new ConfigError(`Item "${id}" has an empty prompt.`);

    const refs =
      item.refs === undefined
        ? normalizeRefs(defaults.refs, refMap, jobDir, 'defaults.refs', warnings)
        : normalizeRefs(item.refs, refMap, jobDir, `item "${id}"`, warnings);

    const mode = item.mode ?? defaults.mode ?? 'image';
    if (!MODES.has(mode)) {
      throw new ConfigError(`Item "${id}" uses unsupported mode "${mode}". Use one of: ${[...MODES].join(', ')}.`);
    }

    const outputs = item.outputs ?? defaults.outputs ?? null;
    if (outputs !== null && (!Number.isInteger(Number(outputs)) || Number(outputs) < 1)) {
      throw new ConfigError(`Item "${id}" has an invalid outputs value "${outputs}" (expected a positive integer).`);
    }

    const refMode = item.refMode ?? defaults.refMode ?? raw.refMode ?? null;
    if (refMode !== null && !REF_MODES.has(refMode)) {
      throw new ConfigError(
        `Item "${id}" uses unsupported refMode "${refMode}". Use one of: ${[...REF_MODES].join(', ')}.`,
      );
    }

    const prefix = item.promptPrefix ?? defaults.promptPrefix ?? '';
    const suffix = item.promptSuffix ?? defaults.promptSuffix ?? '';

    const styled = applyStyle(`${prefix}${prompt}${suffix}`);
    const shouldRepair = repairEncoding || raw.repairEncoding === true;
    const finalPrompt = shouldRepair ? repairMojibake(styled) : styled;
    if (shouldRepair && finalPrompt !== styled) repairedCount += 1;

    return {
      id,
      index,
      // The exact filename the job asked for, extension included.
      outputFile: item.file ? path.basename(String(item.file)) : null,
      outputName: fileStem ?? id,
      prompt: finalPrompt,
      refs,
      refNames: refs.map((ref) => ref.name),
      refPaths: refs.filter((ref) => ref.path).map((ref) => ref.path),
      mode,
      model: item.model ?? defaults.model ?? null,
      aspectRatio: item.aspectRatio ?? defaults.aspectRatio ?? null,
      outputs: outputs === null ? null : Number(outputs),
      timeoutMs: item.timeoutMs ?? defaults.timeoutMs ?? null,
      retries: item.retries ?? defaults.retries ?? null,
      refMode,
    };
  });

  const outputsDir = fromRoot(raw.outputsDir ?? path.join('output', name));

  // Flow refuses over-long prompts with the same generic "unusual activity"
  // message it uses for rate limiting, which makes the two easy to confuse.
  // Measured boundary: 2427 characters succeeded and 2510 was refused three
  // times, with identical references and in the same sessions. The prompt box
  // itself accepts 5000+, so the limit is server-side.
  const maxPromptChars = Number(
    defaults.maxPromptChars ?? settings?.generation?.maxPromptChars ?? 2420,
  );
  const overLimit = items.filter((item) => item.prompt.length > maxPromptChars);
  if (overLimit.length > 0) {
    const longest = Math.max(...overLimit.map((item) => item.prompt.length));
    warnings.push(
      `${overLimit.length} of ${items.length} prompts exceed ${maxPromptChars} characters ` +
        `(longest ${longest}) and will be refused by Flow as "unusual activity".` +
        (style ? ` The job-wide "style" contributes ${style.length} characters to every prompt.` : '') +
        ` Shorten: ${overLimit.slice(0, 5).map((item) => item.outputName).join(', ')}` +
        `${overLimit.length > 5 ? `, +${overLimit.length - 5} more` : ''}`,
    );
  }

  if (repairedCount > 0) {
    log.info(`Repaired mojibake in ${repairedCount} of ${items.length} prompts.`);
  } else {
    const broken = countMojibake(items.map((item) => item.prompt));
    if (broken > 0) {
      warnings.push(
        `${broken} of ${items.length} prompts contain mojibake (for example "\u00e2\u20ac\u201d" where "\u2014" was ` +
          'intended), which means the JSON was saved with the wrong text encoding. Re-run with ' +
          '--repair-encoding to send corrected text, or re-export the JSON as UTF-8.',
      );
    }
  }

  for (const warning of [...new Set(warnings)]) log.warn(warning);

  return {
    name,
    jobPath: absoluteJobPath,
    project: raw.project ?? settings?.generation?.project ?? null,
    projectUrl: raw.projectUrl ?? settings?.projectUrl ?? null,
    outputsDir,
    refMode: raw.refMode ?? defaults.refMode ?? null,
    style,
    stylePosition,
    refMap,
    defaults,
    items,
  };
}
