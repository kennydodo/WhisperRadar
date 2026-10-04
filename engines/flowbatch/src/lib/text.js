/**
 * Text written by a tool that encoded UTF-8 bytes as Windows-1252 shows up as
 * mojibake: an em dash (—) becomes "â€”". Prompts affected by this get sent to
 * Flow with corrupted characters, so they are detected and can be repaired.
 */

const CP1252_TO_BYTE = {
  0x20ac: 0x80,
  0x201a: 0x82,
  0x0192: 0x83,
  0x201e: 0x84,
  0x2026: 0x85,
  0x2020: 0x86,
  0x2021: 0x87,
  0x02c6: 0x88,
  0x2030: 0x89,
  0x0160: 0x8a,
  0x2039: 0x8b,
  0x0152: 0x8c,
  0x017d: 0x8e,
  0x2018: 0x91,
  0x2019: 0x92,
  0x201c: 0x93,
  0x201d: 0x94,
  0x2022: 0x95,
  0x2013: 0x96,
  0x2014: 0x97,
  0x02dc: 0x98,
  0x2122: 0x99,
  0x0161: 0x9a,
  0x203a: 0x9b,
  0x0153: 0x9c,
  0x017e: 0x9e,
  0x0178: 0x9f,
};

// Sequences that only appear when UTF-8 bytes were decoded as CP1252:
//   em dash  E2 80 94 -> "â€”"   (U+00E2 U+20AC U+201D)
//   e-acute  C3 A9    -> "Ã©"    (U+00C3 U+00A9)
//   nbsp     C2 A0    -> "Â "    (U+00C2 U+00A0)
// The latin1 spellings (U+0080..U+009F) are matched too, since some tools
// produce those instead of the CP1252 punctuation.
const MOJIBAKE_PATTERN = /(?:\u00e2[\u20ac\u0080]|\u00c3[\u0080-\u00bf]|\u00c2[\u0080-\u00bf]|\u00f0\u0178)/;

export function looksLikeMojibake(text) {
  return typeof text === 'string' && MOJIBAKE_PATTERN.test(text);
}

/**
 * Reverse the damage: map every character back to the CP1252 byte it came from,
 * then decode those bytes as UTF-8. Returns the input untouched when it cannot
 * be represented, so a false positive can never make things worse.
 */
export function repairMojibake(text) {
  if (typeof text !== 'string' || text.length === 0) return text;

  const bytes = [];
  for (const char of text) {
    const code = char.codePointAt(0);
    if (code <= 0xff) {
      bytes.push(code);
      continue;
    }
    const mapped = CP1252_TO_BYTE[code];
    if (mapped === undefined) return text;
    bytes.push(mapped);
  }

  const decoded = Buffer.from(bytes).toString('utf8');
  if (decoded.includes('\uFFFD')) return text;
  return decoded;
}

/** Count of prompts that look mojibake-corrupted, for a single warning. */
export function countMojibake(values) {
  return values.filter((value) => looksLikeMojibake(value)).length;
}
