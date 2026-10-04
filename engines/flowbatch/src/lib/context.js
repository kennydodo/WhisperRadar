import path from 'node:path';

import { loadSelectors, loadSettings, prepareRuntimeDirs } from './config.js';
import { ROOT, fromRoot } from './paths.js';

/**
 * Load config, apply CLI overrides, and make sure runtime directories exist.
 */
export function buildContext(flags = {}) {
  const configDir = path.join(ROOT, 'config');
  const settings = loadSettings(configDir);
  const selectors = loadSelectors(configDir);

  if (flags.headless !== undefined) settings.browser.headless = Boolean(flags.headless);
  if (typeof flags.channel === 'string') settings.browser.channel = flags.channel;
  if (flags.slowmo !== undefined) settings.browser.slowMo = Number(flags.slowmo);
  if (typeof flags.url === 'string') settings.flowUrl = flags.url;
  // Point at a different browser profile. Google's abuse signal is attached to
  // the browser profile, so a flagged one stays flagged; rotating to a fresh
  // profile and signing in again is the way out.
  if (typeof flags.profile === 'string' && flags.profile.trim()) {
    settings.paths = { ...(settings.paths ?? {}), profileDir: flags.profile.trim() };
    settings.dirs.profileDir = fromRoot(flags.profile.trim());
  }

  prepareRuntimeDirs(settings);

  return { settings, selectors, dirs: settings.dirs, configDir };
}
