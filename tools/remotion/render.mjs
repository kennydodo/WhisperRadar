// node render.mjs --props props.json --out clip.mp4 [--scale 1|0.5]
// Stop: SIGTERM/SIGINT cancels through makeCancelSignal and exits 130 leaving no output file.
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition, makeCancelSignal} from '@remotion/renderer';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const here = path.dirname(fileURLToPath(import.meta.url));
const props = JSON.parse(fs.readFileSync(arg('--props'), 'utf8'));
const out = arg('--out');
const scale = Number(arg('--scale', '1'));
const browserExecutable = process.env.WR_BROWSER || null;
const {cancelSignal, cancel} = makeCancelSignal();
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => cancel());
try {
  const serveUrl = await bundle({entryPoint: path.join(here, 'src', 'index.jsx')});
  const composition = await selectComposition({serveUrl, id: 'scene', inputProps: props, browserExecutable});
  await renderMedia({composition, serveUrl, codec: 'h264', outputLocation: out, inputProps: props,
    concurrency: 1, crf: 20, scale, cancelSignal, browserExecutable});
} catch (e) {
  if (/cancel/i.test(String(e))) { try { fs.unlinkSync(out); } catch {} process.exit(130); }
  console.error(String(e && e.stack || e));
  process.exit(1);
}
