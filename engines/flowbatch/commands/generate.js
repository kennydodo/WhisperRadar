import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { loadJob } from '../src/jobs/load.js';
import { RunState } from '../src/runner/state.js';
import { printPlan, runJob, selectItems } from '../src/runner/run.js';
import { intFlag, listFlag } from '../src/lib/args.js';
import { log } from '../src/lib/log.js';
import { fromRoot } from '../src/lib/paths.js';
import { pauseForEnter } from '../src/lib/prompt.js';

export async function generateCommand({ flags, context, positionals }) {
  const jobPath = typeof flags.job === 'string' ? flags.job : positionals[0];
  if (!jobPath) {
    throw new Error('Missing job file. Usage: npm run generate -- --job config/jobs.example.json');
  }

  const { settings } = context;
  const job = loadJob(jobPath, { settings, repairEncoding: flags['repair-encoding'] === true });

  // Let the caller redirect results without editing the job file.
  if (typeof flags.output === 'string' && flags.output.trim()) {
    job.outputsDir = fromRoot(flags.output.trim());
  }
  if (typeof flags['project-url'] === 'string' && flags['project-url'].trim()) {
    job.projectUrl = flags['project-url'].trim();
  }
  // `--agent off` keeps the prompt-box model / aspect-ratio / output-count
  // controls available, at the cost of the refusal risk that Agent OFF carries
  // on a session Flow already distrusts.
  if (typeof flags.agent === 'string') {
    const enabled = !['off', 'false', 'no', '0'].includes(flags.agent.trim().toLowerCase());
    job.defaults = { ...(job.defaults ?? {}), agent: enabled };
    for (const item of job.items) item.agent = enabled;
  }

  const options = {
    only: listFlag(flags, 'only'),
    limit: intFlag(flags, 'limit', 0),
    resume: flags.resume !== false,
    dryRun: flags['dry-run'] === true,
    failFast: flags['fail-fast'] === true,
    pauseOnError: flags['pause-on-error'] === true,
    dumpOnError: flags['dump-on-error'] !== false,
    cooldownSeconds: flags.cooldown === undefined ? undefined : intFlag(flags, 'cooldown', 180),
    delayBetweenItemsMs: flags.delay === undefined ? undefined : intFlag(flags, 'delay', 0) * 1000,
    maxCooldowns: flags['max-cooldowns'] === undefined ? undefined : intFlag(flags, 'max-cooldowns', 10),
    maxConsecutiveFailures:
      flags['max-consecutive-failures'] === undefined
        ? undefined
        : intFlag(flags, 'max-consecutive-failures', 3),
  };

  const state = RunState.open(RunState.pathFor(settings.dirs.stateDir, job.name), {
    jobName: job.name,
    jobPath: job.jobPath,
    items: job.items,
    projectUrl: job.projectUrl,
  });

  if (state.projectChanged) {
    log.warn(
      'The Flow project changed since the last run, so the recorded progress does not apply to it. ' +
        `Every item will run again in ${state.projectChanged.to}`,
    );
    state.save();
  }

  const stale = state.clearStaleRunning();
  if (stale.length > 0) {
    log.info(`Reset ${stale.length} item(s) left mid-run by an interrupted batch: ${stale.join(', ')}`);
    state.save();
  }

  const missing = state.clearMissingFiles();
  if (missing.length > 0) {
    log.warn(
      `${missing.length} item(s) were marked done but their files are gone; they will run again: ` +
        missing.join(', '),
    );
    state.save();
  }

  if (flags['reset-state'] === true) {
    state.reset(job.items.map((item) => item.id));
    state.save();
    log.info('State reset; every item will run again.');
  }

  if (options.dryRun) {
    const planned = selectItems(job.items, {
      only: options.only,
      limit: options.limit,
      resume: options.resume,
      state,
    });
    printPlan(job, planned, settings);
    log.info('Dry run complete; nothing was generated.');
    return 0;
  }

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({
    page,
    context: browserContext,
    selectors: context.selectors,
    settings,
  });

  try {
    const result = await runJob({ job, driver, state, settings, options });
    if (flags['keep-open'] === true) {
      await pauseForEnter('Browser left open (--keep-open).');
    }
    return result.failed > 0 ? 1 : 0;
  } finally {
    await closeSession(browserContext);
  }
}
