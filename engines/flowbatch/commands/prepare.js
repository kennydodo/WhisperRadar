import fs from 'node:fs';
import path from 'node:path';

import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { loadJob } from '../src/jobs/load.js';
import { log } from '../src/lib/log.js';
import { sleep } from '../src/lib/time.js';

/**
 * Prepare a Flow project for a job, without generating anything.
 *
 * This is the FlowBatch half of a frozen contract with WhisperRadar:
 *   1. the report file (--report) is the interface, written ATOMICALLY as soon
 *      as the project exists - not at exit - so a later crash still leaves the
 *      project URL behind;
 *   2. FLOW_PROJECT_URL=<url> is printed un-prefixed on stdout for manual runs;
 *   3. every reference is reported as uploaded | reused | missing, which is what
 *      lets the caller switch the job to refMode "assets" (attach by name, never
 *      upload, no duplicate project assets).
 *
 * The report is optional and its absence is never fatal: the caller treats a
 * failure, a missing command or an unreadable report as "no project yet" and
 * carries on with the stored URL.
 */

const SCHEMA_VERSION = 1;

/** A reader must never catch a half-written report, so write then rename. */
function writeReport(file, report) {
  const resolved = path.resolve(file);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  const temp = `${resolved}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  fs.renameSync(temp, resolved);
}

function projectIdFrom(url) {
  const match = /\/project\/([0-9a-fA-F-]{8,})/.exec(String(url ?? ''));
  return match ? match[1] : null;
}

/** The unique `{ name, path }` references the job declares, in first-seen order. */
export function uniqueRefs(job) {
  const seen = new Map();
  for (const item of job.items ?? []) {
    for (const ref of item.refs ?? []) {
      if (!ref || !ref.name || seen.has(ref.name)) continue;
      seen.set(ref.name, { name: ref.name, path: ref.path ?? null });
    }
  }
  return [...seen.values()];
}

/**
 * Resolve each reference against the project, best-effort.
 *
 * One reference that cannot be attached must not abort the whole command: the
 * project URL has already been reported before this runs, and the generation
 * stage attaches references itself. A Playwright click can time out on a fresh
 * project while the composer is still settling (`element is not enabled`), so the
 * failure is logged and the reference is reported `missing` - which tells the
 * caller to upload it at generation time instead.
 */
export async function resolveReferenceStatuses(driver, refs, { fileExists = fs.existsSync } = {}) {
  const results = [];
  for (const ref of refs) {
    let status = 'missing';
    try {
      if (await driver.galleryHasAsset(ref.name)) {
        status = 'reused';
      } else if (ref.path && fileExists(ref.path)) {
        await driver.attachUploadedFiles([ref.path]);
        status = 'uploaded';
      }
    } catch (error) {
      log.warn(
        `Could not prepare reference "${ref.name}": ${String(error?.message ?? error).split('\n')[0]}. ` +
          'Reporting it missing; the generation stage will try again.',
      );
    }
    results.push({ name: ref.name, kind: 'image', status, path: ref.path ?? null });
  }
  return results;
}

export async function prepareCommand({ flags, context, positionals }) {
  const jobPath = typeof flags.job === 'string' ? flags.job : positionals[0];
  if (!jobPath) {
    throw new Error('Missing job file. Usage: npm run prepare -- --job <job.json> --report <path>');
  }

  const { settings } = context;
  const job = loadJob(jobPath, { settings, repairEncoding: flags['repair-encoding'] === true });
  if (typeof flags['project-url'] === 'string' && flags['project-url'].trim()) {
    job.projectUrl = flags['project-url'].trim();
  }

  const reportPath =
    typeof flags.report === 'string' && flags.report.trim() ? path.resolve(flags.report.trim()) : null;

  const report = {
    schemaVersion: SCHEMA_VERSION,
    projectUrl: null,
    projectId: null,
    project: job.project ?? null,
    created: false,
    jobName: job.name ?? null,
    preparedAt: new Date().toISOString(),
    refs: [],
  };

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({
    page,
    context: browserContext,
    selectors: context.selectors,
    settings,
  });

  try {
    await driver.goto();
    const signIn = await driver.looksSignedIn();
    if (signIn === 'out') {
      throw new Error('Not signed in. Run `npm run login` with this profile first.');
    }

    let created = false;
    if (job.projectUrl) {
      await driver.openProject(job.projectUrl);
    } else {
      // No URL for this job: Flow's "most recent project" must never be adopted
      // silently, so this is said out loud before a project is opened or made.
      log.warn(
        `No projectUrl for "${job.name}"; opening "${job.project ?? 'a new project'}" from the project list, ` +
          'or creating one.',
      );
      const opened = await driver.ensureProject(job.project);
      created = Boolean(opened?.created);
    }

    const url = page.url();
    report.projectUrl = url;
    report.projectId = projectIdFrom(url);
    report.created = created;
    report.project = job.project ?? (await driver.projectName().catch(() => null));

    // Written before the reference work so the URL survives a crash later on.
    if (reportPath) writeReport(reportPath, report);
    process.stdout.write(`FLOW_PROJECT_URL=${url}\n`);
    log.info(`Flow project ${created ? 'created' : 'opened'}: ${url}`);

    const refs = uniqueRefs(job);
    if (refs.length === 0) {
      log.info('This job declares no references.');
    } else {
      // Upload through the same paths the generation stage uses. A fresh upload is
      // provisional: Flow shows it in the picker long before it is committed, and a
      // large file can look present and then be gone by the time the session ends.
      report.refs = await resolveReferenceStatuses(driver, refs);
      const attemptedUpload = report.refs.some((entry) => entry.status === 'uploaded');

      if (attemptedUpload) {
        // Verify durability in a fresh page load, so the report never claims a ref
        // the generation stage cannot find - which is what made `generate` report
        // "not in the project's assets" for refs prepare had called uploaded.
        await driver.detachAllReferences().catch(() => {});
        await sleep(2500);
        await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
        await driver.waitForPromptBox().catch(() => {});
        for (const entry of report.refs) {
          if (entry.status !== 'uploaded') continue;
          if (!(await driver.galleryHasAsset(entry.name))) {
            entry.status = 'missing';
            log.warn(
              `Reference ${entry.name} did not survive in the project; reporting it missing so the ` +
                'generation stage uploads it instead.',
            );
          }
        }
        // Reloading can leave the composer in any state; this run generates nothing.
        await driver.detachAllReferences().catch(() => {});
      } else {
        await driver.detachAllReferences().catch(() => {});
      }

      for (const entry of report.refs) log.info(`Reference ${entry.name}: ${entry.status}`);
      if (reportPath) writeReport(reportPath, report);
    }

    return 0;
  } finally {
    await closeSession(browserContext);
  }
}
