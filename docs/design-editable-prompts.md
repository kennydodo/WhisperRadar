# Design note: editable, per-channel, copyable prompts (NOT IMPLEMENTED)

Status: design only, agreed with Kehinde 2026-10-10 ("no implementation yet").

## Goal
Four prompts drive the pipeline: plan writer, plan judge, script writer, script judge.
Today they are fixed in code. Make each editable per channel, keep today's text as the
default, and let the user copy a channel's prompts to another channel (like the brief).

## Precedents to reuse
- Brief: `studio.notes_prompt`, `briefs.py` (`brief_custom`, `resolve_profile`), `brief_template.md`.
- Per-channel settings on `own_channels`: NULL = inherit global default (`settings.row_get`),
  plus the existing "apply to all channels" UI.
- Niche playbooks (`niche_playbooks.json`) stay separate: niche = genre knowledge,
  channel prompt = how this channel wants things written/judged.

## What is editable
| Kind | Code today | Editable part |
|---|---|---|
| plan_writer | `plan.writer_prompt` + `_RULES` | guidance, tone, title style rules text |
| plan_judge | `plan.judge_prompt` | audit criteria, pick preference wording |
| script_writer | `external_prompts.script_writer_prompt` / `studio.script_prompt` | voice, structure, style guidance |
| script_judge | `studio.rating_prompt` (+ web `script_judge_prompt` bar text) | criteria, severity wording |
Analyst prompt stays fixed (it extracts the package from the original; leak-sensitive).

## Storage
- Table `channel_prompts(channel_id, kind, body, updated_at, version)`; no row = default.
- Defaults stay in code (single source of truth), shown read-only in the editor and
  copyable into the custom box. Reset-to-default = delete the row.
- Alternative (simpler): 4 nullable text columns on `own_channels`. Table preferred:
  versioning and export are easier.

## Template + locked envelope
User edits only the guidance region. Code wraps it in a locked envelope the user cannot change:
- JSON reply format and field names (parsers depend on them), must_fix/claims contract,
  short-reply rule.
- Code-checked rules stay in code (no numbers in titles, spread/variety/first-word limits,
  keyword-copy check). The prompt text may describe them but the checks always run.
- Blindness guarantees: the plan writer never sees the script or original title; nothing
  from the original (names, hook, ending, points) flows judge -> writer except style/tone
  advice and keywords; writer chat is always new when writer != judge. Word window 80-115%.
Placeholders (named, validated): {channel} {genre} {package} {niche} {refs} {rules} {title}...
Save-time validation: required placeholders present, forbidden ones absent from writer prompts
(original title, script text), length cap, unknown placeholders rejected with a clear message.

## Copy / share
- "Copy from channel X" per kind or all four; "Apply to all channels" via existing pattern.
- JSON export/import of the four prompts (backup, move between installs).
- Inheritance order: channel custom -> global custom (optional) -> built-in default.

## UI
Channel page, new "Prompts" tab: 4 editors, each with default view, diff vs default,
reset, copy-from dropdown, and "Preview" rendering the final prompt against a sample
production (shows what will actually be sent, envelope included).

## Runtime
- Both API and web-chat routes must build prompts through the same functions so a custom
  prompt applies everywhere (`autorun`, `webstages`, `external_prompts`, `plan`).
- Log which prompt version/hash each run used (plan file, attempts) so results can be traced.
- Changing a prompt mid-production affects only later stages; show a warning.

## Risks
- Users breaking parsing -> locked envelope + validation + "test prompt" dry run.
- Leaking the original into the writer -> forbidden placeholders + scrub still applied in code.
- Drift between channels -> show "customized" badge and diff vs default.

## Decisions (Kehinde, 2026-10-10)
- Niche playbooks are probably no longer needed: the user writes what they want in the
  prompt (after researching the niche with an LLM). Keep `niche_profile` only until the
  prompts replace it; do not extend it.
- Judge rating bars (`script_min_rating`, `plan_min_rating`, strictness) are settable too,
  per channel, next to the judge prompt.
- Global custom layer: yes. The existing global Settings page is that layer; order is
  channel custom -> global custom -> built-in default. Revisit if it is not liked.
- Settings page gets collapsible sections (see survey in the chat reply / below).

## Suggested build order (later)
1. Prompt registry (defaults + placeholder schema + envelope) with no UI, tests for parity
   (default output byte-identical to today).
2. Storage + resolve per channel + hash logging.
3. Editor UI + validation + preview.
4. Copy/apply-to-all/export-import.
