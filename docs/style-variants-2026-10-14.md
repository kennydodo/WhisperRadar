# Style analysis variants (2026-10-14)

Two versions of the writing-style prompts exist in `whisperradar/studio.py`.

* **time-beat** (default) - the Structure section lists beats by their ROLE
  only (no topic, example, number or title from the reference video) and gives
  the timing only as a share of the runtime ("0-5%", "35-48%"), never minutes.
  The script writer turns those shares into word ranges for its own target
  (`beat_word_table`) and is told to keep the rhythm but write its own story.
  No beat may be a sponsor, ad, course or product promotion.
* **classic** - the prompts exactly as they were until 2026-10-14. Their
  Structure section gave absolute times (0:00-0:45) and described the reference
  video's own story beat by beat, so a script that followed it copied the
  reference's order and failed as "too close to the source".

Use the old one with `style_prompt(..., variant="classic")`,
`style_prompt_from_scratch(..., variant="classic")` and
`script_prompt(..., style_variant="classic")`. The classic text itself lives in
`_style_prompt_classic` / `_style_prompt_from_scratch_classic`.

Style guides already saved keep the old format; their minute timings produce no
word table, and the writer's rule "the Structure is a rhythm, never the story"
still applies to them.
