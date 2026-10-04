import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

import { sleep, timestampSlug } from '../lib/time.js';
import { ensureParent, slugify } from '../lib/paths.js';
import { mimeForExtension, sniffImageExtension } from '../lib/image.js';
import { log } from '../lib/log.js';
import { GenerationError, TimeoutError } from '../lib/errors.js';
import { SelectorSet } from './selectors.js';

const REFERENCE_CONFIRM_MS = 20000;

/**
 * Normalise a model label for comparison: drop the leading emoji and any icon
 * text, collapse whitespace, and lowercase.
 */
function normalizeModelName(value) {
  return String(value ?? '')
    .replace(/[^\p{L}\p{N} ]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

/**
 * Finished results are served from Flow's asset host; grid placeholders use
 * flow.google.com/asb/…. Only the finished host counts as a result, whatever
 * the tile's buttons happen to say.
 */
function isFinalResultUrl(src) {
  try {
    const url = new URL(src);
    return url.hostname === 'flow-content.google' && /^\/image\//.test(url.pathname);
  } catch {
    return false;
  }
}

/**
 * Recovery only. A freshly rendered tile exposes the flow-content.google CDN
 * URL, but a RELOADED project gallery serves every tile through a signed
 * same-origin proxy (https://flow.google.com/asb/...=s1600-rw) - those tiles
 * are finished results too, they just lost their CDN URL when the page was
 * reloaded. Generation's "new result" detection must keep insisting on the
 * CDN URL (a stale tile may never look new); recovery is allowed both.
 */
function isRecoverableAssetSrc(src) {
  if (isFinalResultUrl(src)) return true;
  if (!/^https?:/i.test(src)) return false;
  try {
    const url = new URL(src);
    return url.hostname === 'flow.google.com' && /^\/asb\//.test(url.pathname);
  } catch {
    return false;
  }
}

function hashBytes(buffer) {
  return createHash('sha1').update(buffer).digest('hex');
}

export class FlowDriver {
  constructor({ page, context, selectors, settings }) {
    this.page = page;
    this.context = context;
    this.selectors = selectors instanceof SelectorSet ? selectors : new SelectorSet(selectors);
    this.settings = settings;
    this.timeouts = settings.timeouts;
    this.downloads = [];
    // Filenames already uploaded into the project during this run, so repeated
    // items reuse the asset instead of creating duplicates.
    this.uploadedRefNames = new Set();
    // Byte ownership: every image already on the page when a generation starts is
    // recorded here, so a reference upload - or a stale tile that swaps its src
    // and looks new - can never be adopted as this item's result. This is the
    // guarantee, not the tile's label or buttons.
    this.seenAssetSrcs = new Set();
    this.seenAssetHashes = new Set();
    this.page.on('download', (download) => this.downloads.push(download));
  }

  // ---------------------------------------------------------------- helpers

  find(key, options = {}) {
    return this.selectors.find(this.page, key, {
      timeout: this.timeouts.selectorMs,
      ...options,
    });
  }

  exists(key, options = {}) {
    return this.selectors.exists(this.page, key, options);
  }

  async labelOf(locator) {
    const text = await locator.innerText().catch(() => '');
    const aria = await locator.getAttribute('aria-label').catch(() => '');
    return `${text} ${aria ?? ''}`.replace(/\s+/g, ' ').trim();
  }

  /**
   * Flow menu entries often carry a leading emoji (e.g. "🍌 Nano Banana Pro"),
   * so exact accessible-name matching is unreliable. Try exact first, then
   * case-insensitive substring.
   */
  async findByText(text, { timeout = 5000, required = true, root = this.page } = {}) {
    const quoted = JSON.stringify(String(text));
    const candidates = [
      `role=menuitem[name=${quoted} i]`,
      `role=option[name=${quoted} i]`,
      `[role='menuitem']:has-text(${quoted})`,
      `[role='option']:has-text(${quoted})`,
      `mat-button-toggle:has-text(${quoted})`,
      `button:has-text(${quoted})`,
      `text=${quoted}`,
    ];
    const scoped = new SelectorSet({ transient: candidates });
    return scoped.find(root, 'transient', { timeout, required });
  }

  // ------------------------------------------------------------- navigation

  async goto() {
    await this.page
      .goto(this.settings.flowUrl, {
        waitUntil: 'domcontentloaded',
        timeout: this.timeouts.navigationMs,
      })
      .catch((error) => log.warn(`Navigation warning: ${error.message.split('\n')[0]}`));
    await sleep(1500);
    await this.dismissConsent();
  }

  /**
   * The cookie consent bar overlays the prompt-box controls and swallows clicks,
   * so it has to go before anything in the prompt box can be used.
   */
  async dismissConsent() {
    const banner = await this.selectors.find(this.page, 'consentDismiss', {
      timeout: 2500,
      required: false,
    });
    if (!banner) return false;
    await banner.locator.click({ timeout: 4000 }).catch(() => {});
    await sleep(500);
    log.debug('Dismissed the cookie consent banner.');
    return true;
  }

  /** Open a specific project directly instead of relying on the landing redirect. */
  async openProject(url) {
    log.info(`Opening project ${url}`);
    await this.page
      .goto(url, { waitUntil: 'domcontentloaded', timeout: this.timeouts.navigationMs })
      .catch((error) => log.warn(`Navigation warning: ${error.message.split('\n')[0]}`));
    await sleep(3000);
    await this.dismissConsent();
    if (this.isProjectUnavailable()) {
      throw new Error(
        `Flow reports this project as unavailable (${this.page.url()}). It may have been deleted, or it may ` +
          'belong to a different Google account. Point the job at a current projectUrl, or omit it so a new ' +
          'project is created.',
      );
    }
    await this.waitForPromptBox({ timeout: this.timeouts.readyMs });
  }

  /**
   * A deleted or foreign project URL lands on Flow's 404 page, which has no
   * composer. The URL says so, so it is detected before waitForPromptBox turns a
   * project problem into a misleading "calibrate the selector" error.
   */
  isProjectUnavailable() {
    const url = String(this.page.url() ?? '');
    return /\/404(\/|$|\?)/i.test(url) || /[?&]reason=project\b/i.test(url);
  }

  /** The project's own title, as shown in its header (Flow names new ones by date). */
  async projectName() {
    const found = await this.selectors.find(this.page, 'projectTitle', { timeout: 2500, required: false });
    if (!found) return null;
    const value = await found.locator.inputValue().catch(() => null);
    if (value && value.trim()) return value.trim();
    const text = (await found.locator.innerText().catch(() => '')) ?? '';
    return text.trim() || null;
  }

  async signInState({ timeoutMs = 5000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      if (await this.exists('signedIn', { timeout: 0 })) return 'in';
      if (await this.exists('signedOut', { timeout: 0 })) return 'out';
      if (Date.now() >= deadline) return 'unknown';
      await sleep(500);
    }
  }

  /**
   * Lenient sign-in check: falls back to "a prompt box exists on the Flow host"
   * when the signedIn/signedOut selectors are not calibrated yet. Never reports
   * a signed-in state from the marketing page or from an accounts.google.com
   * challenge page.
   */
  async looksSignedIn() {
    const url = this.page.url();
    if (/accounts\.google\.com/.test(url)) return 'challenge';
    if (!/^https?:\/\/([a-z0-9-]+\.)*flow\.google\.com\//i.test(url)) return 'unknown';

    const state = await this.signInState({ timeoutMs: 4000 });
    if (state !== 'unknown') return state;

    if (/\/about(\/|$|\?)/i.test(url)) return 'out';
    if (await this.exists('promptBox', { timeout: 0 })) return 'in';
    return 'unknown';
  }

  async waitForPromptBox({ timeout } = {}) {
    return this.find('promptBox', { timeout: timeout ?? this.timeouts.readyMs });
  }

  async reload() {
    await this.page.reload({ waitUntil: 'domcontentloaded', timeout: this.timeouts.navigationMs });
    await sleep(1200);
    await this.dismissConsent();
    await this.waitForPromptBox();
  }

  async isInsideProject() {
    if (/\/project\//.test(this.page.url())) return true;
    const hasPrompt = await this.exists('promptBox', { timeout: 0 });
    if (!hasPrompt) return false;
    const hasNewProject = await this.exists('newProjectButton', { timeout: 0 });
    return !hasNewProject;
  }

  async findProjectCard(name) {
    for (const selector of this.selectors.candidates('projectCard')) {
      const card = this.page.locator(selector).filter({ hasText: name }).first();
      if ((await card.count().catch(() => 0)) > 0 && (await card.isVisible().catch(() => false))) {
        return card;
      }
    }
    return null;
  }

  async ensureProject(name, { forceNew = false } = {}) {
    if (forceNew && (await this.isInsideProject())) {
      // Flow landed on a project instead of the project list. A fresh
      // navigation is the only way back; if that still lands inside one, the
      // caller's "create, never reuse" contract cannot be honored and must
      // fail loudly rather than silently adopt the current project.
      log.warn('--new-project: Flow is inside a project; reloading the project list.');
      await this.goto();
    }

    if (!forceNew && (await this.isInsideProject())) {
      log.debug('Already inside a Flow project.');
      return { created: false };
    }

    if (!forceNew && name) {
      const card = await this.findProjectCard(name);
      if (card) {
        log.info(`Opening project "${name}".`);
        await card.click();
        await sleep(1500);
        await this.waitForPromptBox();
        return { created: false };
      }
      log.warn(`Project "${name}" was not found in the project list; creating a new project instead.`);
    }

    const button = await this.find('newProjectButton', { required: false });
    if (!button) {
      throw new Error(
        'A new Flow project was requested, but the project list ("Start new session") is not ' +
          `visible at ${this.page.url()} - Flow may have opened an existing project. Refusing to reuse it.`,
      );
    }
    log.info('Creating a new Flow project.');
    await button.locator.click();
    await sleep(2000);
    await this.waitForPromptBox();
    return { created: true };
  }

  // ---------------------------------------------------------------- settings

  /**
   * Agent mode is ON by default and persists per project. With Agent ON the
   * prompt box only exposes "Add ingredients" + "Start generation"; the model,
   * aspect-ratio and output-count controls (button.settings-trigger-button) are
   * hidden. The batch runner therefore turns Agent OFF for the standard prompt
   * box. This is idempotent, unlike a blind click on the toggle chip.
   */
  async agentModeState() {
    const found = await this.selectors.find(this.page, 'agentToggle', { timeout: 3000, required: false });
    if (!found) return 'unknown';
    const pressed = await found.locator.getAttribute('aria-pressed').catch(() => null);
    if (pressed === 'true') return 'on';
    if (pressed === 'false') return 'off';
    return 'unknown';
  }

  async ensureAgentMode(enabled) {
    const wanted = enabled ? 'true' : 'false';
    const found = await this.selectors.find(this.page, 'agentToggle', { timeout: 8000, required: false });
    if (!found) {
      log.warn('Could not find the Agent mode toggle; leaving Agent as-is.');
      return false;
    }
    for (let attempt = 0; attempt < 4; attempt += 1) {
      const pressed = await found.locator.getAttribute('aria-pressed').catch(() => null);
      if (pressed === wanted) {
        log.debug(`Agent mode is ${enabled ? 'on' : 'off'}.`);
        return true;
      }
      await found.locator.click({ force: true }).catch(() => {});
      await sleep(700);
    }
    const final = await found.locator.getAttribute('aria-pressed').catch(() => null);
    if (final === wanted) return true;
    log.warn(`Could not set Agent mode to ${enabled ? 'on' : 'off'} (aria-pressed=${final}).`);
    return false;
  }

  async openSettingsOverlay() {
    // Idempotent: clicking the trigger while the overlay is already open would
    // close it again, leaving nothing to configure. Trust the overlay's CONTROLS
    // (the mode toggle), not just its container - a container left mid-close by
    // the previous item's in-place clear still matches "settingsOverlay" while
    // its toggles are already gone, which used to make the next apply fail with
    // "Could not locate the Flow UI element modeImageOption".
    if (await this.selectors.exists(this.page, 'modeImageOption', { timeout: 0 })) {
      return null;
    }
    if (await this.selectors.exists(this.page, 'settingsOverlay', { timeout: 0 })) {
      await this.closeSettingsOverlay();
    }
    const button = await this.find('settingsTriggerButton', { timeout: 8000 });
    await button.locator.click();
    await sleep(600);
    return button;
  }

  async closeSettingsOverlay() {
    await this.page.keyboard.press('Escape').catch(() => {});
    await sleep(400);
  }

  /** The "🍌 Nano Banana 2 Lite · 16:9 · x2" summary shown on the settings trigger. */
  async settingsSummary() {
    const found = await this.selectors.find(this.page, 'settingsTriggerButton', {
      timeout: 2500,
      required: false,
    });
    if (!found) return null;
    return (await found.locator.innerText().catch(() => '')).replace(/\s+/g, ' ').trim();
  }

  async ensureMode(mode) {
    const key = mode === 'image' ? 'modeImageOption' : mode === 'video' ? 'modeVideoOption' : null;
    if (!key) throw new Error(`Unsupported generation mode "${mode}". Use "image" or "video".`);
    const option = await this.find(key, { timeout: 8000 });
    if (await this.#isToggleChecked(option.locator)) return true;
    await option.locator.click();
    await sleep(500);
    return true;
  }

  /** Flow's mat-button-toggle buttons expose selection via aria-checked. */
  async #isToggleChecked(locator) {
    const checked = await locator.getAttribute('aria-checked').catch(() => null);
    if (checked === 'true') return true;
    const pressed = await locator.getAttribute('aria-pressed').catch(() => null);
    return pressed === 'true';
  }

  /**
   * Model names are prefixes of one another ("Nano Banana 2" vs "Nano Banana 2
   * Lite"), so substring matching would silently pick the wrong model. Compare
   * normalised labels for equality instead, and say so when only a near match
   * exists.
   */
  async selectModel(name) {
    if (!name) return true;
    const wanted = normalizeModelName(name);
    const button = await this.find('modelFamilyButton', { timeout: 8000 });

    const current = normalizeModelName(await this.modelButtonLabel(button.locator));
    if (current === wanted) {
      log.debug(`Model already set to "${name}".`);
      return true;
    }

    await button.locator.click();
    await sleep(700);

    const items = this.page.locator("[role='menuitem']");
    const count = await items.count().catch(() => 0);
    let exact = -1;
    let first = -1;
    const seen = [];

    for (let index = 0; index < count; index += 1) {
      const raw = (await items.nth(index).innerText().catch(() => '')).replace(/\s+/g, ' ').trim();
      if (!raw) continue;
      seen.push(raw);
      if (first < 0) first = index;
      if (normalizeModelName(raw) === wanted) {
        exact = index;
        break;
      }
    }

    const chosen = exact >= 0 ? exact : first;
    if (chosen < 0) {
      await this.page.keyboard.press('Escape').catch(() => {});
      log.warn(`No model named "${name}" in the model menu (saw: ${seen.join(', ')}). Keeping the current model.`);
      return false;
    }
    if (exact < 0) {
      log.warn(`No exact model named "${name}"; falling back to "${seen[0]}".`);
    }

    await items.nth(chosen).click();
    await sleep(600);
    return true;
  }

  /** The model button's own text, without the trailing icon ligature. */
  async modelButtonLabel(locator) {
    return locator
      .evaluate((element) => {
        const clone = element.cloneNode(true);
        clone.querySelectorAll('mat-icon').forEach((icon) => icon.remove());
        return clone.textContent ?? '';
      })
      .catch(() => '');
  }

  async setAspectRatio(ratio) {
    if (!ratio) return true;
    const group = await this.find('aspectRatioGroup', { timeout: 6000 });
    const option = await group.locator.locator(`button:has-text(${JSON.stringify(ratio)})`).first();
    if ((await option.count().catch(() => 0)) === 0) {
      log.warn(`Aspect ratio "${ratio}" is not offered by the current mode; keeping the current ratio.`);
      return false;
    }
    if (await this.#isToggleChecked(option)) return true;
    await option.click();
    await sleep(500);
    return true;
  }

  async setOutputCount(count) {
    if (!count || count < 1) return true;
    const group = await this.find('outputCountGroup', { timeout: 6000 });
    const option = await group.locator.locator(`button:has-text(${JSON.stringify(`x${count}`)})`).first();
    if ((await option.count().catch(() => 0)) === 0) {
      log.warn(`Output count x${count} is not offered; keeping the current count.`);
      return false;
    }
    if (await this.#isToggleChecked(option)) return true;
    await option.click();
    await sleep(500);
    return true;
  }

  /**
   * Flow's Agent mode must be OFF for the standard prompt box: only then does
   * the settings trigger (model / aspect ratio / output count) appear. Mode,
   * model, ratio and count all live inside that one overlay.
   */
  // ------------------------------------------------- project default settings

  /**
   * Agent mode ON is the mode Flow actually allows automated sessions to
   * generate in. With it ON the prompt-box settings trigger is hidden, so the
   * model / aspect ratio / output count come from the PROJECT defaults, which
   * this panel edits.
   */
  async openProjectSettings() {
    const button = await this.find('projectSettingsButton', { timeout: 8000 });
    // The panel is a sidebar and the trigger does not always register first
    // time, so retry before giving up.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await button.locator.click({ force: true }).catch(() => {});
      await sleep(1600);
      if (await this.selectors.exists(this.page, 'projectAspectGroup', { timeout: 4000 })) return true;
    }
    throw new Error('The project settings panel did not open. Calibrate "projectSettingsButton".');
  }

  async projectModelLabel() {
    const found = await this.selectors.find(this.page, 'projectModelButton', { timeout: 4000, required: false });
    if (!found) return '';
    return this.modelButtonLabel(found.locator);
  }

  async setProjectModel(name) {
    const found = await this.selectors.find(this.page, 'projectModelButton', { timeout: 5000 });
    await found.locator.click();
    await sleep(800);
    const option = await this.findByText(name, { timeout: 6000, required: false });
    if (!option) {
      log.warn(`Project default model "${name}" was not found in the model menu.`);
      await this.page.keyboard.press('Escape').catch(() => {});
      return false;
    }
    await option.locator.click();
    await sleep(600);
    return true;
  }

  /** Returns true when the value had to change. */
  async setProjectToggle(groupKey, label) {
    const group = await this.find(groupKey, { timeout: 5000 });
    const option = group.locator.locator(`button:has-text(${JSON.stringify(label)})`).first();
    if ((await option.count().catch(() => 0)) === 0) {
      log.warn(`Project setting "${label}" is not offered.`);
      return false;
    }
    if (await this.#isToggleChecked(option)) return false;
    await option.click();
    await sleep(400);
    return true;
  }

  async saveProjectSettings() {
    const save = await this.selectors.find(this.page, 'projectSettingsSave', { timeout: 5000, required: false });
    if (!save) {
      log.warn('No Save control in the project settings panel; changes may not persist.');
      return false;
    }
    await save.locator.click();
    await sleep(1200);
    return true;
  }

  /**
   * The panel is a sidebar that covers the composer, so it MUST be closed or
   * every later step fails to find the prompt box. Verified rather than assumed.
   */
  async closeProjectSettings() {
    for (let attempt = 0; attempt < 4; attempt += 1) {
      if (!(await this.selectors.exists(this.page, 'projectAspectGroup', { timeout: 0 }))) return true;
      const close = await this.selectors.find(this.page, 'projectSettingsClose', { timeout: 2500, required: false });
      if (close) await close.locator.click({ force: true }).catch(() => {});
      else await this.page.keyboard.press('Escape').catch(() => {});
      await sleep(700);
    }
    const stillOpen = await this.selectors.exists(this.page, 'projectAspectGroup', { timeout: 0 });
    if (stillOpen) log.warn('The project settings panel would not close; the composer may be covered.');
    return !stillOpen;
  }

  /**
   * Bring the project's image defaults in line with the job. Only writes when
   * something actually differs, so a run does not re-save on every item.
   */
  async applyProjectDefaults({ model, aspectRatio, outputs }) {
    await this.openProjectSettings();
    let changed = false;

    if (model) {
      const current = await this.projectModelLabel();
      if (normalizeModelName(current) !== normalizeModelName(model)) {
        changed = (await this.setProjectModel(model)) || changed;
      }
    }
    if (aspectRatio) changed = (await this.setProjectToggle('projectAspectGroup', aspectRatio)) || changed;
    if (outputs) changed = (await this.setProjectToggle('projectOutputGroup', `x${outputs}`)) || changed;

    if (changed) {
      await this.saveProjectSettings();
      log.info('Updated the project defaults for image generation.');
    }
    const label = await this.projectModelLabel();
    await this.closeProjectSettings();
    if (label) log.info(`Project image defaults: ${label.replace(/\s+/g, ' ').trim()}`);
    return true;
  }

  async applyGenerationSettings({ mode, model, aspectRatio, outputs, agent }) {
    // Agent ON is the default because Agent OFF is refused outright for an
    // automated session ("unusual activity"), which no amount of waiting fixes.
    if (agent !== false) {
      await this.ensureAgentMode(true);
      return this.applyProjectDefaults({ model, aspectRatio, outputs });
    }

    await this.ensureAgentMode(false);

    const summary = await this.settingsSummary();
    const alreadyMatches =
      summary !== null &&
      (!model || summary.includes(model)) &&
      (!aspectRatio || summary.includes(aspectRatio)) &&
      (!outputs || summary.includes(`x${outputs}`)) &&
      (mode === 'image' ? /Nano Banana|Imagen/i.test(summary) : !/Nano Banana|Imagen/i.test(summary));
    if (alreadyMatches) {
      log.debug(`Prompt-box settings already match: ${summary}`);
      return true;
    }

    await this.openSettingsOverlay();
    try {
      await this.ensureMode(mode ?? 'image');
    } catch (error) {
      if (!/modeImageOption/.test(String(error?.message ?? ''))) throw error;
      // The trigger click is sometimes swallowed while the Agent toggle is
      // still transitioning (right after an in-place composer clear), leaving
      // the overlay without its controls. Close and reopen once the UI has
      // settled rather than failing the whole item.
      await this.closeSettingsOverlay();
      await this.openSettingsOverlay();
      await this.ensureMode(mode ?? 'image');
    }
    await this.selectModel(model);
    await this.setAspectRatio(aspectRatio);
    await this.setOutputCount(outputs);
    await this.closeSettingsOverlay();
    const after = await this.settingsSummary();
    if (after) log.info(`Prompt-box settings: ${after}`);
    return true;
  }

  // ------------------------------------------------------------------ prompt

  async clearPrompt() {
    const box = await this.find('promptBox');
    await box.locator.click();
    await this.page.keyboard.press('Control+A').catch(() => {});
    await this.page.keyboard.press('Delete').catch(() => {});
    await sleep(150);
  }

  async setPrompt(text) {
    const box = await this.find('promptBox');
    await box.locator.click();
    await this.page.keyboard.press('Control+A').catch(() => {});
    await this.page.keyboard.press('Delete').catch(() => {});
    await sleep(120);
    try {
      await box.locator.fill(text);
    } catch {
      await box.locator.click();
      await this.page.keyboard.type(text, { delay: 8 });
    }
    await sleep(200);
  }

  /** Append text at the end of the prompt box without clearing it. */
  async typePrompt(text) {
    if (!text) return;
    const box = await this.find('promptBox');
    await box.locator.click();
    await this.page.keyboard.press('End').catch(() => {});
    await this.page.keyboard.type(text, { delay: 8 });
    await sleep(200);
  }

  // -------------------------------------------------------------- references

  async pickFileInput() {
    const inputs = this.page.locator('input[type=file]');
    const count = await inputs.count().catch(() => 0);
    let fallback = null;
    for (let index = 0; index < count; index += 1) {
      const input = inputs.nth(index);
      const accept = (await input.getAttribute('accept').catch(() => '')) ?? '';
      if (accept === '' || /image/i.test(accept)) return input;
      fallback = fallback ?? input;
    }
    return fallback;
  }

  /**
   * Reference images go in through the prompt box's Add menu:
   *   button[aria-label="Add ingredients to the prompt box"]
   *     -> "Upload media"            (opens the project asset picker)
   *     -> hidden input[type=file]   (uploads into the project; the new asset is
   *                                   auto-selected in the picker)
   *     -> "Add to prompt"           (attaches the selection as ingredients)
   *
   * Uploading through the picker means each reference becomes a project asset.
   * Re-running the same refs therefore creates duplicates in the project.
   */
  /**
   * Clicking the prompt box's "+" opens Flow's asset library **inline**: search,
   * category navigation and the project's asset list. It does NOT create a file
   * input, so no upload dialog is involved.
   *
   * "Upload media" is a separate button *inside* that library whose only job is
   * to spawn the hidden file input; it is clicked only when a file really has to
   * be uploaded.
   */
  async openAssetLibrary() {
    const addButton = await this.find('addIngredientsButton', { timeout: 8000 });
    await addButton.locator.click();

    let ready = await this.selectors.find(this.page, 'assetPickerSearch', { timeout: 8000, required: false });
    if (!ready) ready = await this.selectors.find(this.page, 'assetPickerItem', { timeout: 5000, required: false });
    if (!ready) {
      throw new Error(
        'Clicking the prompt-box "+" did not open the asset library. Calibrate "assetPickerSearch" / ' +
          '"assetPickerItem" in config/selectors.json.',
      );
    }
    await sleep(600);
  }

  async pickerIsOpen() {
    return this.selectors.exists(this.page, 'assetPickerDialog', { timeout: 0 });
  }

  /** Any CDK popover still on screen - the asset library, a menu, a settings panel. */
  async overlayIsOpen() {
    for (const selector of this.selectors.candidates('overlayPane')) {
      const count = await this.page.locator(`${selector}:visible`).count().catch(() => 0);
      if (count > 0) return true;
    }
    return false;
  }

  /**
   * Dismiss anything still covering the prompt box. The asset library has its own
   * close, but a plain CDK popover - left open by a menu or a retry - covers the
   * Generate button and is not matched by pickerIsOpen, so Escape it away.
   */
  async dismissOverlays({ attempts = 4 } = {}) {
    for (let index = 0; index < attempts; index += 1) {
      if (!(await this.overlayIsOpen())) return true;
      await this.page.keyboard.press('Escape').catch(() => {});
      await sleep(400);
    }
    return !(await this.overlayIsOpen());
  }

  /** Wait until the picker has marked the freshly uploaded assets as selected. */
  async waitForUploadToSettle({ expected = 1, timeout = 120000 } = {}) {
    const deadline = Date.now() + timeout;
    let lastLogged = 0;
    while (Date.now() < deadline) {
      const selected = await this.page
        .locator("button.asset-item[role='option'][aria-selected='true'], .asset-item-active")
        .count()
        .catch(() => 0);
      if (selected >= expected) {
        await sleep(800);
        return true;
      }
      if (Date.now() - lastLogged > 15000) {
        lastLogged = Date.now();
        log.debug(`Waiting for upload to finish (${selected}/${expected} selected)…`);
      }
      if (!(await this.pickerIsOpen())) {
        // The picker closed itself, which means it accepted the upload.
        return true;
      }
      await sleep(700);
    }
    return false;
  }

  /**
   * Upload path: open the library, then use its "Upload media" button to reach
   * the hidden file input. The freshly uploaded asset is left selected and must
   * be confirmed with "Add to prompt".
   */
  async attachUploadedFiles(files) {
    await this.openAssetLibrary();

    const mediaOption = await this.find('addMediaOption', { timeout: 8000, required: false });
    if (!mediaOption) {
      await this.page.keyboard.press('Escape').catch(() => {});
      throw new Error(
        'The asset library did not offer "Upload media". Calibrate "addMediaOption" in config/selectors.json.',
      );
    }
    // "Upload media" spawns the hidden file input, and clicking it opens the OS
    // file dialog. Intercept that chooser so the native window is never shown:
    // setting the files on the input afterwards still uploads them, but leaves
    // the OS dialog sitting on top of the page.
    const chooserPromise = this.page.waitForEvent('filechooser', { timeout: 8000 }).catch(() => null);
    await mediaOption.locator.click();
    const chooser = await chooserPromise;
    if (chooser) {
      log.debug('Intercepted the OS file chooser; the native dialog is not shown.');
      await chooser.setFiles(files);
    } else {
      const input = await this.waitForFileInput(8000);
      if (!input) {
        throw new Error(
          'No file input appeared after choosing "Upload media". Calibrate "fileInput" in config/selectors.json.',
        );
      }
      await input.setInputFiles(files);
    }
    // Flow must upload and thumbnail the file before it can be attached. Large
    // references (the BG_*.png set is 14-18 MB each) need real time here, so
    // wait for the picker to actually mark the upload as selected.
    const settled = await this.waitForUploadToSettle({ expected: files.length });
    if (!settled) {
      log.warn(
        `Uploaded ${files.length} file(s) but the picker did not mark them as selected within the timeout; ` +
          'attempting to attach anyway.',
      );
    }
    for (const file of files) this.uploadedRefNames.add(path.basename(file));

    if (!(await this.pickerIsOpen())) {
      log.debug('Picker closed after upload; assuming the assets were attached.');
      return;
    }
    const attach = await this.findByText('Add to prompt', { timeout: 15000, required: false });
    if (!attach) {
      await this.page.keyboard.press('Escape').catch(() => {});
      throw new Error(
        'The asset picker did not offer "Add to prompt". Calibrate "addToPromptButton" in config/selectors.json.',
      );
    }
    await attach.locator.click();
    await sleep(1500);
  }

  /**
   * True when the project already holds an asset with this name.
   *
   * Strict on purpose: the title must match exactly, or match once its file
   * extension is stripped. A fuzzy search hit is never taken as proof, because
   * "prepare" uses this to decide whether a reference still has to be uploaded.
   * Opens and closes the asset library itself.
   */
  async galleryHasAsset(name) {
    await this.openAssetLibrary();
    try {
      const search = await this.find('assetPickerSearch', { timeout: 8000, required: false });
      if (!search) return false;
      await search.locator.fill(name);
      await sleep(1800);

      let items = null;
      for (const selector of this.selectors.candidates('assetPickerItem')) {
        const candidate = this.page.locator(selector);
        if ((await candidate.count().catch(() => 0)) > 0) {
          items = candidate;
          break;
        }
      }
      if (!items) return false;

      const count = await items.count();
      for (let index = 0; index < count; index += 1) {
        const title = (
          (await items
            .nth(index)
            .locator('span.asset-title, .asset-title')
            .first()
            .innerText()
            .catch(() => '')) ?? ''
        ).trim();
        if (!title) continue;
        if (title === name || title.replace(/\.[a-z0-9]+$/i, '') === name) return true;
      }
      return false;
    } finally {
      await this.closeAssetLibrary();
    }
  }

  /**
   * Reuse path: searching for an asset and clicking it attaches it to the prompt
   * and closes the picker in one action - no "Add to prompt" step.
   *
   * Prefers an exact title match ("Maya") over an exact filename match
   * ("Maya.png") over the first fuzzy hit, so a name never attaches the wrong
   * asset when the project holds similarly named files.
   */
  async attachExistingAsset(name) {
    await this.openAssetLibrary();
    const search = await this.find('assetPickerSearch', { timeout: 6000 });
    await search.locator.fill('');
    await search.locator.fill(name);
    await sleep(1800);

    let items = null;
    for (const selector of this.selectors.candidates('assetPickerItem')) {
      const candidate = this.page.locator(selector);
      if ((await candidate.count().catch(() => 0)) > 0) {
        items = candidate;
        break;
      }
    }
    if (!items) {
      await this.page.keyboard.press('Escape').catch(() => {});
      return false;
    }

    const count = await items.count();
    let chosen = -1;
    let fuzzy = -1;
    for (let index = 0; index < count; index += 1) {
      const title = (await items
        .nth(index)
        .locator("span.asset-title, .asset-title")
        .first()
        .innerText()
        .catch(() => '') ?? ''
      ).trim();
      if (!title) continue;
      if (fuzzy < 0) fuzzy = index;
      const stem = title.replace(/\.[a-z0-9]+$/i, '');
      if (title === name || stem === name) {
        chosen = index;
        break;
      }
    }
    if (chosen < 0) chosen = fuzzy;
    if (chosen < 0) {
      await this.page.keyboard.press('Escape').catch(() => {});
      return false;
    }

    await items.nth(chosen).click().catch(() => {});
    await sleep(1800);

    // Clicking a result either attaches it and closes the library, or selects it
    // and shows a preview that still needs "Add to prompt". Both happen, so the
    // confirm step is conditional rather than assumed.
    if (await this.pickerIsOpen()) {
      const attach = await this.findByText('Add to prompt', { timeout: 8000, required: false });
      if (attach) {
        await attach.locator.click().catch(() => {});
        await sleep(1500);
      }
    }

    // Never leave an overlay covering the prompt box.
    if (await this.pickerIsOpen()) {
      await this.page.keyboard.press('Escape').catch(() => {});
      await sleep(500);
    }
    return true;
  }

  /** Close the asset library if it is still on screen. */
  async closeAssetLibrary() {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (!(await this.pickerIsOpen())) return true;
      await this.page.keyboard.press('Escape').catch(() => {});
      await sleep(500);
    }
    return !(await this.pickerIsOpen());
  }

  /**
   * Attach reference images to the prompt box.
   *
   * `refs` is a list of `{ name, path }`: `name` is the asset name inside the
   * Flow project, `path` is the local file to upload if that name is missing.
   *
   * Flow has no direct file-to-prompt path, so its asset picker is used either
   * way. `mode` decides which route is taken per reference:
   *   reuse  - attach the existing project asset by name; upload the local file
   *            only when the name is not found (default, self-healing)
   *   assets - attach by name only; never upload
   *   upload - always upload the local file
   *
   * Each reference is handled in its own picker session. That is deliberate:
   * uploading several files at once only ever attaches the first one.
   */
  async addReferences(refs, { mode = 'reuse' } = {}) {
    if (!refs || refs.length === 0) return true;

    const before = await this.selectors.count(this.page, 'promptReferenceChip');
    log.debug(
      `References (${mode}): ${refs
        .map((ref) => `${ref.name}${ref.path ? '' : ' [project-only]'}`)
        .join(', ')}`,
    );

    for (const ref of refs) {
      if (mode !== 'upload') {
        const attached = await this.attachExistingAsset(ref.name);
        if (attached) {
          log.debug(`Attached project asset "${ref.name}".`);
          continue;
        }
        if (mode === 'assets') {
          log.warn(`Reference "${ref.name}" is not in the project's assets, and refMode "assets" never uploads.`);
          continue;
        }
        if (!ref.path) {
          log.warn(`Reference "${ref.name}" is not in the project's assets and has no local path to upload.`);
          continue;
        }
        log.debug(`"${ref.name}" not found in the project; uploading ${path.basename(ref.path)}.`);
      }

      if (!ref.path) {
        log.warn(`Reference "${ref.name}" has no local path; skipping.`);
        continue;
      }
      await this.attachUploadedFiles([ref.path]);
    }

    const confirmed = await this.waitForReferences(before + refs.length);
    if (!confirmed) {
      // Generating without the intended reference would silently produce the
      // wrong image, so fail the item and let the runner retry it.
      await this.closeAssetLibrary();
      throw new GenerationError(
        `Attached ${refs.length} reference(s) but could not confirm them in the prompt box; refusing to ` +
          'generate without them. Calibrate "promptReferenceChip" if this is a false alarm.',
        { retryable: true },
      );
    }
    return true;
  }

  async waitForFileInput(timeout) {
    const deadline = Date.now() + timeout;
    for (;;) {
      const input = await this.pickFileInput();
      if (input) return input;
      if (Date.now() >= deadline) return null;
      await sleep(300);
    }
  }

  /**
   * Detach every ingredient from the composer.
   *
   * The chips live in `flow-ingredient-bar`, NOT inside the ProseMirror
   * editable, so a select-all in the editor does not remove them. Each chip
   * carries its own remove control that has to be clicked.
   */
  async detachAllReferences({ max = 12 } = {}) {
    for (let attempt = 0; attempt < max; attempt += 1) {
      const count = await this.selectors.count(this.page, 'promptReferenceChip');
      if (count === 0) return true;

      const remove = await this.selectors.find(this.page, 'promptReferenceRemoveButton', {
        timeout: 2500,
        required: false,
      });
      if (!remove) {
        log.warn('No control found to detach an attached reference.');
        return false;
      }
      // The overlay is transparent until hovered, so force the click.
      await remove.locator.click({ force: true }).catch(() => {});
      await sleep(600);
    }
    return (await this.selectors.count(this.page, 'promptReferenceChip')) === 0;
  }

  /**
   * Reset the composer in place for the next item.
   *
   * Reloading the whole Flow app before every item is slow and unlike anything a
   * human does - it was the largest behavioural difference from the Renderly
   * driver, which loads the page once per batch. Clearing the composer achieves
   * the same clean state; a reload stays available as a fallback and via
   * `generation.resetBetweenItems: "reload"`.
   */
  async clearComposerForNextItem() {
    await this.closeAssetLibrary();
    await this.page.keyboard.press('Escape').catch(() => {});
    await sleep(300);

    // References first: clicking the editor while a chip sits under the cursor
    // can open the chip preview instead of placing the caret.
    const detached = await this.detachAllReferences();
    await this.clearPrompt();

    const remaining = await this.selectors.count(this.page, 'promptReferenceChip');
    if (remaining > 0) log.warn(`${remaining} reference(s) still attached after clearing.`);
    return detached && remaining === 0;
  }

  /** "Clear prompt" wipes both the text and every attached ingredient. */
  async clearPromptAndReferences() {
    const button = await this.selectors.find(this.page, 'clearPromptButton', {
      timeout: 3000,
      required: false,
    });
    if (button) {
      await button.locator.click().catch(() => {});
      await sleep(600);
      return true;
    }
    // No clear control in this version of the UI - do it by hand.
    await this.detachAllReferences();
    await this.clearPrompt();
    return (await this.selectors.count(this.page, 'promptReferenceChip')) === 0;
  }

  /**
   * Read a generated tile's prompt by clicking ITS OWN redo ("Reuse prompt")
   * control - which repopulates the composer with the tile's stored prompt -
   * reading the composer text, then clearing the composer again. One tile at a
   * time; the run never touches "Start generation", so nothing can be
   * generated accidentally. Returns null when the tile carries no redo control.
   */
  async readTilePrompt(tileIndex, { selector }) {
    const tile = this.page.locator(selector).nth(tileIndex);
    await tile.scrollIntoViewIfNeeded().catch(() => {});
    await tile.hover().catch(() => {});
    await sleep(250);

    let clicked = false;
    for (const candidate of this.selectors.candidates('tileRedoButton')) {
      const button = tile.locator(candidate).first();
      if ((await button.count().catch(() => 0)) > 0) {
        // The tile hotbar is transparent until hovered, like the chip remove
        // control, so force the click.
        await button.click({ force: true, timeout: 4000 }).catch(() => {});
        clicked = true;
        break;
      }
    }
    if (!clicked) return null;
    await sleep(1200);

    const box = await this.selectors.find(this.page, 'promptBox', {
      timeout: 4000,
      required: false,
    });
    const text = box
      ? await box.locator
          .evaluate((node) => (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim())
          .catch(() => null)
      : null;
    const cleared = await this.clearComposerForNextItem().catch(() => false);
    if (!cleared) await this.clearPrompt().catch(() => {});
    return text;
  }

  async waitForReferences(expected) {
    const deadline = Date.now() + REFERENCE_CONFIRM_MS;
    while (Date.now() < deadline) {
      const count = await this.selectors.count(this.page, 'promptReferenceChip');
      if (count >= expected) return true;
      await sleep(400);
    }
    log.warn(
      `Uploaded ${expected} reference image(s) but could not confirm them in the prompt box. ` +
        'Calibrate "promptReferenceChip" if generations ignore the references.',
    );
    return false;
  }

  async mentionReferences(names) {
    if (!names || names.length === 0) return true;
    const box = await this.find('promptBox');
    for (const name of names) {
      await box.locator.click();
      await this.page.keyboard.press('End').catch(() => {});
      await this.page.keyboard.type(`@${name}`, { delay: 30 });
      await sleep(700);
      const suggestion = await this.findByText(name, { timeout: 2500, required: false });
      if (suggestion) await suggestion.locator.click();
      else await this.page.keyboard.press('Enter').catch(() => {});
      await this.page.keyboard.type(' ');
      await sleep(200);
    }
    return true;
  }

  // -------------------------------------------------------------- generation

  async generate() {
    // An open popover (the asset library, a menu left open by a retry) covers the
    // Generate button and makes the click time out, so clear anything on screen.
    await this.closeAssetLibrary();
    await this.dismissOverlays();
    await this.page.keyboard.press('Escape').catch(() => {});
    await sleep(300);

    const button = await this.find('generateButton', { requireEnabled: true, timeout: 15000 });
    // Whatever is already on screen is another item's outcome, not this one's.
    await this.markStaleAlerts();
    await button.locator.click({ timeout: 20000 });
  }

  async snapshotAssets() {
    const redoSelectors = this.selectors.candidates('tileRedoButton');
    for (const selector of this.selectors.candidates('assetTile')) {
      const tiles = this.page.locator(selector);
      const count = await tiles.count().catch(() => 0);
      if (count === 0) continue;

      const entries = [];
      for (let index = 0; index < count; index += 1) {
        const info = await tiles
          .nth(index)
          .evaluate(
            (node, redoSel) => {
              const img = node.tagName === 'IMG' ? node : node.querySelector('img');
              const src = img ? img.currentSrc || img.getAttribute('src') || '' : '';
              // The redo control ("Reuse prompt") exists only on a generated
              // result, so it is the discriminator - but it must be read from the
              // DOM. innerText is empty on these tiles, and the textContent
              // fallback concatenates the icon ligatures into
              // "favoriteredomore_vert" with no word boundary to match on, which
              // is why a text regex missed real results.
              const canRedo = redoSel.some((sel) => {
                try {
                  return node.matches(sel) || Boolean(node.querySelector(sel));
                } catch {
                  return false;
                }
              });
              return {
                key: src || node.getAttribute('data-testid') || node.getAttribute('id') || '',
                src,
                text: (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
                // A reloaded gallery names each tile with a short Flow caption
                // ("Woman auctioning vintage camera"), not the prompt; innerText is
                // empty there. The caption cannot identify the item, but it is the
                // only per-tile name available without clicking the redo control.
                label: (node.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 200),
                hasImage: Boolean(src) && /^https?:/i.test(src),
                canRedo,
                width: img ? img.naturalWidth : 0,
                height: img ? img.naturalHeight : 0,
              };
            },
            redoSelectors,
          )
          .catch(() => ({ key: '', text: '', label: '' }));
        // Uploaded references are labelled with their filename; generated stills
        // are not. Used to keep uploads out of "new result" detection.
        const uploaded = /\.(png|jpe?g|webp|gif|heic?|mp4|m4v|mov|avi|3gp)\b/i.test(info.text);
        // Flow reports a refused generation inside the tile itself.
        const failed = /\b(failed|unusual activity|not been charged|try again)\b/i.test(info.text);
        // Generated tiles offer "redo"; uploaded references never do.
        const canRedo = info.canRedo === true;
        entries.push({
          index,
          key: info.key || `#${index}`,
          src: info.src || '',
          text: info.text,
          label: info.label || '',
          uploaded,
          failed,
          canRedo,
          hasImage: info.hasImage,
          width: info.width,
          height: info.height,
        });
      }
      // The grid is a virtual scroller, so only rendered tiles are present.
      return { selector, entries };
    }
    return { selector: null, entries: [] };
  }

  /**
   * Scroll the gallery grid by one page (or to the top). Playwright's CSS
   * pierces shadow roots but page.evaluate is plain DOM, so the tile lookup
   * and the ancestor walk (across shadow hosts) happen in-page. Returns
   * whether the scroll position actually moved.
   */
  async scrollGallery({ toTop = false } = {}) {
    return this.page
      .evaluate((top) => {
        const findTile = (root) => {
          const direct = root.querySelector(
            'flow-grid-tile-container, flow-tile-container, flow-image-tile'
          );
          if (direct) return direct;
          for (const el of root.querySelectorAll('*')) {
            if (!el.shadowRoot) continue;
            const hit = findTile(el.shadowRoot);
            if (hit) return hit;
          }
          return null;
        };
        let node = findTile(document);
        if (!node) return false;
        let scroller = null;
        while (node) {
          const style = getComputedStyle(node);
          if (
            node.scrollHeight > node.clientHeight + 100 &&
            /auto|scroll/.test(style.overflowY)
          ) {
            scroller = node;
            break;
          }
          node =
            node.assignedSlot ||
            node.parentElement ||
            (node.getRootNode() instanceof ShadowRoot ? node.getRootNode().host : null);
        }
        if (!scroller) scroller = document.scrollingElement || document.documentElement;
        if (top) {
          const was = scroller.scrollTop;
          scroller.scrollTop = 0;
          return scroller.scrollTop !== was;
        }
        const before = scroller.scrollTop;
        scroller.scrollTop += Math.max(300, scroller.clientHeight * 0.8);
        return scroller.scrollTop !== before;
      }, toTop)
      .catch(() => false);
  }

  /**
   * Union of every tile the virtual grid mounts: snapshot, page down, snapshot
   * again until the scroll position stops moving. A tile can appear twice
   * under different keys when its <img> src lazily swaps; result filtering
   * keeps only finished-host srcs, so the placeholder copy drops out.
   */
  async scanAssets({ rounds = 40, settleMs = 700, initialSettle = true } = {}) {
    const first = initialSettle ? await this.waitForGridToSettle() : await this.snapshotAssets();
    if (!first.selector) return first;
    const merged = new Map();
    let snapshot = first;
    for (;;) {
      for (const entry of snapshot.entries) {
        if (!merged.has(entry.key)) merged.set(entry.key, entry);
      }
      if (merged.size === 0) break;
      const moved = await this.scrollGallery().catch(() => false);
      if (!moved || rounds <= 0) break;
      rounds -= 1;
      await sleep(settleMs);
      snapshot = await this.snapshotAssets();
    }
    await this.scrollGallery({ toTop: true }).catch(() => {});
    return { selector: first.selector, entries: [...merged.values()] };
  }

  /**
   * The gallery's generated results: the redo control is the discriminator
   * (uploads never carry one), the finished host means the render is done, and
   * tiles Flow reported as failed are excluded.
   */
  async listGeneratedResults(options = {}) {
    const scanned = await this.scanAssets(options);
    return scanned.entries.filter(
      (entry) =>
        entry.canRedo &&
        entry.hasImage &&
        !entry.uploaded &&
        !entry.failed &&
        !/\b(failed|unusual activity|not been charged|try again)\b/i.test(entry.label || '') &&
        isRecoverableAssetSrc(entry.src),
    );
  }

  /**
   * Tag every refusal / error element currently on screen.
   *
   * A banner left behind by an earlier item otherwise reads as THIS item's
   * outcome: on a 156-item batch a single stale "You have not been charged for
   * this generation" failed sixteen items in a row, twice each, while Flow was
   * generating every one of them successfully.
   */
  async markStaleAlerts() {
    for (const key of ['generationRefusal', 'errorBanner']) {
      for (const selector of this.selectors.candidates(key)) {
        const locator = this.page.locator(selector);
        const count = await locator.count().catch(() => 0);
        for (let index = 0; index < count; index += 1) {
          await locator
            .nth(index)
            .evaluate((node) => node.setAttribute('data-flow-imagesgen-stale', '1'))
            .catch(() => {});
        }
      }
    }
  }

  /**
   * The first refusal / error element that appeared since markStaleAlerts().
   * Only that can belong to the generation just started; anything already on the
   * page is another item's business.
   */
  async freshAlert(keys = ['generationRefusal', 'errorBanner']) {
    for (const key of keys) {
      for (const selector of this.selectors.candidates(key)) {
        const locator = this.page.locator(selector);
        const count = await locator.count().catch(() => 0);
        for (let index = 0; index < count; index += 1) {
          const node = locator.nth(index);
          const stale = await node
            .evaluate((element) => element.hasAttribute('data-flow-imagesgen-stale'))
            .catch(() => true);
          if (stale) continue;
          const text = (await node.innerText().catch(() => '')).replace(/\s+/g, ' ').trim();
          if (text) return text;
        }
      }
    }
    return null;
  }

  /**
   * Wait until the grid stops changing, so reference uploads have finished
   * adding their tiles before the "before" snapshot is taken.
   */
  async waitForGridToSettle({ stableForMs = 3000, timeoutMs = 40000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    let lastSignature = null;
    let stableSince = Date.now();
    let snapshot = await this.snapshotAssets();

    while (Date.now() < deadline) {
      const signature = snapshot.entries.map((entry) => entry.key).join('|');
      if (signature !== lastSignature) {
        lastSignature = signature;
        stableSince = Date.now();
      } else if (Date.now() - stableSince >= stableForMs) {
        return snapshot;
      }
      await sleep(700);
      snapshot = await this.snapshotAssets();
    }
    return snapshot;
  }

  async waitForNewAssets(before, expected, { timeout, excludeNames = [] } = {}) {
    const beforeKeys = new Set(before.entries.map((entry) => entry.key));
    // Reference tiles can appear or re-render after the snapshot; never mistake
    // one for a generated result.
    // Reference tiles are labelled with their filename ("Maya.png"), while a
    // generated still is auto-named after the prompt ("Maya holding perfume
    // bottles"). Match the filename form only: a bare-name substring test threw
    // away real results whenever the prompt happened to name the reference.
    const excluded = excludeNames
      .flatMap((name) => [name, name.replace(/\.[a-z0-9]+$/i, '')])
      .filter(Boolean)
      .map(
        (name) =>
          new RegExp(
            `${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.(png|jpe?g|webp|gif|heic?|mp4|m4v|mov|avi|3gp)\\b`,
            'i',
          ),
      );
    const isExcluded = (entry) => excluded.some((pattern) => pattern.test(entry.text));
    const isReferenceTile = (entry) => entry.uploaded || isExcluded(entry);

    // A reference is kept out of the candidates by its label (an upload is named
    // after its file, a generated still is not) and by byte ownership. There is
    // deliberately no size test: every reference in a refs run renders at the same
    // 1376x768 as a result, so "this tile is the size of a reference" rejected
    // every real result and each item burned the full 300s timeout.

    // Byte ownership, the guarantee Renderly relies on: everything already on the
    // page when this generation starts is hashed, so an upload - or a stale tile
    // that swaps its src and looks new - can never be adopted as the result. Only
    // this baseline seeds ownership: re-hashing during the wait would mark the
    // result itself as seen (a late-mounting reference can push it off index 0).
    await this.noteSeenAssets(before.entries);

    const deadline = Date.now() + (timeout ?? this.timeouts.generationMs);
    const settleMs = expected > 1 ? 15000 : 6000;

    let latest = before;
    let added = [];
    let lastFresh = [];
    let seen = 0;
    let lastGrowthAt = Date.now();

    for (;;) {
      const snapshot = await this.snapshotAssets();
      if (snapshot.entries.length > 0) latest = snapshot;

      // Tiles that changed since the snapshot and are not references.
      const changed = latest.entries.filter(
        (entry) => !beforeKeys.has(entry.key) && !isReferenceTile(entry),
      );
      // A result is served from the finished-asset host, never a grid placeholder.
      const fresh = changed.filter((entry) => isFinalResultUrl(entry.src));
      if (fresh.length > 0) lastFresh = fresh;
      // A result must carry the redo control that only generated tiles have, and
      // its bytes must never have been seen this run. A tile that cannot be
      // positively identified is not accepted - the item times out and is retried
      // rather than silently saving the wrong image.
      const candidates = fresh.filter((entry) => entry.hasImage && entry.canRedo);
      // The newest non-reference candidate is the result. It is not always at
      // index 0: a reference tile can mount after the generation and land above
      // it, which is how a real result was skipped and the item timed out. Entries
      // are in grid order, so the first candidate is the newest one.
      const newest = expected === 1 ? candidates.slice(0, 1) : candidates;
      added = await this.ownNewAssets(newest);

      // A refused generation shows up as a new tile, not as a new image. A page
      // banner only counts when it appeared AFTER the click - a stale one belongs
      // to an earlier item.
      const refusal = changed.find((entry) => entry.failed) ?? null;
      const pageRefusal = refusal ? null : await this.freshAlert(['generationRefusal']);
      const refused = refusal ?? (pageRefusal ? { text: pageRefusal } : null);
      if (refused) {
        const throttled = /unusual activity/i.test(refused.text);
        throw new GenerationError(
          throttled
            ? `Flow refused the generation: "${refused.text}". Google is throttling this account ` +
              '(automated activity detected). Wait before retrying, lower the request rate, and avoid ' +
              'running large batches back to back.'
            : `Flow reported a failed generation: "${refused.text}".`,
          { retryable: !throttled },
        );
      }

      if (added.length > seen) {
        seen = added.length;
        lastGrowthAt = Date.now();
        log.debug(`New assets detected: ${added.length}${expected ? `/${expected}` : ''}`);
      }

      const generating = await this.exists('generatingIndicator', { timeout: 0 });
      const settled =
        added.length >= expected || (added.length > 0 && !generating && Date.now() - lastGrowthAt >= settleMs);
      if (settled) return { ...latest, added };

      if (added.length === 0) {
        const banner = await this.freshAlert(['errorBanner']);
        if (banner) throw new GenerationError(`Flow reported an error while generating: ${banner}`);
      }

      if (Date.now() >= deadline) break;
      await sleep(1500);
    }

    if (added.length > 0) return { ...latest, added };

    // Salvage: the attempt produced a finished tile that could not be claimed
    // before the deadline (a transient byte fetch, or the settle window). Saving
    // it beats failing the item and generating a second copy on retry, which
    // leaves an orphan in the project and wastes a generation.
    //
    // Only a tile that still carries the redo control is salvaged. That control is
    // what proves the tile is a generation rather than a reference, and keeping it
    // mandatory is what makes a wrong save impossible - without it the item times
    // out as before rather than risking a reference being written as the result.
    const salvagePool = lastFresh.filter((entry) => entry.canRedo);
    const salvaged = await this.ownNewAssets(salvagePool.slice(0, Math.max(1, expected)));
    if (salvaged.length > 0) {
      log.warn(
        `Salvaged ${salvaged.length} result(s) that could not be positively identified before the timeout.`,
      );
      return { ...latest, added: salvaged, salvaged: true };
    }

    throw new TimeoutError(
      `No new result tile appeared within ${Math.round((timeout ?? this.timeouts.generationMs) / 1000)}s ` +
        '(detection timeout, not a refusal). Check the open browser window; if the generation finished, ' +
        'calibrate "assetTile".',
    );
  }

  // ---------------------------------------------------------------- download

  async awaitDownload(startIndex, destPath) {
    const deadline = Date.now() + this.timeouts.downloadMs;
    while (Date.now() < deadline) {
      if (this.downloads.length > startIndex) {
        const download = this.downloads[startIndex];
        ensureParent(destPath);
        // Copy the file Playwright already persisted rather than saveAs(), which
        // fails if the originating page has since navigated or closed.
        const source = await download.path().catch(() => null);
        if (source) {
          fs.copyFileSync(source, destPath);
          return true;
        }
        const ok = await download
          .saveAs(destPath)
          .then(() => true)
          .catch(() => false);
        return ok;
      }
      await sleep(250);
    }
    return false;
  }

  async downloadAsset(tileIndex, destPath, { selector }) {
    const start = this.downloads.length;
    const tile = this.page.locator(selector).nth(tileIndex);
    await tile.scrollIntoViewIfNeeded().catch(() => {});
    await tile.hover().catch(() => {});
    await sleep(300);

    const menu = await this.selectors.find(tile, 'assetMenuButton', { timeout: 2500, required: false });
    if (menu) {
      await menu.locator.click().catch(() => {});
      const item = await this.selectors.find(this.page, 'downloadMenuItem', {
        timeout: 4000,
        required: false,
      });
      if (item) {
        await item.locator.click().catch(() => {});
        if (await this.awaitDownload(start, destPath)) return { method: 'menu' };
      }
      await this.page.keyboard.press('Escape').catch(() => {});
    }

    await tile.click().catch(() => {});
    await sleep(1500);
    await this.page.keyboard.press('Control+D').catch(() => {});
    if (await this.awaitDownload(start, destPath)) {
      await this.page.keyboard.press('Escape').catch(() => {});
      return { method: 'shortcut' };
    }
    await this.page.keyboard.press('Escape').catch(() => {});
    return { method: null };
  }

  /**
   * Non-destructive fallback: the tile's <img> points at a signed CDN URL that
   * the browser context can fetch with its own cookies. No UI interaction, so
   * it cannot disturb the page.
   */
  async fetchBytes(src) {
    if (!src || !/^https?:/i.test(src)) return null;
    const response = await this.context.request.get(src).catch(() => null);
    if (!response || !response.ok()) return null;
    const body = await response.body().catch(() => null);
    return body && body.length > 0 ? body : null;
  }

  async fetchAssetBytes(tileIndex, { selector }) {
    const src = await this.page
      .locator(selector)
      .nth(tileIndex)
      .evaluate((node) => {
        const img = node.tagName === 'IMG' ? node : node.querySelector('img');
        return img ? img.currentSrc || img.getAttribute('src') || '' : '';
      })
      .catch(() => '');
    return this.fetchBytes(src);
  }

  /**
   * Record the bytes of tiles already on the page. A src is hashed once per run,
   * so this stays cheap as the grid fills up: only tiles that are new since the
   * previous call cost a request.
   */
  async noteSeenAssets(entries) {
    for (const entry of entries) {
      if (!entry.hasImage || !entry.src || this.seenAssetSrcs.has(entry.src)) continue;
      this.seenAssetSrcs.add(entry.src);
      const bytes = await this.fetchBytes(entry.src);
      if (bytes) this.seenAssetHashes.add(hashBytes(bytes));
    }
  }

  /**
   * Keep only the candidates whose bytes have never been seen this run, and mark
   * them seen. This is what stops a reference upload, or a reused tile, from being
   * saved as a generation.
   */
  async ownNewAssets(entries) {
    const owned = [];
    for (const entry of entries) {
      const bytes = await this.fetchBytes(entry.src);
      if (!bytes) continue;
      const hash = hashBytes(bytes);
      if (this.seenAssetHashes.has(hash)) continue;
      this.seenAssetHashes.add(hash);
      this.seenAssetSrcs.add(entry.src);
      owned.push(entry);
    }
    return owned;
  }

  /**
   * Re-encode an image as PNG using the browser's own canvas, so no image library
   * is needed. Flow only exports JPEG, but jobs routinely ask for .png.
   * Returns false (leaving the caller to keep the original) on any failure.
   */
  async convertToPng(inputPath, outputPath) {
    const bytes = fs.readFileSync(inputPath);
    const mime = mimeForExtension(sniffImageExtension(inputPath));
    const dataUrl = `data:${mime};base64,${bytes.toString('base64')}`;

    const base64 = await this.page
      .evaluate(async (url) => {
        const image = new Image();
        image.src = url;
        await image.decode();
        const canvas = document.createElement('canvas');
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const context = canvas.getContext('2d');
        context.drawImage(image, 0, 0);
        return canvas.toDataURL('image/png').split(',')[1] ?? '';
      }, dataUrl)
      .catch(() => '');

    if (!base64) return false;
    const png = Buffer.from(base64, 'base64');
    // A valid PNG always starts with this signature.
    if (png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') return false;
    fs.writeFileSync(outputPath, png);
    return true;
  }

  // ------------------------------------------------------------------ debug

  async dumpDebug(tag, dirs) {
    const base = path.join(dirs.debugDir, `${slugify(tag)}-${timestampSlug()}`);
    ensureParent(`${base}.png`);
    await this.page.screenshot({ path: `${base}.png`, fullPage: true }).catch(() => {});
    const html = await this.page.content().catch(() => '');
    fs.writeFileSync(`${base}.html`, html, 'utf8');
    return { screenshot: `${base}.png`, html: `${base}.html` };
  }
}
