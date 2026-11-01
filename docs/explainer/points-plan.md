# Plan: "points" for motion scenes (spoken points on screen, in step with the narration)

Status: revision 2 (after GPT review of revision 1). Pilot built for Flow / Levels / Bars / Counter; see section 14. Author: Claude, 2026-11-01.

## 1. Problem

In motion scenes (flow, levels, bars, generation, grid, counter, line, split) the only text the narrator's point
can appear in is the **title** (shown at once) and **one footer line** (shown on a cue). Seen in production 118:

- 3:28 ("That's where NCM comes in. It builds several layers of number codes..."): the title was on screen, but the
  definition/point the narrator was saying was not, until the first diagram element appeared ~9 s later.
- 21 of 34 scenes in the 118 plan had the first diagram element 2.5-9 s after the scene opened.
- One footer per scene forces a choice between two points (e.g. "hierarchy" vs "patterns it learned").

Workaround already applied to 118 (title states the spoken point, footer at the scene start). That is a patch, not a
design: it cannot carry several points and loses the later takeaway.

## 2. Goal

A motion scene can show **up to N short points** (bullets with a marker) that appear **on the narrator's words**,
alongside the diagram, so the viewer reads what is being said while the picture builds, and never waits on a title alone.

Not a goal: more text on screen. Points are short (2-8 words); the diagram stays the main picture.

## 3. Style rule (from the channel owner)

The channel style decides how points are drawn. **Points are NOT handwritten unless the channel look is handwritten.**

| Channel look | How a point is revealed |
|---|---|
| Not handwritten (`pal.hand` false; the default for this channel) | Plain typed text in the channel's body font; the point **appears** (short fade + 8 px rise, 250 ms). No writing animation. |
| Handwritten (`hand_font` set, `pal.hand` true) | The point is **written**: left-to-right wipe over ~0.6-1.0 s in the hand font (pen-style). |

The reveal timing always follows the narration cue (`at`), never a fixed pace.

## 4. Data model (plan.json)

New optional key on every motion layout (`flow, bars, line, split, grid, counter, levels, generation, pipeline, code, ...`):

```json
"points": [
  {"text": "NCM builds layers of number codes", "at": "It builds several layers"},
  {"text": "Codes = patterns it learned for text", "at": "These are patterns the system"}
],
"points_mode": "appear"
```

- `text`: 2-60 characters (about 8 words), plain words, no emoji.
- `at`: narration words, same rules as every other anchor (3+ consecutive words, unique, in the scene).
- Points are in narration order; at least 500 ms apart (same rule as other items).
- `points_mode` (optional, default from the channel): `"appear"` (default), `"all"`, `"written"`.
  - `appear`: each point shows on its cue and stays.
  - `all`: the whole list is on screen from the scene start (dimmed); the point being spoken is highlighted, earlier
    ones stay normal. Good for a checklist or "three details" where the viewer benefits from seeing where it goes.
  - `written`: only honoured when the channel look is handwritten; otherwise falls back to `appear` (logged).
- Limits (revised): **1-3 points is normal**. Hard caps: `appear`/`written` 4, `all` 5. More is crowded, above all on a phone;
  a longer list is split over two scenes. The checker rejects a list over the cap with that advice. Points are OPTIONAL and
  used only where they explain something the diagram cannot show; the planner must not add bullets to every scene (that
  turns the video into a presentation).
- `footer` stays supported (one closing takeaway). A footer and points can both exist; the footer sits under the points.

## 5. Layout

- Placement is per scene (`points_place`, validated: `side` | `below`), with a default per component:
  - `side` (default for counter, bars, flow with up to 3 nodes): diagram scaled to 74%, points in a left column.
  - `below` (default for everything detailed: levels, pipeline, generation, grid, code, line, split, flows with 4+ nodes):
    diagram scaled to 86%, points as a row under it. A detailed hierarchy/pipeline is never squeezed into a side column.
  - Never shrink a diagram further; if it does not fit at these sizes the scene should not carry points.
- Scenes without `points` render exactly as today (no layout change). Old plans keep working untouched.
- Marker: small filled dot in the scene's accent colour; the highlighted point (mode `all`) uses the text colour at full
  strength, others at 55% opacity.
- First point should be anchored on the scene's opening words (the checker enforces, see 6).

## 6. Checker / validation rules (`explainer_scenes.check`, `recipes`, `remotion_backend`)

1. Shape: `points` is a list of `{text, at}`; `text` 2-60 chars; limits from section 4; `points_mode` in the allowed set.
2. Anchors: each `at` resolves inside the scene, in order, at least 500 ms apart, none in the last 2.5 s of the scene.
3. **Conditional rule (single, replaces the earlier "first point on opening words" wording).** First fix the diagram:
   the first meaningful visual should start within ~2 s of the scene start; re-anchor it before reaching for a bullet.
   Only if the diagram cannot start that soon (it would show something false or meaningless), the scene must carry a point
   (or footer) cued within the first 1.5 s. Also check later gaps: no stretch longer than ~6 s without any change on screen
   (new diagram element or new point). Message: "Scene X shows only its title for N s; start the diagram earlier or add a
   point on its opening words."
4. Number check applies to points: a number in a point must be said by the narrator (or the scene is `illustrative`).
5. Key terms (revised): a term counts as shown only if it is VISIBLE when it is said - a point counts only from its
   `start_ms` onward and only if its cue is at or before the cue where the term is said. Until that timing-aware check
   exists (pilot), points do NOT count as showing a term, so the existing rule stays conservative.
5b. **Reading time (new)**: after its reveal, a point must stay long enough to read: `900 ms + 320 ms per word`
   (x1.6 + 0.8 s when written). It must fit before the scene end tail. 500 ms spacing between points is only the minimum for
   distinct anchors, not a reading guarantee. Build error: "point N ... starts too late to be read".
6. Motion share: unchanged (points do not make a scene a text scene).
7. Whole-video coverage scan: unchanged; points only add content.

## 7. Planner prompt (writer + judge)

- Add `points` to the format example and rules: "Put the point the narrator is making on screen as a short bullet on its
  words. Open every motion scene with a point on its first words when the diagram's first element comes later."
- Rule against copying the sentence: a point is a label of the idea, not a transcript.
- Judge prompt: check that points restate what is said (no new facts), are short, and that no point is cued before the
  words that justify it.

## 8. Components (Remotion) - build order

Common: `common.jsx`/`motion.jsx` get `PointsColumn({points, mode, frame, fps, pal})` and a helper that resolves
`start_ms` per point (already provided by `recipes` the way items are). `Scene.jsx` passes `points` and reserves the
column when present.

Order (so the owner can judge the look early):
1. `Flow`, `Levels`, `Bars` (the three used most in 118) + `Counter`.
2. `Generation`, `Grid`.
3. `Line`, `Split`, `Pipeline`, `Code`, memory family (only where needed).

Reveal implementation: `pal.hand` false -> opacity/translate spring; `pal.hand` true -> `clip-path` wipe (inset right
100% -> 0) in the hand font. No stroke-by-stroke drawing in Remotion scenes (the pen renderer keeps real strokes for
text scenes).

## 9. Python side

- `recipes._general_params`/`_Stream`: resolve `points[*].at` to `start_ms` exactly like node/bar anchors; pass
  `points`, `points_mode`, `points_hand` (from the look).
- `remotion_backend`: `_check_points` shared by all components; reject bad shapes with plain messages.
- `explainer_terms.scene_text`: include `points[*].text`.
- `BUILDER_VERSION` bump (so unstamped/old explainer.json files migrate; hand-edited Remotion params are preserved by
  the existing `_same_content` rules, which must include `points`).
- Settings: channel option `points_mode` (appear | all; default appear) in Video options; help text explains that
  "written" only applies when the channel look is handwritten.

## 10. Migration of existing plans

- Plans without `points` are unchanged.
- Production 118: footers that carry a spoken point become points; the title-as-sentence workaround is reverted to
  short titles. Only affected scenes re-render (scene cache keyed by content).

## 11. Tests

- Shape/limit tests (5/8, length, order, spacing, tail margin).
- Text-before-diagram rule: fails a scene with late first element and no early point; passes with an early point or footer.
- Mode fallback: `written` on a non-handwritten channel -> `appear`.
- Layout: the column never overlaps node boxes/bars (Remotion snapshot test for flow/levels/bars at 3 and 5 points).
- Builder-version migration keeps hand-edited points.
- Existing suites unchanged (baseline: 42 known failures; no new ones).

## 12. Risks / open questions for review

1. Column width vs diagram size: resolved by `points_place` (side | below) chosen per scene, see section 5.
2. Should `all` highlight use colour or opacity only (accessibility/contrast on the chalkboard theme)?
3. Is 5/8 the right ceiling? 118 only needs 1-3 per scene.
4. Do we want points on text scenes (row/stack/compare)? Proposed: no, they already are text.
5. Judge: should the judge also see a frame at the first second of each scene (later project: rendered-frame judge)?

## 13. Rollout

1. Land 118 (title/footer patch already in plan.json) and render it.
2. Build sections 8.1 + 9 + 6 on a branch; render still frames of Flow/Levels/Bars with 1, 3, 5 points in both looks
   (typed + handwritten) for the owner to judge.
3. After approval: remaining components, planner/judge prompts, tests, docs (`docs/explainer/user-guide.md`),
   commit.

## 14. Revision 2 (after GPT review) - what changed and what is built

GPT's eight points and how each is handled:

1. Optional, not automatic - stated in sections 4 and 7; prompt will say "only where the diagram cannot show it".
2. Diagram first - rule 3 now says re-anchor the diagram before adding a bullet; later gaps are checked too (not yet wired).
3. 1-3 normal; caps 4 (appear/written) and 5 (all). Built and tested.
4. Layout per component - `points_place` side | below with component defaults. Built and tested.
5. Reading time - hold = 900 ms + 320 ms/word (x1.6 + 0.8 s written). Built and tested (build error when too late).
6. Key terms - points do not count as showing a term until the check is timing-aware. Built (points removed from
   `scene_text`), tested.
7. Contradictions resolved - single conditional rule; `points_place` defined and validated.
8. Cache and migration - point text, timing, mode and place are in the scene props, hence in the scene fingerprint
   (tested). `render._BUILT_KEYS` contained `points` (stroke coordinates) so a hand-edited bullet would have been ignored by
   the migration; `_content` now keeps `points` lists of `{text,...}` as content (tested: an edited point is not
   treated as generated).

Pilot status: Flow, Levels, Bars and Counter render with points in the typed look (frames at opening, mid-build and
completion reviewed). Not yet done: handwritten look frames, phone-sized narrated clips, timing-aware key terms, the
checker rule for late diagrams, planner/judge prompt, channel default setting, the remaining components (their own
layouts were not touched; the wrapper is generic, but each needs a frame review).
