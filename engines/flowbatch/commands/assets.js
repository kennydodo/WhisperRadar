import fs from 'node:fs';
import path from 'node:path';

import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { log } from '../src/lib/log.js';
import { ROOT } from '../src/lib/paths.js';
import { sleep, timestampSlug } from '../src/lib/time.js';

/**
 * List what the project's asset library holds - READ ONLY, nothing generated,
 * uploaded or renamed.
 *
 *   npm run assets -- --project-url <url> [--search maya]
 *
 * Why it exists: `prepare` can only call a reference "reused" when an asset's
 * title equals its name, and the refs stage cannot name the tiles it generates
 * (the `nameAssets` job key is not implemented). To decide how to stop
 * re-uploading references we need to SEE how Flow titles generated tiles. This
 * writes debug/assets-<time>.json (title, aria-label, alt, title attribute and
 * a snippet of the HTML of every item) plus a screenshot.
 */
export async function assetsCommand({ flags, context }) {
  const { settings } = context;
  const projectUrl =
    typeof flags['project-url'] === 'string' && flags['project-url'].trim()
      ? flags['project-url'].trim()
      : settings.projectUrl;
  if (!projectUrl) throw new Error('Missing --project-url <url>.');
  const search = typeof flags.search === 'string' ? flags.search : '';

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({
    page,
    context: browserContext,
    selectors: context.selectors,
    settings,
  });
  try {
    await driver.goto();
    await driver.openProject(projectUrl);
    await driver.openAssetLibrary();
    const box = await driver.find('assetPickerSearch', { timeout: 8000, required: false });
    if (box) {
      await box.locator.fill(search);
      await sleep(1800);
    }
    const stamp = timestampSlug();
    const dir = path.join(ROOT, 'debug');
    fs.mkdirSync(dir, { recursive: true });
    await page.screenshot({ path: path.join(dir, `assets-${stamp}.png`) }).catch(() => {});

    const items = [];
    for (const selector of context.selectors.candidates('assetPickerItem')) {
      const found = page.locator(selector);
      const count = await found.count().catch(() => 0);
      if (count === 0) continue;
      for (let i = 0; i < count; i += 1) {
        const item = found.nth(i);
        const info = await item
          .evaluate((node) => ({
            text: (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
            title: node.querySelector('.asset-title')?.textContent?.trim() ?? '',
            ariaLabel: node.getAttribute('aria-label') ?? '',
            titleAttr: node.getAttribute('title') ?? '',
            imgAlt: node.querySelector('img')?.getAttribute('alt') ?? '',
            imgSrc: (node.querySelector('img')?.getAttribute('src') ?? '').slice(0, 120),
            html: node.outerHTML.slice(0, 1200),
          }))
          .catch(() => null);
        if (info) items.push({ index: i, ...info });
      }
      break;
    }
    const file = path.join(dir, `assets-${stamp}.json`);
    fs.writeFileSync(file, JSON.stringify({ projectUrl, search, count: items.length, items }, null, 2));
    log.ok(`${items.length} asset(s) listed -> ${path.relative(ROOT, file)}`);
    for (const item of items.slice(0, 40)) log.info(`  [${item.index}] title="${item.title}" alt="${item.imgAlt}" aria="${item.ariaLabel}"`);
    await driver.closeAssetLibrary().catch(() => {});
    return 0;
  } finally {
    await closeSession(browserContext);
  }
}
