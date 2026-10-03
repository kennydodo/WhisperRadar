import zlib from 'node:zlib';

/**
 * Minimal PNG codec (8-bit, non-interlaced) plus a Lanczos resampler.
 *
 * The upscaler needs this for two reasons: the CPU fallback has to resize an
 * image without Pillow or any image library, and the engine refuses inputs that
 * carry an alpha channel, so images are flattened to RGB first.
 */

const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buffer) {
  let c = 0xffffffff;
  for (let i = 0; i < buffer.length; i += 1) c = CRC_TABLE[(c ^ buffer[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  const typeAndData = Buffer.concat([Buffer.from(type, 'latin1'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(typeAndData), 0);
  return Buffer.concat([length, typeAndData, crc]);
}

/** Channels per PNG colour type. */
const CHANNELS = { 0: 1, 2: 3, 4: 2, 6: 4 };

/**
 * @returns {{width:number, height:number, channels:number, data:Buffer}}
 *   `data` is tightly packed, `channels` is 3 (RGB) or 4 (RGBA).
 */
export function decodePng(buffer) {
  if (buffer.length < 8 || !buffer.subarray(0, 8).equals(SIGNATURE)) {
    throw new Error('Not a PNG file');
  }

  let offset = 8;
  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  let interlace = 0;
  const idat = [];

  while (offset + 8 <= buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString('latin1', offset + 4, offset + 8);
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    offset += 12 + length;

    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      interlace = data[12];
    } else if (type === 'IDAT') {
      idat.push(Buffer.from(data));
    } else if (type === 'IEND') {
      break;
    }
  }

  if (!width || !height) throw new Error('PNG is missing its IHDR chunk');
  if (bitDepth !== 8) throw new Error(`Unsupported PNG bit depth ${bitDepth} (only 8-bit is supported)`);
  if (interlace !== 0) throw new Error('Interlaced PNGs are not supported');
  const channels = CHANNELS[colorType];
  if (!channels) {
    throw new Error(
      colorType === 3
        ? 'Palette PNGs are not supported; re-encode as RGB first'
        : `Unsupported PNG colour type ${colorType}`,
    );
  }

  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const out = Buffer.alloc(stride * height);

  let position = 0;
  for (let y = 0; y < height; y += 1) {
    const filter = raw[position];
    position += 1;
    const line = raw.subarray(position, position + stride);
    position += stride;

    const rowStart = y * stride;
    const prevStart = rowStart - stride;

    for (let x = 0; x < stride; x += 1) {
      const left = x >= channels ? out[rowStart + x - channels] : 0;
      const up = y > 0 ? out[prevStart + x] : 0;
      const upLeft = y > 0 && x >= channels ? out[prevStart + x - channels] : 0;
      const value = line[x];

      let restored;
      switch (filter) {
        case 0:
          restored = value;
          break;
        case 1:
          restored = value + left;
          break;
        case 2:
          restored = value + up;
          break;
        case 3:
          restored = value + ((left + up) >> 1);
          break;
        case 4: {
          const p = left + up - upLeft;
          const pa = Math.abs(p - left);
          const pb = Math.abs(p - up);
          const pc = Math.abs(p - upLeft);
          restored = value + (pa <= pb && pa <= pc ? left : pb <= pc ? up : upLeft);
          break;
        }
        default:
          throw new Error(`Unsupported PNG filter type ${filter} on row ${y}`);
      }
      out[rowStart + x] = restored & 0xff;
    }
  }

  return { width, height, channels, data: out };
}

/** Drop an alpha channel. Returns the input unchanged when it is already RGB. */
export function toRgb(image) {
  if (image.channels === 3) return image;
  const { width, height, channels, data } = image;
  if (channels !== 4) {
    // Grey or grey+alpha: expand to RGB.
    const out = Buffer.alloc(width * height * 3);
    for (let i = 0; i < width * height; i += 1) {
      const grey = data[i * channels];
      out[i * 3] = grey;
      out[i * 3 + 1] = grey;
      out[i * 3 + 2] = grey;
    }
    return { width, height, channels: 3, data: out };
  }
  const out = Buffer.alloc(width * height * 3);
  for (let i = 0; i < width * height; i += 1) {
    out[i * 3] = data[i * 4];
    out[i * 3 + 1] = data[i * 4 + 1];
    out[i * 3 + 2] = data[i * 4 + 2];
  }
  return { width, height, channels: 3, data: out };
}

/**
 * Read only the IHDR, without inflating the image. Used to compare a candidate
 * result against the reference images, which can be 15 MB each.
 */
export function pngDimensions(buffer) {
  if (buffer.length < 24 || !buffer.subarray(0, 8).equals(SIGNATURE)) return null;
  if (buffer.toString('latin1', 12, 16) !== 'IHDR') return null;
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

export function encodePng({ width, height, channels, data }) {
  const colorType = channels === 4 ? 6 : 2;
  const stride = width * channels;

  // One filter byte (0 = None) per scanline.
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y += 1) {
    raw[y * (stride + 1)] = 0;
    data.copy(raw, y * (stride + 1) + 1, y * stride, y * stride + stride);
  }

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = colorType;
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0;

  return Buffer.concat([
    SIGNATURE,
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 6 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function lanczos(x, a = 3) {
  if (x === 0) return 1;
  if (x <= -a || x >= a) return 0;
  const px = Math.PI * x;
  return (a * Math.sin(px) * Math.sin(px / a)) / (px * px);
}

/** Precompute the source taps and normalised weights for one axis. */
function buildTaps(sourceLength, targetLength) {
  const scale = sourceLength / targetLength;
  const support = scale * 3;
  const taps = [];

  for (let i = 0; i < targetLength; i += 1) {
    const center = (i + 0.5) * scale - 0.5;
    const first = Math.max(0, Math.ceil(center - support));
    const last = Math.min(sourceLength - 1, Math.floor(center + support));

    const indices = [];
    const weights = [];
    let total = 0;
    for (let s = first; s <= last; s += 1) {
      const weight = lanczos((s - center) / scale);
      if (weight === 0) continue;
      indices.push(s);
      weights.push(weight);
      total += weight;
    }
    if (total === 0) {
      indices.push(Math.min(sourceLength - 1, Math.max(0, Math.round(center))));
      weights.push(1);
      total = 1;
    }
    taps.push({ indices, weights: weights.map((w) => w / total) });
  }
  return taps;
}

/** Separable Lanczos-3 resample. Always returns RGB. */
export function resizeLanczos(image, targetWidth, targetHeight) {
  const source = toRgb(image);
  const { width: sw, height: sh, data } = source;

  const horizontal = Buffer.alloc(targetWidth * sh * 3);
  const xTaps = buildTaps(sw, targetWidth);

  for (let y = 0; y < sh; y += 1) {
    const rowOffset = y * sw * 3;
    const outOffset = y * targetWidth * 3;
    for (let x = 0; x < targetWidth; x += 1) {
      const { indices, weights } = xTaps[x];
      let r = 0;
      let g = 0;
      let b = 0;
      for (let t = 0; t < indices.length; t += 1) {
        const p = rowOffset + indices[t] * 3;
        const w = weights[t];
        r += data[p] * w;
        g += data[p + 1] * w;
        b += data[p + 2] * w;
      }
      const o = outOffset + x * 3;
      horizontal[o] = Math.max(0, Math.min(255, Math.round(r)));
      horizontal[o + 1] = Math.max(0, Math.min(255, Math.round(g)));
      horizontal[o + 2] = Math.max(0, Math.min(255, Math.round(b)));
    }
  }

  const out = Buffer.alloc(targetWidth * targetHeight * 3);
  const yTaps = buildTaps(sh, targetHeight);

  for (let y = 0; y < targetHeight; y += 1) {
    const { indices, weights } = yTaps[y];
    const outOffset = y * targetWidth * 3;
    for (let x = 0; x < targetWidth; x += 1) {
      let r = 0;
      let g = 0;
      let b = 0;
      for (let t = 0; t < indices.length; t += 1) {
        const p = indices[t] * targetWidth * 3 + x * 3;
        const w = weights[t];
        r += horizontal[p] * w;
        g += horizontal[p + 1] * w;
        b += horizontal[p + 2] * w;
      }
      const o = outOffset + x * 3;
      out[o] = Math.max(0, Math.min(255, Math.round(r)));
      out[o + 1] = Math.max(0, Math.min(255, Math.round(g)));
      out[o + 2] = Math.max(0, Math.min(255, Math.round(b)));
    }
  }

  return { width: targetWidth, height: targetHeight, channels: 3, data: out };
}

/**
 * Mean absolute deviation between a source and a candidate, both reduced to a
 * 64x64 grid. Used to detect corrupt GPU tiles, which look nothing like the input.
 */
export function meanAbsoluteDeviation(source, candidate, threshold = 60) {
  const a = resizeLanczos(source, 64, 64).data;
  const b = resizeLanczos(candidate, 64, 64).data;
  let total = 0;
  for (let i = 0; i < a.length; i += 1) total += Math.abs(a[i] - b[i]);
  const mad = total / a.length;
  return { mad, ok: mad <= threshold };
}
