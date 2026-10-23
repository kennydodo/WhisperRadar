# Script-first beats: channel inputs and output contract

The optional beats action plans from the recorded narration without asking the writer to count subtitle cues. It uses the same `external_prompts.planning_context_prompt` as the regular web-chat shotlist planner: configured planning brief, motion/hold/type/presentation profile, pacing, visual density, creator shot direction, channel visual style, bible, reference policy and supplied reference filenames. Elements are additional consistency guidance, not a replacement for the bible or reference images.

The beats adapter replaces only the brief's output schema and cue-number instructions. Content, text/number policy and composition rules still apply. Animation instructions are added only when the production's effective channel settings enable the animated opening.

## JSON

Top-level fields: `style`, `beats`, optional `refs` and `refPrompts` maps. Each beat has `starts_with`, `scene`, `type`, `motion`, `prompt`; optional `refs`, `land`, `ai_motion`, `overlays`, `transition`, `sfx`, `reveal`. Beat `refs` are copied onto its image. The reference registry and generation prompts are copied to the normal shotlist. Supplied names/paths override conflicting generated entries and never get a generation prompt. Reference-disabled channels reject reference-bearing output.

Reveals use `{"starts_with": ["first item quote", "second item quote"], "layout": "row"}` in place of cue numbers. A separate-image build-up also has `images: [{"prompt": "...", "refs": [...]}, ...]` inside `reveal`; the app allocates filenames and maps each quote to its subtitle cue. Existing structural checks validate the resulting reveal contract. Overlay times remain seconds relative to the shot, as in the regular planner.

## Corrections and checks

Corrections contain `replace` entries keyed to the current beat index, with `original_starts_with` identifying the old beat. They may include `style`, `refs` and `refPrompts` changes. Empty `replace` supports registry-only/style-only corrections; an empty registry map clears that map. Single-beat corrections preserve omitted optional fields; explicit null/empty fields clear them. Corrections receive the channel context, current index and reference registries.

To merge adjacent beats, a replacement may include `through`: `{"beat": 6, "through": 7, "with": [<one combined beat>]}` removes beats 6–7 and inserts the combined beat. All ranges use the pre-correction index, are applied in reverse order, and must not overlap. Merged beats explicitly describe their combined visual, reference assignments and optional fields. Opening-word quotes mark locations, not the length of a beat; the planner is told to group developing ideas rather than split every subtitle fragment into a new image.

The app rechecks the final allowed correction. A checker exception is a failure, never a pass. Failed attempts save `beats.json` including reference metadata but preserve the previous `shotlist.json`. Successful attempts back up the prior shotlist and use the existing save/stage-advance path.

The beats form offers an optional quality reviewer, off by default. It runs only after code checks pass and uses the regular shotlist reviewer prompt with an additional focus on explanatory props, composition and one depictable moment per image. The writer and reviewer must use different site/account keys. A rejection becomes a targeted beats correction, then code checks and review run again. Invalid reviewer replies are retried once and cannot advance or overwrite the old shotlist.

## Diagnostics, source consistency and numeric rendering

Each run gets its own directory under `webchat_debug/beats_runs/`, containing the full writer/correction/reviewer prompts, replies, complete per-round fault reports, numerical coverage, source comparison, pacing summary and a final result. The dashboard log remains concise; the files are not truncated to the first three faults.

The production page and jobs warn about script/subtitles content differences, including changed amounts/percentages. Elements now use recorded subtitle narration when available and record a source fingerprint in `elements_source.json`; old or mismatched elements are flagged for regeneration. A newer timestamp alone is not treated as proof of different content.

Pacing reports expose the number of shots under three seconds and whether a channel minimum is configured. Existing channel hold and share checks still apply; no universal minimum or channel setting is silently imposed.

With renderer text enabled, generated images remain text-free. Automatic narration-value overlays count as numerical coverage; the writer must not duplicate them. The audit previews automatic values with the same renderer helper, while missing required renderer values return to the writer for explicit overlays. Image-only omissions are advisory because a channel may prohibit numeric image text. Split amounts such as `$43` followed by `,680` are reconstructed across subtitle cues, and a third value in one cue is no longer discarded. Raw stills cannot establish whether final numeric rendering is readable; inspect the rendered video too.

Unit tests use fake chat transports and local checks; no paid API or live chat is called. A fresh live run is required to assess creative quality; these changes do not rewrite an existing production's saved shotlist.


### Explicit motion and pacing settings

Channel planning briefs control motion limits for both beats and regular shotlists.
Custom motion shares left blank impose no limit. Family share meaning selects
approximate targets (default) or maximums only. Selecting a preset still selects
that preset's stated limits.
There is no fixed ZI/ZO split. The Custom motion form includes a maximum for any one
motion code as well as the ST, pan and tilt family caps.

The channel brief also has two pacing controls, available with every preset:
- Shots allowed below minimum hold (%): blank means 0% before the configured tolerance; the final shot is exempt. Without a minimum hold this has no effect.
- Maximum shots spanning one subtitle cue (%): blank disables this check. Every cue
  mode follows its own one-shot-per-cue contract instead.

The two pacing maxima each have a separate tolerance control, defaulting to +5
percentage points. A 50% single-cue cap accepts up to 55%; lower shares always
pass. Set either tolerance to 0 for strict enforcement. A disabled single-cue
limit stays disabled. These tolerances affect the share of shots, not their duration.

The writer and judge receive the same settings that the code validates. With no
minimum hold configured, beats have no invented minimum duration or word count.


### Motion mix tolerance

Custom motion defaults to approximate family targets, with a configurable motion
mix tolerance of +/-5 percentage points. For example, tilt at 15% makes the combined
ZI/ZO remainder 85% when those are the only allowed families. The app, writer and
judge accept 10-20% tilt and 80-90% combined zoom, including the boundaries. The two
zoom codes have no required split. The tolerance is per family, not multiplied by
the number of codes in that family.

Blank family shares stay unrestricted; the zoom remainder is only derived when
every other enabled family has a share. Set tolerance to 0 for strict percentages,
or choose Maximums only for one-sided caps. The any-one-motion-code field remains a
maximum, with the same configured upper tolerance. Existing custom shares use the
approximate-target default; no stored channel values or existing shotlists are rewritten.
