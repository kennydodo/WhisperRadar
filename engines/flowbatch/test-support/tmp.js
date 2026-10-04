import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export function makeTempDir(prefix = 'flow-test-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

export function removeDir(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
}

export function writeFile(dir, name, contents) {
  const filePath = path.join(dir, name);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, contents);
  return filePath;
}
