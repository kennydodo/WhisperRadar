import fs from 'node:fs';

/**
 * Flow downloads stills as JPEG regardless of the prompt, so the extension is
 * sniffed from the file's magic bytes instead of being assumed.
 */
const MIME_BY_EXTENSION = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
};

export function mimeForExtension(extension) {
  return MIME_BY_EXTENSION[String(extension).toLowerCase()] ?? 'application/octet-stream';
}

export function sniffImageExtension(filePath) {
  let header;
  try {
    const fd = fs.openSync(filePath, 'r');
    header = Buffer.alloc(16);
    const read = fs.readSync(fd, header, 0, 16, 0);
    fs.closeSync(fd);
    header = header.subarray(0, read);
  } catch {
    return '.bin';
  }

  if (header.length >= 8 && header.subarray(0, 8).toString('hex') === '89504e470d0a1a0a') return '.png';
  if (header.length >= 3 && header[0] === 0xff && header[1] === 0xd8 && header[2] === 0xff) return '.jpg';
  if (header.length >= 12 && header.subarray(0, 4).toString() === 'RIFF' && header.subarray(8, 12).toString() === 'WEBP') {
    return '.webp';
  }
  if (header.length >= 3 && header.subarray(0, 3).toString() === 'GIF') return '.gif';
  return '.bin';
}
