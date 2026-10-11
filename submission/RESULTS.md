# Final case: builds and own-scenario results (11 Oct 2026, cloud session)

All agents were built by `build.py` from the case folder with no manual edits, and all ran on Freya's AGENT_LLM, which was left unchanged.
Each one was tested with our 42 scenarios (`cloud-handoff/final-scenarios.json`: 14 ordinary, 14 edge, 14 adversarial) through
`tools/run_scenarios.py` against the live mock https://freya-statement-mock.ulasucrak.workers.dev. The mock was
re-verified at 18:43 UTC: 617/617 request/response pairs identical, including the 12 s slow answer.

| Build (slot) | Builder commit | System prompt | Own scenarios | Median slowest turn | Notes |
|---|---|---|---|---|---|
| final-cloud | ad23ef3 | 47K chars | 18/42 | 7 s | verbatim rules part 2/3 timed out (330 s): mail/meeting rules lost |
| final | a1950c9 | 90K chars (~25.5K tokens) | 22/42 | 11 s | rules in 7 smaller parts, all ok |
| final3 | 38e832e | ~90K | 21/42 | 9 s | + trigger notes in situation lines |
| final4 | a3c0043 | 94K | 23/42 | - | + negative amounts as credit, document precedence line |
| final6 | ecabc3f | 11.4K chars (~3.25K tokens) | 22/42 | 4 s | + rules digest stage |
| final7 | 5ef72f9 | 9.95K chars (~2.84K tokens) | 15/42 | 4 s | digest target 4.5K chars, within the 1K-3K token guidance |

Generic builder fixes made in this session (no case values in builder code):
1. extract: a failed verbatim-rules part keeps its documents' text (paragraphs) instead of silently dropping prose documents; rules are asked in up to 8 parts of about 25K chars.
2. generate: a situation line carries the trigger's notes ("how: ...") when there is no fixed sentence.
3. generic_post: negative amounts get no spoken "minus" form; they are flagged as credit, not debt.
4. generate: one line on precedence when rules come from dated documents (most recent wins; example values are examples).
5. pipeline + generate: a "digest" stage condenses all rules, situations and phrases into a short rulebook, keeping quoted sentences verbatim. The system prompt is kept under 10K chars (~3K tokens), LLM prompt edits that exceed this are dropped, and the size is written to the trace.

**Submitted: final6** (agent abcbb49f, workflow 4dd16563): 22/42 on our scenarios (ordinary 4/14, edge 8/14, adversarial 10/14), median slowest turn 4 s, system prompt 11.4K chars (~3.25K tokens). The builder's full offline suite passed at ecabc3f: 602 tests, 4 skipped.
