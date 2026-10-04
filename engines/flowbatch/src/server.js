import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

import { ROOT, fromRoot } from './lib/paths.js';
import { readJson, writeJson } from './lib/json.js';
import { log } from './lib/log.js';
import { deepMerge, loadSettings } from './lib/config.js';
import { describeUpscaler, normalizeTier, saveUpscaleSettings } from './upscale/index.js';

const CONFIG_DIR = path.join(ROOT, 'config');
const LOCAL_SETTINGS = path.join(CONFIG_DIR, 'settings.local.json');

/**
 * Chrome records the signed-in account in the profile's own Preferences file, so
 * the settings page can name the account rather than just a folder.
 */
function readProfileAccount(profileDir) {
  for (const sub of ['Default', 'Profile 1', 'Profile 2']) {
    const file = path.join(profileDir, sub, 'Preferences');
    if (!fs.existsSync(file)) continue;
    try {
      const prefs = readJson(file, { required: false });
      const email = prefs?.account_info?.[0]?.email;
      if (email) return email;
    } catch {
      /* unreadable profile */
    }
  }
  return null;
}

function listProfiles(currentName) {
  const found = [];
  for (const name of fs.readdirSync(ROOT)) {
    if (name !== 'profile' && !name.startsWith('profile-')) continue;
    const dir = path.join(ROOT, name);
    let stat;
    try {
      stat = fs.statSync(dir);
    } catch {
      continue;
    }
    if (!stat.isDirectory()) continue;
    found.push({
      name,
      account: readProfileAccount(dir),
      modified: stat.mtime.toISOString().slice(0, 16).replace('T', ' '),
      current: name === currentName,
    });
  }
  return found.sort((a, b) => a.name.localeCompare(b.name));
}

function settingsView() {
  const settings = loadSettings(CONFIG_DIR);
  const profileDir = settings.paths?.profileDir ?? 'profile';
  return {
    profileDir,
    agent: settings.generation?.agent === true,
    cooldownSeconds: settings.generation?.cooldownSeconds ?? 180,
    maxCooldowns: settings.generation?.maxCooldowns ?? 0,
    maxPromptChars: settings.generation?.maxPromptChars ?? 2420,
    profiles: listProfiles(profileDir),
    localFile: path.relative(ROOT, LOCAL_SETTINGS),
  };
}

/** Writes only the local override file, so the tracked config stays clean. */
function saveSettingsPatch(patch) {
  const current = readJson(LOCAL_SETTINGS, { required: false }) ?? {};
  writeJson(LOCAL_SETTINGS, deepMerge(current, patch));
  return settingsView();
}

const UI_DIR = path.join(ROOT, 'ui');
const CLI = path.join(ROOT, 'src', 'cli.js');
const MAX_BUFFER = 3000;
const DIALOG_TIMEOUT_MS = 5 * 60 * 1000;

/** Connected SSE clients. */
const clients = new Set();
/** Recent output, replayed to a client that connects mid-run. */
const history = [];

let child = null;
let currentRun = null;

// ---------------------------------------------------------------- broadcasting

function broadcast(message) {
  if (message.type === 'log') {
    history.push(message);
    if (history.length > MAX_BUFFER) history.splice(0, history.length - MAX_BUFFER);
  }
  const payload = `data: ${JSON.stringify(message)}\n\n`;
  for (const client of clients) {
    client.write(payload);
  }
}

function runStatus() {
  return {
    running: Boolean(child),
    pid: child?.pid ?? null,
    job: currentRun?.job ?? null,
    output: currentRun?.output ?? null,
    startedAt: currentRun?.startedAt ?? null,
  };
}

function logLine(stream, text) {
  broadcast({ type: 'log', stream, text, at: Date.now() });
}

// ------------------------------------------------------------------ processes

/** Kill the whole process tree, otherwise Playwright's Chrome survives. */
function killTree(pid) {
  if (!pid) return;
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true }).on('error', () => {});
  } else {
    try {
      process.kill(-pid, 'SIGKILL');
    } catch {
      try {
        process.kill(pid, 'SIGKILL');
      } catch {
        /* already gone */
      }
    }
  }
}

function startBatch({ jobPath, outputDir, dryRun, only, projectUrl }) {
  if (child) throw new Error('A batch is already running. Stop it first.');

  const job = fromRoot(jobPath);
  if (!job || !fs.existsSync(job)) throw new Error(`Job file not found: ${jobPath}`);

  const args = [CLI, 'generate', '--job', job, '--no-color'];
  if (outputDir) args.push('--output', outputDir);
  if (projectUrl) args.push('--project-url', projectUrl);
  if (only) args.push('--only', only);
  if (dryRun) args.push('--dry-run');

  logLine('meta', `$ node src/cli.js ${args.slice(1).join(' ')}`);

  child = spawn(process.execPath, args, {
    cwd: ROOT,
    windowsHide: true,
    env: { ...process.env, FORCE_COLOR: '0' },
  });
  currentRun = { job, output: outputDir ?? null, startedAt: Date.now() };
  broadcast({ type: 'status', ...runStatus() });

  const pipe = (stream) => {
    let pending = '';
    stream.setEncoding('utf8');
    stream.on('data', (chunk) => {
      pending += chunk;
      const lines = pending.split(/\r?\n/);
      pending = lines.pop() ?? '';
      for (const line of lines) logLine(stream === child.stdout ? 'stdout' : 'stderr', line);
    });
    stream.on('end', () => {
      if (pending.trim()) logLine(stream === child.stdout ? 'stdout' : 'stderr', pending);
    });
  };
  pipe(child.stdout);
  pipe(child.stderr);

  child.on('error', (error) => {
    logLine('stderr', `Failed to launch the batch: ${error.message}`);
  });

  child.on('exit', (code, signal) => {
    logLine('meta', `Process finished (exit ${code ?? 'null'}${signal ? `, signal ${signal}` : ''}).`);
    child = null;
    currentRun = null;
    broadcast({ type: 'status', ...runStatus() });
  });

  return runStatus();
}

function stopBatch() {
  if (!child) return { stopped: false, ...runStatus() };
  const pid = child.pid;
  logLine('meta', `Stopping process ${pid}…`);
  killTree(pid);
  return { stopped: true, ...runStatus() };
}

// ------------------------------------------------------------ native dialogs

/**
 * The browser cannot hand a real filesystem path to the server, so the picker
 * runs on the desktop via PowerShell and returns an actual path.
 */
function pickWithPowerShell(script) {
  return new Promise((resolve, reject) => {
    const ps = spawn('powershell.exe', ['-NoProfile', '-STA', '-Command', script], {
      windowsHide: true,
    });

    let out = '';
    let err = '';
    ps.stdout.setEncoding('utf8');
    ps.stderr.setEncoding('utf8');
    ps.stdout.on('data', (chunk) => {
      out += chunk;
    });
    ps.stderr.on('data', (chunk) => {
      err += chunk;
    });

    const timer = setTimeout(() => {
      killTree(ps.pid);
      reject(new Error('The file dialog timed out.'));
    }, DIALOG_TIMEOUT_MS);

    ps.on('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    ps.on('exit', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(err.trim() || `PowerShell exited with code ${code}.`));
        return;
      }
      const picked = out.trim();
      resolve(picked.length > 0 ? picked : null);
    });
  });
}

function pickFolder(initialDir) {
  const start = initialDir && fs.existsSync(initialDir) ? initialDir : ROOT;
  const script = [
    'Add-Type -AssemblyName System.Windows.Forms',
    '$d = New-Object System.Windows.Forms.FolderBrowserDialog',
    "$d.Description = 'Choose where generated images are saved'",
    '$d.ShowNewFolderButton = $true',
    `$d.SelectedPath = '${start.replace(/'/g, "''")}'`,
    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }",
  ].join('; ');
  return pickWithPowerShell(script);
}

function pickJsonFile(initialDir) {
  const start = initialDir && fs.existsSync(initialDir) ? initialDir : path.join(ROOT, 'config');
  const script = [
    'Add-Type -AssemblyName System.Windows.Forms',
    '$f = New-Object System.Windows.Forms.OpenFileDialog',
    "$f.Title = 'Choose a job JSON file'",
    "$f.Filter = 'Job files (*.json)|*.json|All files (*.*)|*.*'",
    `$f.InitialDirectory = '${start.replace(/'/g, "''")}'`,
    "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($f.FileName) }",
  ].join('; ');
  return pickWithPowerShell(script);
}

// -------------------------------------------------------------------- routing

function listJobs() {
  const configDir = path.join(ROOT, 'config');
  if (!fs.existsSync(configDir)) return [];
  return fs
    .readdirSync(configDir)
    .filter((file) => file.endsWith('.json'))
    .filter((file) => !['settings.json', 'selectors.json', 'settings.local.json', 'selectors.local.json'].includes(file))
    .sort()
    .map((file) => {
      const full = path.join(configDir, file);
      let label = file;
      let items = null;
      try {
        const parsed = readJson(full);
        const count = Array.isArray(parsed.images)
          ? parsed.images.length
          : Array.isArray(parsed.items)
            ? parsed.items.length
            : null;
        items = count;
        label = `${parsed.name ?? file}${count === null ? '' : ` (${count} item${count === 1 ? '' : 's'})`}`;
      } catch {
        label = `${file} (unreadable)`;
      }
      return { file, path: full, label, items };
    });
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 1_000_000) reject(new Error('Request body too large.'));
    });
    req.on('end', () => {
      if (!body) return resolve({});
      try {
        resolve(JSON.parse(body));
      } catch (error) {
        reject(new Error(`Invalid JSON body: ${error.message}`));
      }
    });
    req.on('error', reject);
  });
}

async function handle(req, res) {
  const url = new URL(req.url, 'http://localhost');
  const route = `${req.method} ${url.pathname}`;

  if (route === 'GET /' || route === 'GET /index.html') {
    const file = path.join(UI_DIR, 'index.html');
    if (!fs.existsSync(file)) {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end('ui/index.html is missing.');
      return;
    }
    const html = fs.readFileSync(file);
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(html);
    return;
  }

  if (route === 'GET /api/status') {
    sendJson(res, 200, runStatus());
    return;
  }

  if (route === 'GET /api/jobs') {
    sendJson(res, 200, { jobs: listJobs(), root: ROOT });
    return;
  }

  if (route === 'GET /api/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write(`data: ${JSON.stringify({ type: 'status', ...runStatus() })}\n\n`);
    for (const entry of history) {
      res.write(`data: ${JSON.stringify(entry)}\n\n`);
    }
    clients.add(res);
    const keepAlive = setInterval(() => res.write(': ping\n\n'), 15000);
    req.on('close', () => {
      clearInterval(keepAlive);
      clients.delete(res);
    });
    return;
  }

  if (route === 'POST /api/start') {
    try {
      const body = await readBody(req);
      const status = startBatch({
        jobPath: body.job,
        outputDir: body.output,
        dryRun: Boolean(body.dryRun),
        only: body.only,
        projectUrl: body.projectUrl,
      });
      sendJson(res, 200, status);
    } catch (error) {
      logLine('stderr', `Start rejected: ${error.message}`);
      sendJson(res, 400, { error: String(error.message) });
    }
    return;
  }

  if (route === 'GET /api/settings') {
    sendJson(res, 200, settingsView());
    return;
  }

  if (route === 'POST /api/settings') {
    try {
      const body = await readBody(req);
      const patch = {};

      if (typeof body.profileDir === 'string' && body.profileDir.trim()) {
        const name = body.profileDir.trim();
        // Only allow profile folders that exist inside the project, so the
        // profile path can never be pointed somewhere unexpected.
        if (!/^profile(-[\w.-]+)?$/.test(name) || !fs.existsSync(path.join(ROOT, name))) {
          throw new Error(`Unknown profile "${name}". Pick one from the list.`);
        }
        patch.paths = { profileDir: name };
      }
      if (body.agent !== undefined) {
        patch.generation = { ...(patch.generation ?? {}), agent: Boolean(body.agent) };
      }
      if (body.cooldownSeconds !== undefined) {
        const value = Number(body.cooldownSeconds);
        if (!Number.isFinite(value) || value < 0) throw new Error('Cooldown must be a positive number of seconds.');
        patch.generation = { ...(patch.generation ?? {}), cooldownSeconds: value };
      }

      const saved = saveSettingsPatch(patch);
      logLine(
        'meta',
        `Settings saved to ${saved.localFile}: profile "${saved.profileDir}"` +
          `${saved.agent ? ', agent mode ON' : ''}, cooldown ${saved.cooldownSeconds}s.`,
      );
      sendJson(res, 200, saved);
    } catch (error) {
      sendJson(res, 400, { error: String(error.message) });
    }
    return;
  }

  if (route === 'GET /api/upscale') {
    sendJson(res, 200, describeUpscaler());
    return;
  }

  if (route === 'POST /api/upscale') {
    try {
      const body = await readBody(req);
      const patch = {};
      if (body.tier !== undefined) patch.tier = normalizeTier(body.tier);
      if (body.model !== undefined) patch.model = String(body.model);
      if (body.fit !== undefined) patch.fit = String(body.fit);
      if (body.supersample !== undefined) patch.supersample = Boolean(body.supersample);
      const saved = saveUpscaleSettings(patch);
      logLine('meta', `Upscale tier set to ${saved.tier} (${saved.model}, fit=${saved.fit}).`);
      sendJson(res, 200, describeUpscaler());
    } catch (error) {
      sendJson(res, 400, { error: String(error.message) });
    }
    return;
  }

  if (route === 'POST /api/stop') {
    sendJson(res, 200, stopBatch());
    return;
  }

  if (route === 'POST /api/clear') {
    history.length = 0;
    broadcast({ type: 'cleared' });
    sendJson(res, 200, { cleared: true });
    return;
  }

  if (route === 'POST /api/pick-folder' || route === 'POST /api/pick-file') {
    try {
      const body = await readBody(req);
      const picked =
        url.pathname === '/api/pick-folder'
          ? await pickFolder(body.initialDir)
          : await pickJsonFile(body.initialDir);
      sendJson(res, 200, { path: picked });
    } catch (error) {
      sendJson(res, 500, { error: String(error.message) });
    }
    return;
  }

  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('Not found');
}

/** Open a URL in the default browser, detached from this process. */
function openBrowser(url) {
  const [command, args] =
    process.platform === 'win32'
      ? ['cmd', ['/c', 'start', '', url]]
      : process.platform === 'darwin'
        ? ['open', [url]]
        : ['xdg-open', [url]];
  try {
    spawn(command, args, { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  } catch {
    log.warn(`Could not open a browser automatically; visit ${url}`);
  }
}

export function startServer({ port = 8787, host = '127.0.0.1', open = false } = {}) {
  const server = http.createServer((req, res) => {
    handle(req, res).catch((error) => {
      log.error(String(error?.stack ?? error));
      if (!res.headersSent) sendJson(res, 500, { error: String(error?.message ?? error) });
    });
  });

  server.on('error', (error) => {
    if (error.code === 'EADDRINUSE') {
      log.error(`Port ${port} is already in use. Start the UI with a different --port.`);
    } else {
      log.error(error.message);
    }
    process.exitCode = 1;
  });

  const shutdown = () => {
    if (child) killTree(child.pid);
    server.close();
  };
  process.on('SIGINT', () => {
    log.info('Shutting down…');
    shutdown();
    process.exit(0);
  });
  process.on('SIGTERM', shutdown);

  server.listen(port, host, () => {
    const url = `http://${host}:${port}`;
    log.ok(`FlowBatch UI running at ${url}`);
    log.info('Leave this process running. Press Ctrl+C to stop.');
    if (open) openBrowser(url);
  });

  return server;
}

export { listJobs, runStatus };
