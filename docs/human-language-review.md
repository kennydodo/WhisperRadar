# Human-language review

After the normal script writer and judge finish, open the production's Script
stage and use **Human-language review**. Save any edits in the script box first:
the process reads the saved script, not unsaved text.

Choose a reviewer and revision writer from the existing webchat sites, their
model controls, and a maximum of 3, 4 or 5 revision rounds. This is manually
started and does not change the normal writer/judge pipeline. Existing webchat
login profiles and transport are reused; there are no API calls.

The reviewer returns exact passages, language problems and suggested fixes.
The writer receives those findings, the full latest script and writing style.
Each reply uses a new chat with full context, keeping roles isolated even when
both use the same site. Prior findings are included in subsequent reviews.

The process stops early on READY. A final review follows the last permitted
revision, so three revision rounds allow up to four reviews. If issues remain,
the result is “Review limit reached”, not approval. Use the existing Stop
web-chat run control to stop; the saved script stays unchanged.

Only a READY candidate replaces script.md, without advancing the production.
The normal readiness scan is refreshed. If the saved script changed during the
run, the candidate is kept separately instead of overwriting the edit. Approval
becomes outdated if the saved script changes afterward. Regenerate or review
any existing audio, subtitles or shots against the revised script as necessary.

Every invocation has its own human_review/<run-id>/ folder containing the
original, reviewer/writer replies, revisions, latest candidate and result.json.
The Script page links the original, report and drafts. human_review.json stores
the latest run's status. Failed or stopped runs do not resume automatically;
starting again uses the current saved script and retains earlier run history.

This reviews natural spoken language, not AI authorship, factual accuracy, or
the original judge's full criteria. Changes to the hook, facts, meaning and
channel voice are discouraged by both prompts, but remain LLM judgments.

Validation: tests/test_human_review.py uses fake transports and temporary
databases; it does not open chat sites or call paid services.
