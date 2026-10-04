import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { SelectorSet } from '../src/flow/selectors.js';
import { log } from '../src/lib/log.js';
import { ROOT } from '../src/lib/paths.js';

const REQUIRED_KEYS = ['promptBox', 'generateButton', 'assetTile', 'agentToggle', 'settingsTriggerButton'];
const RECOMMENDED_KEYS = [
  'consentDismiss',
  'signedIn',
  'signedOut',
  'newProjectButton',
  'addIngredientsButton',
  'fileInput',
  'promptReferenceChip',
  'clearPromptButton',
  'assetMenuButton',
  'downloadMenuItem',
];
/**
 * Only present while a popover/dialog is open, or only after a reference has
 * been attached, so "no candidate matched" on a freshly loaded project is
 * expected rather than a problem.
 */
const CONTEXTUAL_KEYS = [
  'settingsOverlay',
  'modeGroup',
  'modeImageOption',
  'modeVideoOption',
  'aspectRatioGroup',
  'outputCountGroup',
  'modelFamilyButton',
  'modelMenu',
  'addMediaOption',
  'assetPickerDialog',
  'assetPickerSearch',
  'assetPickerItem',
  'addToPromptButton',
  'promptIngredientBar',
  'generatingIndicator',
  'errorBanner',
];

function findChromeChannel() {
  const candidates = [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Google/Chrome/Application/chrome.exe') : null,
    '/usr/bin/google-chrome',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((candidate) => fs.existsSync(candidate)) ?? null;
}

function check(label, ok, detail = '', { warnOnly = false } = {}) {
  const prefix = ok ? '[PASS]' : warnOnly ? '[WARN]' : '[FAIL]';
  log.raw(`${prefix} ${label}${detail ? ` — ${detail}` : ''}`);
  return ok || warnOnly;
}

export async function doctorCommand({ flags, context }) {
  const { settings, selectors } = context;
  const live = flags.live === true;
  let failures = 0;

  log.heading('Environment');
  const nodeMajor = Number(process.versions.node.split('.')[0]);
  if (!check(`Node.js >= 20 (found ${process.versions.node})`, nodeMajor >= 20)) failures += 1;

  const executable = chromium.executablePath();
  const hasBundled = Boolean(executable) && fs.existsSync(executable);
  check(
    'Playwright bundled Chromium installed',
    hasBundled,
    hasBundled ? '' : 'run: npx playwright install chromium (only needed if browser.channel is unset)',
    { warnOnly: true },
  );

  const chrome = findChromeChannel();
  const channel = settings.browser.channel ?? null;
  check(
    `browser.channel = "${channel ?? 'bundled Chromium'}"`,
    channel ? Boolean(chrome) : true,
    channel ? (chrome ?? 'Chrome not found in the standard install locations') : 'using bundled Chromium',
    { warnOnly: true },
  );

  log.heading('Configuration');
  for (const key of REQUIRED_KEYS) {
    const candidates = selectors[key];
    if (!check(`selectors.${key}`, Array.isArray(candidates) && candidates.length > 0, `${candidates?.length ?? 0} candidate(s)`)) {
      failures += 1;
    }
  }
  for (const key of RECOMMENDED_KEYS) {
    const candidates = selectors[key];
    check(`selectors.${key}`, Array.isArray(candidates) && candidates.length > 0, `${candidates?.length ?? 0} candidate(s)`, {
      warnOnly: true,
    });
  }

  log.heading('Directories');
  for (const [name, dir] of Object.entries(settings.dirs)) {
    if (name === 'root' || name === 'config') continue;
    try {
      fs.mkdirSync(dir, { recursive: true });
      fs.accessSync(dir, fs.constants.W_OK);
      check(`${name}: ${path.relative(ROOT, dir) || '.'}`, true);
    } catch (error) {
      check(`${name}: ${dir}`, false, error.message);
      failures += 1;
    }
  }

  log.heading('Reference images');
  const refs = fs.existsSync(settings.dirs.refsDir)
    ? fs.readdirSync(settings.dirs.refsDir).filter((file) => !file.startsWith('.'))
    : [];
  check(`${refs.length} file(s) in refs/`, refs.length > 0, refs.slice(0, 6).join(', '), { warnOnly: true });

  if (live) {
    log.heading('Live selector resolution');
    const { context: browserContext, page } = await launchSession(settings);
    const driver = new FlowDriver({ page, context: browserContext, selectors: context.selectors, settings });
    const selectorSet = new SelectorSet(selectors);
    try {
      const projectUrl = typeof flags['project-url'] === 'string' ? flags['project-url'] : settings.projectUrl;
      if (projectUrl) {
        await driver.openProject(projectUrl);
      } else {
        await driver.goto();
        // The landing page redirects into the most recent project when signed in.
        const inProject = await driver
          .waitForPromptBox({ timeout: 30000 })
          .then(() => true)
          .catch(() => false);
        if (!inProject) {
          log.warn(
            'Not inside a Flow project, so prompt-box selectors cannot resolve. ' +
              'Pass --project-url <url>, or set projectUrl in config/settings.json.',
          );
        }
      }

      const state = await driver.looksSignedIn();
      check('Signed in to Flow', state === 'in', `state=${state}`, { warnOnly: state === 'unknown' });

      const keys = selectorSet.keys();
      for (const key of keys) {
        const found = await selectorSet.find(page, key, { timeout: 1500, required: false, requireVisible: false });
        if (found) {
          log.raw(`[PASS] ${key.padEnd(24)} -> ${found.selector}`);
        } else if (CONTEXTUAL_KEYS.includes(key)) {
          log.raw(`[SKIP] ${key.padEnd(24)} -> only present in an open popover/dialog`);
        } else {
          const isRequired = REQUIRED_KEYS.includes(key);
          log.raw(`${isRequired ? '[FAIL]' : '[WARN]'} ${key.padEnd(24)} -> no candidate matched`);
          if (isRequired) failures += 1;
        }
      }
    } finally {
      await closeSession(browserContext);
    }
  } else {
    log.info('Skipped live checks. Re-run with `npm run doctor -- --live` while signed in.');
  }

  log.heading('Result');
  if (failures === 0) {
    log.ok('No blocking problems found.');
    return 0;
  }
  log.error(`${failures} blocking problem(s) found.`);
  return 1;
}
