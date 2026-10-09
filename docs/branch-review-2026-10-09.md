# Branch review — feat/thumbnail-upgrade (2026-10-09)

Reviewed `master(eb3e287)...de17718` from the worktree; master checkout untouched.
Verdict: correct — vidIQ list A.2–A.7 + D.1/D.2 all implemented locally; paid
vidIQ MCP (A.4) and WhisperRadar-as-MCP (A.8) deliberately skipped.

Tests: full suite 1281 → 3 errors, all `PermissionError [WinError 32]` on temp
`wr.db` (Windows SQLite lock in teardown). Reproduced on the master checkout —
pre-existing flake, not a branch regression. Branch-touched modules (test_demand,
test_outliers, test_plan, test_research_paging, test_shorts, test_thumbnails,
test_timing, test_youtube_api): 113 OK.

## Minor findings (non-blocking — fix or accept at merge time)

1. **Plan-apply vetting bypass** — `webapp.py` (`studio_plan_save`): a
   `form.get("apply")` click always calls `apply_plan(cfg, pid, title=...)`
   with the page's title, and `apply_plan` only refuses when NO title is
   passed. So a human typing/clicking a non-candidate title bypasses the
   "apply_plan refuses" rule from the D.1 handoff note. The comment says
   this is an intentional human override — confirm it is what we want;
   if not, refuse in `apply_plan` even for explicit titles unless a
   `force=True` flag is set.
2. **Trending is English-only** — `youtube_api.trending()` hardcodes
   `relevanceLanguage="en"`. Fine for the current channels; add a
   language setting if a non-English niche is ever tracked.
3. **Shorts plan preview ignores the spinner** — `webapp.py`
   (`studio_shorts`): the preview table always plans `n=3` clips while the
   page's clip-count input (1–6) only applies on make. Cosmetic; pass
   `?n=` through if it should preview the chosen count.
4. **Blocking autocomplete** — `demand.suggestions` waits up to 10 s
   (`urlopen timeout=10`) inside the request thread; a dead network makes
   the Keywords page sluggish. Consider `timeout=3` or an async fetch.

## Merge checklist (when ready — NOT while a production is running)

- Dashboard runs off the master checkout; do the merge in the worktree:
  `git -C D:\Repos\WhisperRadar-thumb ...` (main checkout stays on master).
- Re-run the 113 branch tests, merge `feat/thumbnail-upgrade` into master,
  restart the dashboard (Flask does not hot-reload), then
  `git worktree remove D:\Repos\WhisperRadar-thumb`.
- Note the doc's Section C reminder: production 15 still needs a
  dashboard "Generate images" run with the logged-in Chrome/Flow session.
