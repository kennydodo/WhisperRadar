/**
 * Flow does not let automation name a generated image: the asset library lists
 * it under a short caption Flow writes itself ("Two apes sitting hunched"),
 * truncated with an ellipsis when long ("Four stick figures standing toge…").
 * Uploaded files keep their file name. So a generated reference cannot be found
 * by its ref name; instead the refs stage records the caption Flow gave each
 * one, and later stages look the image up by that caption.
 *
 * Pure helpers, no browser: shared by the driver, the runner and prepare.
 */

const ELLIPSIS = /(?:…|\.{3})\s*$/;
const MIN_PREFIX = 12;

/** Whitespace-collapsed title, trailing "..." / "…" removed. */
export function normalizeTitle(title) {
  return String(title ?? '')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(ELLIPSIS, '')
    .trim();
}

/** True when the title was cut by an ellipsis. */
export function isTruncated(title) {
  return ELLIPSIS.test(String(title ?? '').trim());
}

/** The text to type in the picker's search box: the title without its ellipsis. */
export function titleSearchTerm(title) {
  return normalizeTitle(title).slice(0, 60);
}

/**
 * Does a picker item's title denote the wanted caption? Exact after
 * normalisation, or - when either side was truncated by Flow - one is a
 * prefix of the other (at least MIN_PREFIX characters, so "Character" never
 * matches everything). Case-insensitive.
 */
export function titlesMatch(itemTitle, wanted) {
  const a = normalizeTitle(itemTitle).toLowerCase();
  const b = normalizeTitle(wanted).toLowerCase();
  if (!a || !b) return false;
  if (a === b) return true;
  const truncated = isTruncated(itemTitle) || isTruncated(wanted);
  if (!truncated) return false;
  const shorter = a.length <= b.length ? a : b;
  const longer = shorter === a ? b : a;
  return shorter.length >= MIN_PREFIX && longer.startsWith(shorter);
}

function counts(titles) {
  const map = new Map();
  for (const title of titles ?? []) {
    const key = normalizeTitle(title);
    if (key) map.set(key, (map.get(key) ?? 0) + 1);
  }
  return map;
}

/** Titles present in `after` more often than in `before` (multiset difference). */
export function newTitles(before, after) {
  const was = counts(before);
  const now = counts(after);
  const added = [];
  for (const [title, n] of now) {
    const extra = n - (was.get(title) ?? 0);
    for (let i = 0; i < extra; i += 1) added.push(title);
  }
  return added;
}

/**
 * The caption of the ONE image a generation added, or null when it cannot be
 * told: nothing new, several new, or the new caption is shared with another
 * asset (a lookup by it could attach the wrong image).
 */
export function uniqueNewTitle(before, after) {
  const added = newTitles(before, after);
  if (added.length !== 1) return null;
  const title = added[0];
  const sameCaption = (after ?? []).filter((t) => titlesMatch(t, title)).length;
  return sameCaption === 1 ? title : null;
}
