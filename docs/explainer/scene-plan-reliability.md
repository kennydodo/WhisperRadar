# Scene-plan review and promotion

The scene-plan writer and judge share the same compiled renderer contract. A
plan that can be built is technically valid, but that is not editorial approval.

## Revision loop

1. The writer proposes a complete plan using narration anchors.
2. The checker validates anchors, component constraints, counts and timing.
3. Corrections return complete replacements for the named scene IDs only. The
   app merges those scenes locally; unchanged scenes need not be retransmitted.
   Unrelated edits, duplicate IDs and new IDs are rejected. Boundary errors may
   also open the adjacent scenes for correction. Global feedback opens the plan.
4. The judge receives complete scene narration and compiled motion parameters,
   including scene-relative timing and footers. It judges that specification,
   rather than inferring the animation from labels alone.
5. Promotion requires a finite numeric score of at least 8/10, explicit boolean
   `pass: true`, empty faults/fixes lists and no blocking checker faults.

The loop does not call automatic semantic simplification or delete faulty scenes
to obtain a technical pass. The writer must make the explanation fit without
discarding its meaning. Decorative icons may be removed when adding required
spoken terminology, but explanatory labels and scenes are retained.

## Saved state

- `plan_candidate.json` and `plan_candidate_review.json` retain the best candidate.
- `plan_review.json` reports `approved` or `needs_revision`, the candidate digest,
  checker findings, judge feedback and the review basis.
- Only an approved result replaces `plan.json`; the previous plan is backed up.
- An unapproved run raises an actionable needs-revision result and does not advance
  the production. Existing `plan.json` is preserved byte for byte.
- Stop, manual plan edits or changed subtitles prevent promotion.
- Editing an approved plan invalidates its displayed approval.

The judge reviews the compiled specification, not rendered pixels. The UI says
that a visual preview is still needed; it does not claim a video was watched.
Review representative start, middle and end frames and a short motion preview
before a full production render. This change does not start live model calls or
render a production automatically.

## Renderer regressions

Pipeline schedules use absolute scene milliseconds. Narration holds remain fixed
while task durations fit within the available animation window. A default window
expands to include late stage anchors when the scene has room; an explicit window
that cannot accommodate an anchor is rejected. Worker queues use their actual
column count when calculating vertical space. Hierarchy cells within one level
appear together, and four-level diagrams leave room for decoder/result text.

Windows cancellation has bounded tree-kill and wait deadlines, with a direct kill
of the owned process if taskkill is unavailable. It never selects other renders.

## Verification

Offline regressions cover failed/malformed approvals, candidate preservation,
targeted scene patches, unrelated-edit rejection, concurrent manual edits, stale
approval, late stage timing, task dependencies and cancellation. Node scheduler
tests exercise 1/4/8 workers across eight stages with 24 queued tasks. Production
115 is read as a regression case only; it is not rewritten by these checks.
