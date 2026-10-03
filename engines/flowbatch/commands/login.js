import { launchSession, closeSession } from '../src/browser/session.js';
import { FlowDriver } from '../src/flow/driver.js';
import { log } from '../src/lib/log.js';
import { pauseForEnter } from '../src/lib/prompt.js';
import { sleep } from '../src/lib/time.js';

/**
 * How long the signed-in state must hold before the window is allowed to close.
 * Google often re-challenges a fresh automated profile, and closing too early
 * loses the session.
 */
const STABLE_MS = 12000;

export async function loginCommand({ flags, context }) {
  const { settings } = context;
  const keepOpen = flags['keep-open'] === true;
  const confirm = flags.confirm === true;

  const { context: browserContext, page } = await launchSession(settings);
  const driver = new FlowDriver({ page, context: browserContext, selectors: context.selectors, settings });

  try {
    await driver.goto();

    log.heading('Sign in to Google Flow');
    log.raw('A browser window is open. Sign in with the Google account that has Flow access.');
    log.raw('If Google shows "Verify it\'s you", complete that step in the browser window.');
    log.raw(`The window closes on its own once the session holds steady for ${STABLE_MS / 1000}s.`);

    const deadline = Date.now() + settings.timeouts.loginMs;
    let state = 'unknown';
    let stableSince = null;
    let warnedChallenge = false;

    while (Date.now() < deadline) {
      state = await driver.looksSignedIn();

      if (state === 'in') {
        if (stableSince === null) {
          stableSince = Date.now();
          log.info('Signed-in page detected; confirming it holds…');
        }
        if (Date.now() - stableSince >= STABLE_MS) break;
      } else {
        stableSince = null;
        if (state === 'challenge' && !warnedChallenge) {
          warnedChallenge = true;
          log.warn('Google is asking for extra verification — complete "Verify it\'s you" in the browser window.');
        }
      }

      await sleep(2000);
    }

    const signedIn = state === 'in' && stableSince !== null;

    if (signedIn) {
      // Re-enter Flow to make sure a challenge is not waiting on the next launch.
      log.info('Re-opening Flow to confirm the session survives a fresh navigation…');
      await driver.goto();
      const recheck = await driver.looksSignedIn();
      if (recheck === 'in') {
        log.ok(`Signed in and stable. The session is stored in ${settings.dirs.profileDir}`);
        log.info('Next: `npm run discover` to calibrate selectors, then `npm run doctor --live`.');
      } else {
        log.warn(
          `The session did not survive a fresh navigation (state=${recheck}). ` +
            'Re-run `npm run login` and complete any Google verification prompt before the window closes.',
        );
        if (confirm || keepOpen) await pauseForEnter('Finish the verification in the browser, then press Enter.');
        return 1;
      }
    } else {
      log.warn(`Sign-in was not confirmed (last state=${state}). Re-run \`npm run login\` and complete the Google flow.`);
      return 1;
    }

    if (confirm || keepOpen) await pauseForEnter('Browser left open.');
    return 0;
  } finally {
    await closeSession(browserContext);
  }
}
