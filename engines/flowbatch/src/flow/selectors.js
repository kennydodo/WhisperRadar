import { sleep } from '../lib/time.js';
import { SelectorError } from '../lib/errors.js';

/**
 * Resolves semantic element names (from config/selectors.json) to Playwright
 * locators by trying each candidate selector in order.
 */
export class SelectorSet {
  constructor(map) {
    this.map = map ?? {};
  }

  keys() {
    return Object.keys(this.map);
  }

  candidates(key) {
    const value = this.map[key];
    if (!value) return [];
    return (Array.isArray(value) ? value : [value]).filter(
      (entry) => typeof entry === 'string' && entry.trim().length > 0,
    );
  }

  has(key) {
    return this.candidates(key).length > 0;
  }

  /**
   * @returns {Promise<{locator: import('playwright').Locator, selector: string}|null>}
   */
  async find(root, key, options = {}) {
    const {
      timeout = 8000,
      requireVisible = true,
      requireEnabled = false,
      required = true,
      pollMs = 250,
    } = options;

    const candidates = this.candidates(key);
    if (candidates.length === 0) {
      if (!required) return null;
      throw new SelectorError(key, candidates, ['No candidates configured in config/selectors.json.']);
    }

    const deadline = Date.now() + Math.max(0, timeout);
    const details = [];

    for (;;) {
      for (const selector of candidates) {
        try {
          const locator = root.locator(selector).first();
          if ((await locator.count()) === 0) continue;
          if (requireVisible && !(await locator.isVisible().catch(() => false))) continue;
          if (requireEnabled && !(await locator.isEnabled().catch(() => false))) continue;
          return { locator, selector };
        } catch (error) {
          details.push(`${selector}: ${String(error.message ?? error).split('\n')[0]}`);
        }
      }

      if (Date.now() >= deadline) break;
      await sleep(pollMs);
    }

    if (!required) return null;
    throw new SelectorError(key, candidates, details);
  }

  async exists(root, key, options = {}) {
    const found = await this.find(root, key, {
      ...options,
      timeout: options.timeout ?? 0,
      required: false,
    });
    return found !== null;
  }

  async count(root, key) {
    for (const selector of this.candidates(key)) {
      const count = await root.locator(selector).count().catch(() => 0);
      if (count > 0) return count;
    }
    return 0;
  }
}
