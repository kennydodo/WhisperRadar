# Next steps (written 2026-10-10)

## Video port (branch `video-render`)
1. Verify on Kehinde's machine: pull the branch, set `studio.video_engine: python` in config.yaml,
   run a real production's merge, compare the preview with the C# one; check NVENC/AMF/QSV auto-detect and a full-length render.
2. Merge to master: first discard/commit the uncommitted copies of AGENTS.md note + docs/design-editable-prompts.md in master.
3. Later: Settings toggle for the engine (config.yaml only for now); port Premiere/CapCut exports to drop the C# tool.

## Planning / scripting (waiting for the running production)
4. Review the new production's logs: script leak gone? judge strictness balanced? web relay errors ("ask failed", "no usable score")?
5. Flow rename test: log "Named the Flow asset ..." vs "Could not rename ..."; one gallery entry per ref.

## Designed, not built
6. Collapsible Settings sections (Auto Run, Script & shotlist, Production & images, LLM, Tools, Web chat LLMs).
7. Editable per-channel prompts (plan writer/judge, script writer/judge), judge ratings settable, global layer: docs/design-editable-prompts.md.

## Smaller items
8. Keep the judge's pick away from titles it marked `narrow`.
9. Title Case rule for titles.
10. Record the project where refs were generated, upload only for a different project.
11. (New) Judge -> writer hand-over templates (see below), planned on master.

## 11. Hand-over templates (judge -> writer)
Idea (Kehinde): a selectable template deciding what the judge passes to the writer: "as it used to be"
(old behaviour), "as it is now" (current: style/tone advice + keywords only, no original details),
"custom", and user-added templates. Design discussion happens before implementation.
