# Optional Google Flow accounts

Settings > Production & images > Enable multiple Flow accounts is OFF by default.
Save the setting to reveal sign-in buttons. Account 1 retains the configured
FlowBatch profile and runtime state. Account 2 has an independent persistent
browser profile and runtime directories under the database folder's
`flow_accounts/2/`. Sign into your own second Google account manually; this
feature does not create Google accounts. Close other manually opened FlowBatch
browsers before using a profile here. Enable the feature when Flow jobs are idle.

Choose the global default, optionally override it in My Channels, or choose an
account on the production page before starting references or another Flow job.
An account selector never moves an existing Flow project between accounts.
Productions with a stored project or legacy Flow sidecar remain on Account 1.
The production's `flow_account.json` records its selection and becomes immutable
when the first Flow operation starts. Keep that file with a moved production.
Changing defaults only affects productions that have no selection/project yet.
Account 2 never inherits the legacy global project URL; a channel or explicitly
supplied URL must belong to the selected account.

References, prepare, stills, gallery recovery and motion all use the pinned
account. A file lock serializes operations and sign-in per account, including
across app processes that share the same database folder. Other jobs for that
account wait and can be cancelled. A different account may run simultaneously.
The existing scheduler still allows one job per own channel: parallel productions
should belong to different channels. Stop/kill remains scoped to a production.
Throttling/recovery retains the original account; it never rotates accounts.
Do not run another standalone FlowBatch process on an app-owned profile while
WhisperRadar is using it: standalone processes do not participate in these locks.

Disabling the setting restores legacy CLI arguments and hides account controls.
Account 1 can still resume using its legacy state. Account 2 productions require
re-enabling the setting; they never silently run with Account 1's login. Existing
in-flight operations retain their account until they finish or are stopped.
Enabled jobs pass their upscale tier to their own CLI process instead of writing
the shared upscale configuration. Local post-download upscaling is unchanged.

Python changes require an app restart after merging. An explicitly configured
external FlowBatch checkout must also contain the new `--runtime-dir` and
`--upscale-tier` CLI support; the bundled engine is updated in this branch.
Do not start this worktree against the production database to test the feature.
Offline tests use disposable databases/profiles and mock browser subprocesses.
A real two-account sign-in/generation smoke test remains to be done after merge
and after the current production has finished.

## Validation in the isolated worktree

- 91 targeted Python tests passed (accounts, recovery, reference generation,
  resolution/upscale pairing, image stop behavior, channel import/export).
- 209 FlowBatch tests passed with local-server tests excluded. The new runtime
  and process-local tier options also passed a CLI dry run without generation.
- Broader local-server tests encountered localhost EACCES restrictions; Windows
  process-tree kill tests encountered process-control restrictions in this
  sandbox. Those checks are not claimed as passing. No production app/browser
  was restarted or used for validation.
