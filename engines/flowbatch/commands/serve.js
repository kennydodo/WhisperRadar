import { startServer } from '../src/server.js';
import { intFlag } from '../src/lib/args.js';
import { log } from '../src/lib/log.js';

export async function serveCommand({ flags }) {
  const port = intFlag(flags, 'port', 8787);
  const host = typeof flags.host === 'string' ? flags.host : '127.0.0.1';

  startServer({ port, host, open: flags.open === true });

  // Keep the CLI process alive; the HTTP server is the long-running work.
  await new Promise(() => {});
  log.debug('server stopped');
}
