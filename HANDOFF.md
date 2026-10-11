# HANDOFF — Freya "Build the Builder", session of 10–11 Oct 2026

You are picking up a hackathon repo. Read this file fully, then `STATUS.md` (Turkish, team status) and
`build-the-builder-v3/build-the-builder/README.md` (the organisers' rules). All kit code is in
`build-the-builder-v3/build-the-builder/`; paths below are relative to it unless they start with `shared-results/`.

## 1. The task in one paragraph

We write a **builder**: brief files + the bank's tool API (`openapi.json`) → a Freya phone-agent workflow, with no manual
step, in ≤ 30 minutes per brief (self-test included). On Sunday the organisers publish unseen briefs (one clean spec, one
raw bank material: mail, policy extract, memos, code list) with their own tool API (same 7 tool names, fields and allowed
values may change). They test the exported agents on unseen customers, on several calendar days, **3 times each; a customer
counts only if all 3 runs pass** (bank records + policy + honesty, judged per customer). Up to 3 products per customer
(Brief B). Customers talk like people (numbers in pieces, corrections, words, questions back). **After the final briefs
open, nothing from them may be written into the builder** — every fix must be generic.

## 2. Where things are

| What | Where |
|---|---|
| Build an agent | `python build.py briefs/brief-a.md briefs/brief-b.md --openapi tools/openapi.json --slot <slot>` (needs `source env.sh`) |
| Generator (policy → workflow) | `builder/generate.py`; runtime Python embedded in hooks/code nodes: `builder/oracle.py` (EMBED region) + `builder/codegen/*.py` |
| Self-test (fake bank inside Freya) | `builder/selftest.py`, `builder/testmode.py`, cases: `builder/casegen.py` |
| Freya client | `freya.py` (do not change `AGENT_LLM`) |
| Run test customers | `tools/run_cases.py <agent_id> --cases <file> [--reps N] [--only id1,id2] [--out file]` |
| Edge-case sets | `tools/edge_cases.py` → `results/edge-cases*.json`; runner `run_edge.sh [--quick|--focus|--days|--large]`, `AGENT_ID=` to pick an agent |
| Failure transcripts / latency | `tools/fail_transcripts.py`, `tools/turn_latency.py` (new) |
| Fixture banks (other briefs) | `fixtures/S1..S7` (brief material, `openapi.json`, `cases.json` with answer keys), policies in `policies/` |
| Offline tests | `uv run --with pytest --python .venv/bin/python -m pytest -q` → **212 passed, 2 skipped** |
| Results of this session | `shared-results/2026-10-11/` (index in §10) |
| Agents (state file `.freya-agents.json`, not in git) | `brief-a` (old, `ab2440a1`), `brief-a-opt` (this session, `7c4f99c6`), `s6-audit` (S6), each with a `-selftest` copy |

## 3. What happened, in order

1. Pulled main, ran the teammate's 99 edge cases → 21/99 with a "date of birth loop". Root cause: `run_edge.sh` took the
   last agent in `.freya-agents.json`, which was the build's **`-selftest` copy** (fake bank, needs `scenario_json`) →
   every `verify_identity` rejected. Fixed (skip `-selftest`). Real agent: 84/99 passing both of 2 runs.
2. Freya's judge hung a simulation twice (calls stuck in `analyzing`) → runs blocked forever. Found the cancel endpoint.
3. Wrote 31 more hand-written cases (EDGE-24..54), merged the teammate's branch `claude/epic-pascal-reyni1` into main,
   deleted merged branches (kept `wip-t22`, unverified WIP: hard attempt limits + new policy fields).
4. Prompt optimisation (rule split by phase) + `check_offer` tool; new agent `brief-a-opt` (self-test 10/10 first try).
5. 250-case set (diversified: 69 new situations, conversation styles on ~half) → **222/250**. All 28 failures traced
   (§6), fixed, re-run ×2 → 25/28 both runs.
6. Systematic audit of every deterministic layer against independent references (§7) → parser, formula-floor and
   self-test-bank bugs found and fixed. Live S6 build exposed the self-test bank bugs (1/10).
7. Chain started at ~06:00 (§11): 250 re-run on the fully fixed agent, call-day set ×2, S6 rebuild.

## 4. Code changes (all generic; commit `adc6fbd` and later)

| Area | File(s) | Problem → change |
|---|---|---|
| Prompt size | `generate.py` `rule_phases()` | All ~80 brief rules were in the system prompt on every turn. Now each rule goes to `all` (system prompt), `greet` (identity step) or `negotiate` (after verification), from where the extracted policy files the same text; rules filed under triggers/outcomes/closing stay global; call-variable table rows that repeat the variable list are dropped. No rule is lost (test enforces it). Brief A+B per-turn prompt: greet 20.3k → 16.1k chars, negotiate 19.4k → 16.8k. |
| New tool | `codegen/check_offer.py`, `generate.py` `_code_tool`, `build_code(mode="tool")` | `check_offer(product_id, date, amount)`: model-callable `code_exec` (args arrive as `params`, top-level `return` is the result; verified on Freya by test mode). Same `check_promise` as the `create_promise` guard. Called when the customer names a date, a period or an amount; not for the full amount today or for offers in the account facts. Option `check_tool` (default on). |
| Spoken dates | `oracle.py` `day_words_to_digits`, `parse_spoken_date` | "twenty-sixth" → the 6th (6 Sep 2027 / 6 Nov: all holiday failures); "October the first" → 1 Nov; "the four of November" → nothing; "in about five weeks" → nothing. Now compounds, cardinals, years in words ("nineteen eighty-eight", "two thousand and five"), filler "the", "in N days/weeks/months". |
| Times | `oracle.parse_time` | `"noon" in "afternoon"` → every afternoon time 12:00; words gave nothing. Rewritten (words, half/quarter past/to, morning/afternoon/evening). |
| Phone digits | `oracle.spoken_digits`, `verify_pre.py` | Words ("four four seven one", "double four", "oh three", "forty-four seventy-one") → digits; "the last four digits are…" phrase ignored. |
| Birth date w/o year | `verify_pre.py` | The model filled in the current year ("the 23rd of February" → 2026-02-23) → bank counted wrong answers → verification failed. A *birth* date in this/last year is kept as a day+month piece; the agent asks only for the year; nothing sent or counted. |
| Parallel calls | `codegen/common.py` `sent_ok`/`promised_of`/`notes_of`, `promise_pre.py`, `note_pre.py` | Two `create_promise`/`add_note` in one message: both post-hooks credited one product (shared `w_cp_pending`/`w_note_pending`) → duplicate notes; a "refused" note on a product WITH a promise. Pre-hooks mark each product per key (`w_cp_sent_<pid>`, `w_note_sent_<pid>`); counted as done when `<tool>_success_count` ≥ marks and `_failure_count` = 0; any failure → explicit marks only. |
| Floor formulas | `oracle.eval_amount_expr`, `build_product`; `generate.runtime_policy` | `products.types[t].floor_expr` (e.g. `max(minimum_payment, round_half_up(0.1 * past_due, 2))`) was extracted but **dropped from the runtime policy** → floor = full amount → every partial promise rejected on such banks (S4/S5). Safe recursive-descent evaluator (numbers, fields, + − × ÷, min/max/abs/round/round_half_up/round_up/round_down), no `eval`. |
| Negotiation text | `oracle.account_facts_text`, `offer_text` | Floor recorded on Friday instead of today; extra offer after the last attempt. Attempts labelled "a persuasion attempt: only after a refusal or a non-answer"; floor only when the customer says the amount itself is too much ("a plain no or 'I can't' is a refusal"); a floor offer keeps the day being discussed; "after attempt N is refused, make no other offer (not the floor either), no other day". |
| Triggers | `generate._trigger_lines`, `default_greet_task` | "Don't want to talk": the first time before verification explain once why the questions are asked; the second time close at once. Callbacks: call `schedule_callback` with every offered time (the hook's window check and "ask once, then no_callback" become deterministic). |
| Hung simulations | `freya.py` `run_simulation`/`cancel_simulation`/`LAST_SIM`; `selftest.py` | Cancel when all calls ended and analysis has not moved for `SIM_ANALYSIS_STALL_S` (240 s) or nothing moved for `SIM_STALL_S` (900 s); status `cancelled_stalled`; finished calls are still scored. The self-test cancels a run it abandons (it used to keep blocking the workspace). |
| Self-test bank | `testmode.py` `_roles`, `spec_from_openapi`, fake bank | With renamed API fields (S6: `member_ref`, `check`/`response`, `account_id`, `due_on`, `slot`, `desk`, `result`/`detail`; list `accounts`) the fake bank read `member_ref` as the identity answer → **every self-test identity check failed**, callbacks with one `slot` field rejected, `get_account` answered `products` instead of the bank's list key. Now: customer-id field = what `get_account` takes; product = `get_account` item id; dates also from descriptions ("YYYY-MM-DD"); combined date-time role; last remaining field takes the role; `get_account` answers in the bank's own layout. |
| Case generator | `casegen.py` | "Year of birth" was 4 random digits (3573) → simulated customers confused. Now a year 1950–2000; wrong year = year + 7. |
| Tests | `tests/test_generate.py`, `tests/test_freya_sim.py` | Regression test per bug class; all 77 answer-key promises of S1–S7 through `check_offer`; stall cancel. Hook-size guard 21000 → 23000 chars (real sandbox limit ≈ 64 KB code + variables; if hooks grow further, zlib+base64 them like `testmode.tool_code`). |
| Edge tooling | `tools/edge_cases.py`, `run_edge.sh` | EDGE-24..54, FOCUS (teammate), `extra()` + `WIDE` (MIX/AD/MP/TP/ID families), `with_styles()`, 250 set, call-day set (`day_cases`, independent calculator), Brief B day cases. |

## 5. Rules compliance (checked line by line against README)

No manual edits to agents; `AGENT_LLM` untouched; agent calls only the bank's tool API — `check_offer` is a `code_exec`
step inside the workflow with no network access (README: "Code steps and hooks inside the workflow are allowed and
expected"; kit docs describe `code_exec` as a tool); workflows saved in place via slots; builds ≈ 6–10 min; trace written
by `build.py`; synthetic data only; Brief A dates/holidays exist only in test tooling (`tools/edge_cases.py`), never in
the builder; `env.sh` never committed.

## 6. The 28 failures of the first 250 run (agent `brief-a-opt` before the fixes) — complete analysis

Method for every case: judge reason + bank-record diff + full transcript with tool calls/results.

| Case(s) | Class | Root cause | Fix / status |
|---|---|---|---|
| GEN-07-holiday, GEN-03-holiday, FOC-HOL-01, MP-14 | agent (code) | "Monday the twenty-sixth" parsed as 6 Sep 2027, "the twenty sixth" as 6 Nov → "after the deadline" → offered 2 Nov instead of 27 Oct. Same parser in the `create_promise` guard, so the old agent had it too. | parser fixed; 2/2 in re-run |
| EDGE-51, EDGE-52, GEN-11-refuses-all | agent (code) | parallel `add_note` → both post-hooks credited one product → `wrap_up` re-sent the other → duplicate note | per-key marks; 2/2 |
| MP-09 | agent (code) | parallel `create_promise` (p1, p3) credited p3 twice → p1 got a "refused" note despite its promise | same fix; 2/2 |
| ID-07, GEN-08-restructure, EDGE-28 | agent (code) | birth date without year: model added 2026 → counted wrong → verification failed | year asked for, not counted; 2/2 (ID-07: bank records correct, case wording fixed) |
| GEN-01/05/07/04-too-much | agent (text) | floor offer (rule 26) read back for Friday (attempt-1 date) instead of today; GEN-04 also offered "this Friday" itself | attempt label + "keeps the day"; 2/2 |
| GEN-03-too-much, GEN-06-too-much | simulator | the simulated customer itself said "on Friday" / "by Friday" → agent was right | — (2/2 in re-run) |
| MIX-09 | agent (text) | after attempt 1 the agent offered the floor: read "No, I can't do that" as "too much" (rule 26). A first fix ("no offer after the last attempt") was not enough. | rule-26 clause ("a plain no or 'I can't' is a refusal"); **not yet verified live** |
| FOC-NOTALK-04, FOC-NOTALK-PRE-03 | agent (text) | no instruction for the 2nd "don't want to talk", nor for the 1st before verification | trigger clauses; 2/2 |
| EDGE-20, TP-09 | agent (text) | callback window checked by the model itself, tool skipped → asked twice (EDGE-20); goodbye loop ~50 turns without `close_call` (TP-09) | always call `schedule_callback`; 2/2 |
| EDGE-27, EDGE-42, AD-09 | case error | my expectations contradicted the brief (03/14 can only be March 14; floor-then-attempt sequence is legal; talkative style's "wedding on the 30th" clashed with a 30th payment day) | cases fixed; 2/2 |
| GEN-07-today | judge noise | judge's own reason says every criterion was met | — (2/2) — but it showed a needless `check_offer` call (fixed) |
| GEN-08-human | judge strict | "I can help you with this over the phone. Would you still like a person?" vs "ask whether you can help first" | — (2/2 in re-run) |
| GEN-09-no-talk | simulator | talkative persona's sentences cut off mid-way; never said "don't want to talk" | — (1/2) |

Totals: 20 agent bugs (all fixed), 3 case errors (fixed), 5 judge/simulator noise. Re-run of the 28 (×2, build with the
first-round fixes): **25/28 both runs** (MIX-09 0/2 → new clause, ID-07 0/2 → case wording, GEN-09-no-talk 1/2 → noise).
Earlier, call-day run on an older build (stopped at 27/54): 26/27; the one failure (DAY1016-10-too-late, "in about five
weeks" treated as a refusal) is fixed by relative-period parsing + `check_offer` for periods.

## 7. Audit of the deterministic layers (all against independent references)

| Check | Result |
|---|---|
| Spoken promise dates, 1,753 forms (every day 1–31, ordinal/cardinal, both orders, weekday) | 618 wrong → **0** |
| Birth dates, 12,000 forms (1940–2009, words/digits/ISO/slashes) | 150 wrong → **0** |
| Callback times, 30 forms | 14 wrong → **0** |
| Phone digits in words, 19 forms | 7 wrong → **0** |
| Deadlines / attempt dates / attempt-2 amount / replacement days: 728 call-day × band cases over 6 months vs a calculator written from the brief | **0** |
| Callback windows: 17,500 cases (call day × now × candidate day × time) | **0** |
| Closing outcome + reason: 1,032 complete end states, 1–3 products | **0** (incomplete states are routed to `wrap_up` first) |
| Answer-key promises of fixture banks S1–S7 (77) through `check_offer` | 26 rejected → **0** |
| Amounts in words | not parsed — the `amount` field is a JSON number, the model converts; fails safe (asks again) |

## 8. Response time (1,507 turns of the first 250 run)

Turns without a tool are effectively instant; **each tool call adds ~1.5–3.5 s** (median: `verify_identity` 1.7 s,
`verify`+`get_account` 3.6 s, `create_promise` 2.5 s, `check_offer` 2.1 s). Slowest turn per call: median 4 s, worst 27 s
(old agent: 5 s / 3 s, worst 30 s). Lever: avoid unneeded tool calls (not prompt length). Customers hang up when the
agent is silent too long (README); `run_cases.py` warns above a 10 s median.

## 9. Platform facts learned (add to STATUS)

- One simulation per workspace. The judge can hang a call in `analyzing`: cancel with `POST /api/v2/simulations/{id}/cancel`.
- Freya API returns intermittent 504/503: retry reads.
- The model makes **parallel tool calls**; post-hooks of parallel calls share one variable snapshot, per-key variable
  writes and the engine's `<tool>_success_count` / `_failure_count` survive.
- The model invents a year for a birth date given without one.
- `code_exec` works as a model-callable tool (args in `params`, top-level `return`).
- Generated code: no `{{`/`}}`, no leading-underscore names, no JSON `null` (tests enforce all three).
- A code change reaches an agent only through a new build (generator files are read at the build's generate stage).

## 10. Results index (`shared-results/2026-10-11/`)

| File | What |
|---|---|
| `run-edge.json`, `edge-report.txt` | teammate 99 cases on the old agent `ab2440a1` (84/99 both runs); `edge-report.txt` was later overwritten by the first 250 run's report |
| `run-edge-250.json` | first 250 run, `brief-a-opt` before the fixes: 222/250 (rows with call ids, failures) |
| `tx-250/*.txt` | transcripts (speech + tool calls + results) of its 28 failures |
| `run-fix28.json`, `fix28-report.txt` | re-run of the 28 (×2): 25/28 |
| `edge-cases*.json` | the case sets (130 / quick 54 / focus 28 / 250 / days 84) |
| `build-*.log` | build logs (opt, opt2, opt3 = A+B; s6 = first S6 build with the broken test bank) |
| `full-chain.log`, `verify-chain.log` | chain logs |
| After the chain (auto-pushed): `r250b-report.txt`, `run-250b.json`, `tx-250b/`, `days-report.txt`, `run-days.json`, `tx-days/`, `build-s6b.log`, `s6-after.log` | second 250 run on the fully fixed agent, call days ×2, S6 rebuild; §12 below is appended automatically |

## 11. Running when this was written (11 Oct ~06:20)

A chain on the workspace: (3) full 250 on the rebuilt `brief-a-opt` (all fixes except the very last test-mode ones,
which do not affect a normal run) → (4) call-day set ×2 (84) → then the S6 rebuild with the fixed self-test bank. A
background script waits for all of it, copies the results to `shared-results/2026-10-11/`, writes failure transcripts,
appends §12 with the numbers and pushes to `main`. **Do not start simulations before it has pushed** (one per workspace).

## 12. How to continue

```bash
git pull   # then:
cd build-the-builder-v3/build-the-builder && source env.sh
tail -3 ../../shared-results/2026-10-11/r250b-report.txt ../../shared-results/2026-10-11/days-report.txt
grep -E "stage .*:" ../../shared-results/2026-10-11/build-s6b.log | tail -5
ls ../../shared-results/2026-10-11/tx-250b/      # one transcript per failed case
```

1. Compare: 250 vs **222/250** (Brief A 197/220, B 25/30), slowest-turn median 4 s; days: expect ~all pass; S6 self-test
   vs **1/10** (broken test bank) — most should pass now. MIX-09's new clause is verified here for the first time.
2. For every failure read the transcript (tool calls included), not only the judge reason; classify agent / case / noise.
3. Fix agent bugs in shared generic code (hooks/oracle first, prompt lines last, never brief values), add a regression
   test, rebuild (`--slot brief-a-opt`), re-run only the failed ids:
   `python tools/run_cases.py <agent> --cases results/edge-cases-250.json --only "id1,id2" --reps 2`.
4. Then the open items below.

## 13. Gotchas

- **Never edit a shell script while it runs** (bash reads it incrementally; an edit re-ran a 250-case run).
- Code changes need a rebuild before a live test. Check the build's generate time vs the file's edit time.
- Judge reasons are hints: 5 of 28 failures were judge/simulator noise. Always read the transcript.
- `.personas.json`: personas are recreated when their text changes; harmless.
- macOS `sed` has no `\b`; use Python for edits.

## 14. Open items / recommendations (priority order)

1. Verify the chain's results (above); fix what is real.
2. **3/3 matters**: run focus + failed + days with `--reps 3`; work on cases at 2/3.
3. Brief B coverage in the builder's own self-test is thin (1 of 10 cases multi-product) — the final has up to 3 products.
4. Full timed builds on raw-material banks S2/S4/S6 (S6 is in the chain) and one with a modified `openapi.json`.
5. Safety net: "both sides said goodbye but no `close_call`" → close (seen once, TP-09).
6. `verify_pre` keeps the last question in one variable: parallel verify calls can mislabel the next-question hint (routing unaffected).
7. Decide with the team about branch `wip-t22` (hard attempt limits in code; overlaps the MIX-09 approach).
8. Final-day rehearsal: `git tag pre-final`, timed RUNBOOK dry run, final `ANTHROPIC_API_KEY`, package without `env.sh`
   (`tools/package_submission.py`), workspace reserved for the final runs.

## 15. Results of the chain (appended automatically 11 Oct 12:28)

```
--- 250 re-run (compare: 222/250 before the fixes)
slowest agent turn per call: median 4 s, worst 24 s. Failed calls where the customer hung up: 0.
243 / 250 passed (bank records and conversation checks). Last simulation 83c8c7b2-9056-41cc-a1ee-5d733260116b, status completed, 2307 s.
--- failed in the 250 re-run:
FAIL EDGE-05 edge   Dates         
FAIL EDGE-20 edge   Third party   
FAIL GEN-05-refuses edge   Generated     
FAIL GEN-09-no-talk edge   Generated     
FAIL MIX-09 edge   Mid-call change
FAIL MIX-14 edge   Mid-call change
FAIL AD-16 edge   Amounts and dates
--- call days x2 (84 cases, 30 Brief B)
slowest agent turn per call: median 5 s, worst 32 s. Failed calls where the customer hung up: 0.
-- run 1 of 2: 83 / 84 passed, 1032 s
--- failed on call days:
--- S6 rebuild with the fixed self-test bank (compare: self-test 1/10 with the broken bank)
[build 12:25:04] stage save: ok (4.2 s) generated + LLM prompts -> 593e6140-117f-40f5-92a4-4b03dc27e07e
[build 12:27:36] stage self-test v1: error (131.2 s) WorkspaceBusy: another simulation kept the workspace busy for 120 s
--- turn latency
all turns                                    n= 1508 median   0.6s  p90   4.3s  max  23.7s
node negotiate                               n=  906 median   0.8s  p90   5.3s  max  23.7s
node greet_verify                            n=  602 median   0.0s  p90   2.9s  max  11.7s
tools (no tool)                              n=  783 median   0.0s  p90   0.0s  max   9.8s
tools verify_identity                        n=  228 median   1.8s  p90   4.4s  max  11.7s
tools api_call,verify_identity               n=  206 median   3.5s  p90  10.3s  max  16.3s
tools create_promise                         n=  153 median   2.4s  p90   6.7s  max  23.7s
tools check_offer                            n=  107 median   1.4s  p90   7.1s  max  22.6s
```

Transcripts of every failure: `shared-results/2026-10-11/tx-250b/`, `tx-days/`. Next: §12 step 2.
