# Manual script and explainer-plan handoff

This is the shared manual workflow for Codex/GPT, Claude and the user. Read the production's `manual_review/handoff.md` before continuing its work. If absent, create it from [the template](manual-production-handoff-template.md). This is a living handoff, not an automated judge or a replacement for the app's approval records.

## Roles and sequence

1. Agree the topic, audience, angle and viewer payoff. Use evidence for demand; distinguish source observations from guesses. Record the selected references.
2. One agent writes the script; the other reviews the actual file. Either agent can take either role. Review hook, channel-specific opening, purpose where appropriate, spoken delivery, simple English, awkward idioms, repetition, retention, calculations, consistency and material claims. Learn technique from references without copying their expression. Do not force every channel into the same opening or enforce 4,000 words.
3. The writer corrects the named findings; the reviewer rechecks them against the revised artifact. Keep optional preferences separate from errors. User approval of the script precedes final audio.
4. The user supplies final audio and SRT. Check they correspond to the approved narration. Anchor the plan to this SRT, not estimated speech timings.
5. One agent writes the plan; the other checks it against the whole narration and the actual renderer's supported behavior. Check coverage, anchors, timing, factual meaning of charts and diagrams, readability and animation state. A valid JSON file or a clean checker is not proof of a faithful visual explanation.
6. Fix the affected scenes and recheck. Record changes and unresolved findings; do not repeatedly rewrite unrelated scenes.
7. The user accepts the plan and renders. Inspect playback afterwards for visual clarity, pacing, clipping and synchronization. Passing a text/code review does not prove the rendered video looks right.

## Keep one source of truth

- Update `manual_review/handoff.md` after a meaningful review, revision, approval or render inspection. Keep it short and current; append a compact history.
- Link actual production files by absolute path. Record SHA-256 fingerprints for the script, SRT, active/candidate plan and other inputs relevant to the reviewed claim. Do not paste whole scripts into the handoff.
- A fingerprint identifies the reviewed version; it does not establish user approval or that audio matches the script. If inputs changed, identify what must be rechecked before relying on an older review.
- Findings have stable IDs, scene/passage, concrete mismatch, requested correction, reviewer, status and verification evidence. Quote enough to find the problem. Mark resolved only after inspecting the fix.
- Say what was checked and what was not. Distinguish structural validation, editorial review, external fact verification, user acceptance and rendered playback. Never invent scores, sources, test results or approval.
- If the file is missing or stale, inspect current artifacts and conversation before reconstructing state. Do not assume silence means approval.
- Follow explicit user instructions over this workflow. Changing who writes does not authorize external paid calls, live webchat runs, audio generation or publication.

## While a render is running

Do not stop/restart it or change its script, audio, subtitles, plan, compiled plan, renderer, settings or assets without explicit authorization. Read-only inspection and this separate handoff are allowed. Stage proposed changes for after the render. Do not start a render yourself unless asked.

## App integration boundary

This handoff documents manual work. Do not forge automated judge passes, change approval flags, or call a stage Done with GPT-Claude merely because the agents reviewed it here. Use the app's existing explicit acceptance workflow when the user wants to promote a candidate. No automatic webchat, polling loop or model-to-model messaging is introduced.

## Topic structure and The AI Guy opening

Every script needs a connected explanation: identify its central question and viewer payoff, introduce necessary information in a useful order, make each section add something, and end with a direct answer. Choose a structure appropriate to the topic; this is not an identical outline for every channel.

For The AI Guy, establish the named subject, its plain-English meaning, relevance and viewing purpose immediately. Do not open on an unexplained number or postpone the topic behind background lessons. Introduce any LLM-killer or speed claim early as a question or attributed claim. Demonstrations and comparisons follow once the viewer knows what is being explained. Treat an unclear opening as an editorial finding even when individual facts are correct. Audio/SRT and plan need regeneration and review after a structural script rewrite.

## Explain technical terms in everyday language

Keep useful technical terms so viewers learn them, but explain each needed term immediately in ordinary words and connect it to a concrete example. Avoid several unfamiliar terms in one sentence. Define acronyms on first use. Do not explain jargon with more unexplained jargon or remove an important concept merely to make the words shorter. A viewer new to the subject should be able to follow the explanation on one listening. Explain plainly without making an inaccurate analogy sound literal. Review comprehension, not just word length.
