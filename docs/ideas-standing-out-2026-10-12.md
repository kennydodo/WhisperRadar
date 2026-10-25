# Ideas: how to stand out on YouTube (written 2026-10-12)

Context: WhisperRadar Studio already goes source transcript -> style guide + facts -> packaging plan -> script (judge + 5-gram overlap check) -> TTS -> shots -> Flow images -> merge. Many others run a similar pipeline, so the pipeline itself is not the edge. Channels: The Nature Made Us (animal behavior / human evolution), To Live and More (Japanese lifestyle), Dr. Oz Reyes (health), POV Finance (personal finance). Faceless: no on-camera.

## Findings from reading the code/DB
1. One source video per production: a script can't be better than its single source, and source errors get repeated. 435 transcripts sit unused as a corpus.
2. No results feedback loop: `prod_results` is empty; `view_snapshots` exists but doesn't feed topic choice or prompts.
3. Risk: "Dr. Oz Reyes" is a fictional doctor persona narrating medical content; "Pov Finance Replica" is named as a copy of an existing channel. YouTube's medical-misinformation and inauthentic/reused-content policies target both. Consider dropping the "Dr." framing and showing sources on screen.

## Ideas (priority order)
1. **Multi-source synthesis stage (all channels).** Before "script": pull the 5-15 most relevant transcripts on the topic from the library + a short web search. Produce a brief: what sources agree on, where they disagree, doubtful claims, what nobody covers. Script from the brief. Optional stage, start with The Nature Made Us. Reuse existing LLM provider/judge settings; add tests.
2. **Signature format per channel.**
   - Nature Made Us: "Why does X do Y? Stranger than you think"; recurring character; "human parallel" ending linking animal behavior to human evolution; series structure.
   - To Live and More: one Japanese habit per video, concrete step-by-step routine, "try it for 7 days" closer; calm slow pace + ambient sound design (differs from fast-cut channels).
   - Dr. Oz Reyes: claim-vs-evidence structure; study population/limits shown on the chalkboard; cite sources on screen.
   - POV Finance: concrete numbers in every video, computed by a calculation step (not LLM-invented); fixed protagonist as a series.
3. **Close the feedback loop.** Auto-fill `prod_results` from YouTube analytics (views, avg view duration, CTR); feed top/bottom performers into the planning prompt. Meaningful after ~20 videos per channel.
4. **Packaging tests.** Several title/thumbnail variants per video, use YouTube's thumbnail test, record winners. (See docs/next-session-thumbnails.md.)
5. **Better visuals where it counts.** Real motion on hook shots and key reveals instead of every shot; animated charts for finance/health; strict character bibles.
6. **Speed.** The radar sees uploads daily: react to news/trends in the niche within hours.
7. **Series/recurring formats** (weekly roundup, myth vs reality, deep dives) so viewers have a reason to subscribe.
8. **Add what AI can't fake:** original data/analysis from the transcript corpus, own voice/opinion segments (VibeVoice / record_my_voice.py), real screenshots/documents/maps mixed in.

## Open questions
- Which niche first? (Suggested: The Nature Made Us.)
- Build #1 (synthesis) or #3 (feedback loop) first?
