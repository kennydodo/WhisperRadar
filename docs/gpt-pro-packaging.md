# Generate with GPT Pro

The Finished page and Thumbnails page have a **Generate with GPT Pro** button.
It queues only the clicked, ready production. Published productions have the button disabled and are refused by the server, including jobs queued before publication. A scheduled Codex task checks
the local worker page once an hour, reads the final script and resolved
channel/production style and Bible, inspects references, and generates four
finished thumbnails using Codex's built-in image tool. It returns a recommended
title, four distinct title options and a complete YouTube description. The button label is not a model ID
and does not switch a ChatGPT subscription or select a separate Pro model.

## Files and review

- `thumbnails/gpt-<job>-1.jpg`, `-2.jpg`, `-3.jpg`, `-4.jpg`: 1280x720, under 2 MB.
- `title.txt`, `titles.txt` and `description.txt`: in the production's source folder.
- `publish_kit.json` and `thumbnails/thumbs.json`: imported for the existing UI.
- `thumbnails/jobs/<job>/`: source result and backups of replaced metadata/text.

Old thumbnail image files remain available. The new set has no automatically
chosen image; use **Choose this one**. Its words are part of the image, so the
old Pillow text controls are hidden. A new click after completion requests a
new set; duplicate clicks while queued/running reuse the existing job.

The production's approval/publication state and its source video stay as they
were. This does not upload anything to YouTube or modify the final video/export.

## Worker connection

The active Codex chat heartbeat is `whisperradar-gpt-pro-packaging`, scheduled
once an hour. Manage it in Codex Scheduled. Use Run now on this task to process queued jobs immediately. Empty checks stay quiet.

The browser worker lives at `http://127.0.0.1:8540/gpt-pro/worker`. It is restricted
to direct loopback requests. A browser session holds its claim token. Claiming
is serialized across workers; failed or abandoned claims require an explicit
retry. After two hours without completion, a claim becomes failed.

The scheduled worker uses the browser page, avoiding unattended filesystem
writes into production folders. It supplies paths only from
`$CODEX_HOME/generated_images`; the dashboard validates and imports the files.
Other paths and non-landscape images are refused. The script, subtitle, style,
Bible and production folder are fingerprinted at claim time; changed inputs
invalidate the result rather than silently publishing stale packaging.

WhisperRadar and the Codex desktop app must be available on this same computer
when the worker runs. Queue pickup can take up to one hour, plus generation
time. No FlowBatch, Renderly or API fallback is used for this route. If the
built-in image tool is unavailable, the worker reports a failed job.

The JSON/CLI bridge is also available for diagnostics:
`scripts/gpt_pro_bridge.py claim --job-file <private-state-file>`.
It must connect to localhost and keeps claim tokens out of console output.

## Returned JSON

```json
{
  "title": "Recommended video title",
  "description": "Complete description, including optional verified chapters and hashtags",
  "keyword": "main topic",
  "titles": [{"text": "Recommended video title", "why": "Reason"}, {"text": "Second title", "why": "Reason"}, {"text": "Third title", "why": "Reason"}, {"text": "Fourth title", "why": "Reason"}],
  "recommended_thumbnail": 1,
  "thumbnails": [
    {"path": "C:/Users/.../.codex/generated_images/.../one.png", "text": "HEADLINE ONE", "prompt": "Exact generation prompt", "idea": "Concept"},
    {"path": "C:/Users/.../.codex/generated_images/.../two.png", "text": "HEADLINE TWO", "prompt": "Exact generation prompt", "idea": "Concept"},
    {"path": "C:/Users/.../.codex/generated_images/.../three.png", "text": "HEADLINE THREE", "prompt": "Exact generation prompt", "idea": "Concept"},
    {"path": "C:/Users/.../.codex/generated_images/.../four.png", "text": "HEADLINE FOUR", "prompt": "Exact generation prompt", "idea": "Concept"}
  ]
}
```

Tests: `python -m unittest tests.test_gpt_packaging tests.test_finished_page
tests.test_packaging tests.test_thumbnails tests.test_thumbnail_at_review
tests.test_thumbnail_auto_api` (no paid generators).

Ready status does not automatically queue packaging. Click Generate with GPT Pro for each production. YouTube currently accepts up to three variants per A/B test; the fourth is a spare.
