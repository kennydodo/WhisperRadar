# Script warnings and targeted fixes

Scripts are scanned locally after generation/save and when an externally edited script is opened.
Unresolved warnings pause audio generation until corrected, cleared as false alarms by AI review,
or explicitly ignored for that version.
The scanner is heuristic: it does not establish factual accuracy or validate arithmetic.

## Fix workflow

1. Select **Codex CLI** or **Claude Code** above the warning list.
   The browser remembers this selection. Discovery checks PATH and standard per-user install locations,
   including the Codex desktop app's bundled executable. Install and authenticate the CLI on each PC.
2. Click **Fix this — preview** next to an issue, or **Fix all warnings — preview**.
   Fix all excludes informational CTAs. Individual CTAs can be explicitly selected.
3. A background job sends full script context and the selected findings to the provider.
   The page checks status every five seconds; manual refresh is also available.
4. Review the before/after passages and notes. **Accept changes** applies the preview;
   **Reject preview** leaves the script untouched. No safe proposal means no changes.

The provider may only propose replacements for the flagged lines. The server validates line
numbers and exact original text, rejects duplicate/unselected edits, and rejects stale previews
if the script changed. Uncertain facts must be qualified without inventing evidence or left for
human review. A flag can remain after a fix; inspect it before ignoring it.

Each selected finding receives a structured verdict: acceptable in the original context or unresolved.
Confirmed false alarms stop showing red and no longer pause audio. Missing evidence and uncertain
claims remain warnings. No proposed edits alone never clears warnings. Reviews expire on script edits.
When no edits are proposed, the panel offers **Dismiss review** rather than a disabled Accept button.

Accepted changes create `versions/script-before-fix-<id>.md`, save atomically, and rescan.
Existing audio and downstream assets are not regenerated; regenerate them when their script changes.
Only one fix job per production runs at once. CLI jobs time out after five minutes. Interrupted jobs
are reported as failed after a server restart and can be retried.

## Providers

Settings > LLM > Codex & Claude Code configures both CLIs: enabled state, executable path
(blank means auto-detect), optional model override, default fix provider, and subscription sign-in
checks. Checks use the CLI's auth-status command and never generate content or store account emails.
Save settings before checking connection. Each card shows a PowerShell sign-in command.
Script fixes read these settings and confirm subscription auth before generating; API credentials
are removed from the child environment. Models default to each CLI's default when left blank.

- Codex: `codex exec`, read-only sandbox, ephemeral session, structured JSON schema.
  User configuration is skipped for this invocation; saved CLI authentication is reused.
- Claude Code: print mode with structured JSON, no tools, no MCP servers, no saved session.

Codex CLI is the default. Old browser selections of the configured script LLM reset to Codex.
Configured API providers are not available for script fixes, and CLI failures never fall back to them.

CLI jobs run from an isolated temporary working directory; script content is provided on stdin.
They do not receive production paths or write access to productions from WhisperRadar.
The CLI starts locally but model requests are online and consume the account's applicable limits
or billing. WhisperRadar does not install or authenticate either CLI automatically.

No real provider calls are made by the tests. CLI invocation/response parsing, scope validation,
preview/accept/reject, backups, stale handling and Flask routes are tested with mocked providers.

Script and shotlist form saves normalize Windows line endings before writing, avoiding CRCRLF
duplication. Script versions and accepted replacements preserve intentional paragraph spacing.
