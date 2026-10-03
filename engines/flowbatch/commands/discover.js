import fs from 'node:fs';
import path from 'node:path';

import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { log } from '../src/lib/log.js';
import { ROOT, slugify } from '../src/lib/paths.js';
import { timestampSlug, sleep } from '../src/lib/time.js';
import { intFlag, repeatFlag } from '../src/lib/args.js';

/**
 * Runs inside the page. Returns a flat inventory of interactive elements plus
 * suggested Playwright selectors for each one.
 */
function collectInPage() {
  const MAX_TEXT = 140;
  const truncate = (value) => (value ?? '').replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT);

  const isVisible = (element) => {
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(element);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };

  const escapeRegex = (value) => value.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');

  const cssPath = (element) => {
    const testid = element.getAttribute('data-testid');
    if (testid) return `[data-testid="${testid}"]`;
    if (element.id) return `#${element.id}`;

    const parts = [];
    let node = element;
    while (node && node.nodeType === 1 && parts.length < 5) {
      let part = node.tagName.toLowerCase();
      const classes = (node.getAttribute('class') ?? '').split(/\s+/).filter(Boolean).slice(0, 2);
      if (classes.length > 0) part += `.${classes.join('.')}`;
      const parent = node.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
        if (sameTag.length > 1) part += `:nth-of-type(${sameTag.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };

  const suggestions = (element) => {
    const out = [];
    const testid = element.getAttribute('data-testid');
    if (testid) out.push(`[data-testid="${testid}"]`);

    const aria = element.getAttribute('aria-label');
    if (aria) out.push(`[aria-label="${aria}"]`);

    const text = truncate(element.innerText || element.textContent);
    const role =
      element.getAttribute('role') ?? (element.tagName === 'BUTTON' ? 'button' : element.tagName === 'A' ? 'link' : null);

    if (role && text && text.length <= 40) out.push(`role=${role}[name="${text}" i]`);
    if (text && text.length <= 40) out.push(`text=/^\\s*${escapeRegex(text)}\\s*$/i`);

    out.push(cssPath(element));
    return [...new Set(out)];
  };

  const selector = 'button, a[href], [role], input, textarea, select, [contenteditable="true"], [data-testid]';
  const nodes = Array.from(document.querySelectorAll(selector));

  const elements = nodes
    .map((element) => ({
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute('role'),
      type: element.getAttribute('type'),
      ariaLabel: element.getAttribute('aria-label'),
      placeholder: element.getAttribute('placeholder'),
      testid: element.getAttribute('data-testid'),
      id: element.id || null,
      accept: element.getAttribute('accept'),
      contenteditable: element.getAttribute('contenteditable'),
      text: truncate(element.innerText || element.textContent),
      visible: isVisible(element),
      disabled: element.disabled === true || element.getAttribute('aria-disabled') === 'true',
      rect: (() => {
        const box = element.getBoundingClientRect();
        return { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height) };
      })(),
      suggestions: suggestions(element),
    }))
    .filter((entry) => entry.visible || entry.testid || entry.tag === 'input' || entry.contenteditable === 'true');

  const fileInputs = Array.from(document.querySelectorAll('input[type=file]')).map((element) => ({
    accept: element.getAttribute('accept'),
    multiple: element.multiple === true,
    hidden: !isVisible(element),
    testid: element.getAttribute('data-testid'),
    html: element.outerHTML.slice(0, 300),
  }));

  const testids = [
    ...new Set(
      Array.from(document.querySelectorAll('[data-testid]'))
        .map((element) => element.getAttribute('data-testid'))
        .filter(Boolean),
    ),
  ].sort();

  return {
    url: location.href,
    title: document.title,
    counts: {
      matched: nodes.length,
      buttons: document.querySelectorAll('button').length,
      contenteditable: document.querySelectorAll('[contenteditable="true"]').length,
      images: document.querySelectorAll('img').length,
    },
    elements,
    fileInputs,
    iframes: Array.from(document.querySelectorAll('iframe')).map((frame) => frame.src),
    testids,
  };
}

function toMarkdown(report) {
  const lines = [
    `# Flow DOM report`,
    '',
    `- URL: ${report.url}`,
    `- Title: ${report.title}`,
    `- Interactive nodes: ${report.counts.matched} (buttons: ${report.counts.buttons}, images: ${report.counts.images})`,
    '',
    `## data-testid values (${report.testids.length})`,
    '',
    ...report.testids.map((value) => `- \`${value}\``),
    '',
    `## File inputs (${report.fileInputs.length})`,
    '',
    ...report.fileInputs.map(
      (input) =>
        `- accept=\`${input.accept ?? ''}\` multiple=${input.multiple} hidden=${input.hidden} testid=\`${input.testid ?? ''}\``,
    ),
    '',
    `## Iframes (${report.iframes.length})`,
    '',
    ...report.iframes.map((src) => `- ${src}`),
    '',
    `## Visible interactive elements`,
    '',
    '| tag | role | text | aria-label | testid | best selector |',
    '| --- | --- | --- | --- | --- | --- |',
  ];

  const escapeCell = (value) => String(value ?? '').replace(/\|/g, '\\|').slice(0, 60);

  for (const element of report.elements.filter((entry) => entry.visible)) {
    lines.push(
      `| ${escapeCell(element.tag)} | ${escapeCell(element.role)} | ${escapeCell(element.text)} | ` +
        `${escapeCell(element.ariaLabel)} | ${escapeCell(element.testid)} | \`${escapeCell(element.suggestions[0])}\` |`,
    );
  }

  lines.push('', `## Hidden / special elements`, '');
  for (const element of report.elements.filter((entry) => !entry.visible)) {
    lines.push(
      `- \`${element.tag}\` type=\`${element.type ?? ''}\` accept=\`${element.accept ?? ''}\` ` +
        `testid=\`${element.testid ?? ''}\` suggestions: ${element.suggestions.map((s) => `\`${s}\``).join(', ')}`,
    );
  }

  return `${lines.join('\n')}\n`;
}

export async function discoverCommand({ flags, context }) {
  const { settings } = context;
  const waitSeconds = intFlag(flags, 'wait', 8);
  const keepOpen = flags['keep-open'] === true;

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({ page, context: browserContext, selectors: context.selectors, settings });

  try {
    await driver.goto();

    if (typeof flags.navigate === 'string') {
      log.info(`Navigating to ${flags.navigate}`);
      await page.goto(flags.navigate, { waitUntil: 'domcontentloaded' }).catch((error) => {
        log.warn(`Navigation warning: ${error.message.split('\n')[0]}`);
      });
    }

    const state = await driver.looksSignedIn();
    log.info(`Sign-in state: ${state}`);

    log.info(`Waiting ${waitSeconds}s for the page to settle…`);
    await sleep(waitSeconds * 1000);

    if (typeof flags.agent === 'string') {
      const enabled = flags.agent.toLowerCase() === 'on';
      const ok = await driver.ensureAgentMode(enabled);
      log.info(`Agent mode -> ${enabled ? 'on' : 'off'} (${ok ? 'ok' : 'failed'})`);
    }

    const uploads = repeatFlag(flags, 'upload').map((entry) =>
      path.isAbsolute(entry) ? entry : path.resolve(ROOT, entry),
    );
    if (uploads.length > 0) {
      for (const file of uploads) {
        if (!fs.existsSync(file)) throw new Error(`--upload file not found: ${file}`);
      }
      log.info(`Attaching ${uploads.length} reference file(s) via the Add ingredients menu…`);
      const confirmed = await driver.addReferences(
        uploads.map((file) => ({ name: path.basename(file, path.extname(file)), path: file })),
        { mode: 'upload' },
      );
      log.info(`References confirmed in the prompt box: ${confirmed}`);
    }

    const clicks = repeatFlag(flags, 'click');
    for (const selector of clicks) {
      log.info(`Clicking ${selector}`);
      const target = page.locator(selector).first();
      if ((await target.count().catch(() => 0)) === 0) {
        log.warn(`  no element matched ${selector}`);
        continue;
      }
      const clicked = await target
        .click({ timeout: 8000 })
        .then(() => true)
        .catch(() => false);
      if (!clicked) {
        // Overlays (e.g. the cookie banner) can intercept the pointer; force it.
        const forced = await target
          .click({ timeout: 5000, force: true })
          .then(() => true)
          .catch((error) => {
            log.warn(`  click failed: ${String(error.message).split('\n')[0]}`);
            return false;
          });
        if (forced) log.debug('  clicked with force after a normal click timed out.');
      }
      await sleep(2000);
    }
    if (clicks.length > 0) await sleep(1500);

    const report = await page.evaluate(collectInPage);
    const stamp = timestampSlug();
    const base = path.join(settings.dirs.discoverDir, `flow-${stamp}`);

    fs.mkdirSync(settings.dirs.discoverDir, { recursive: true });
    fs.writeFileSync(`${base}.json`, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
    fs.writeFileSync(`${base}.md`, toMarkdown(report), 'utf8');
    await page.screenshot({ path: `${base}.png`, fullPage: true }).catch(() => {});

    if (flags.html) {
      const html = await page.content().catch(() => '');
      fs.writeFileSync(`${base}.html`, html, 'utf8');
    }

    for (const selector of repeatFlag(flags, 'dump')) {
      const locator = page.locator(selector);
      const count = await locator.count().catch(() => 0);
      const parts = [];
      for (let index = 0; index < Math.min(count, 10); index += 1) {
        const html = await locator
          .nth(index)
          .evaluate((node) => node.outerHTML)
          .catch(() => '');
        parts.push(`<!-- match ${index + 1} of ${count} for ${selector} -->\n${html}`);
      }
      const file = `${base}-dump-${slugify(selector, 'selector')}.html`;
      fs.writeFileSync(file, `${parts.join('\n\n')}\n`, 'utf8');
      log.ok(`Dumped ${count} match(es) -> ${path.relative(ROOT, file)}`);
    }

    log.ok(`Report written: ${path.relative(ROOT, `${base}.md`)}`);
    log.raw(`  json       : ${path.relative(ROOT, `${base}.json`)}`);
    log.raw(`  screenshot : ${path.relative(ROOT, `${base}.png`)}`);
    log.raw(`  testids    : ${report.testids.length}`);
    log.raw(`  fileInputs : ${report.fileInputs.length}`);

    if (report.testids.length > 0) {
      log.raw(`\nFirst 40 data-testid values:\n${report.testids.slice(0, 40).map((v) => `  ${v}`).join('\n')}`);
    }

    if (keepOpen) {
      const { pauseForEnter } = await import('../src/lib/prompt.js');
      await pauseForEnter('Browser left open (--keep-open).');
    }
    return 0;
  } finally {
    await closeSession(browserContext);
  }
}
