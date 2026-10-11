# Handoff: audit and fixes of 11 Oct 2026

For a Claude Code session (or a teammate) picking up this repo. Read this, then `STATUS.md`. Kit code lives in
`build-the-builder-v3/build-the-builder/` (paths below are relative to it). Everything here is generic builder
code: nothing from a specific brief is written into the builder (README rule for the final).

## TL;DR

- One agent is built from Brief A + B: `python build.py briefs/brief-a.md briefs/brief-b.md --openapi tools/openapi.json --slot <slot>`.
- 250-case edge run (before the latest fixes): **222/250**; the 28 failures were traced call by call. After the first round
  of fixes they were re-run twice: **25/28 pass in both runs**. The remaining three are explained below.
- A systematic audit of every deterministic layer found and fixed real bugs in the **date/time/number parsers**,
  **parallel tool calls**, **formula floors**, and the **builder's self-test bank** (renamed API fields).
- Offline tests: `uv run --with pytest --python .venv/bin/python -m pytest -q` → **212 passed, 2 skipped**.

## What changed (code)

| Area | File(s) | Change |
|---|---|---|
| Prompt size | `builder/generate.py` | `rule_phases()`: each brief rule goes to the system prompt (`all`), to `greet_verify` only (`greet`) or to `negotiate` only (`negotiate`), from where the extracted policy files the same text. Call-variable table rows that repeat the variable list are dropped. **No rule is lost.** Brief A+B per-turn prompt: greet 20.3k → ~16.1k chars, negotiate 19.4k → ~16.8k. |
| New tool | `builder/codegen/check_offer.py`, `generate.py` (`_code_tool`, `build_code(mode="tool")`) | `check_offer(product_id, date, amount)`: a `code_exec` tool the model calls when the customer names a date, period or amount. Uses the same `check_promise` as the `create_promise` guard. Toggle: option `check_tool`. |
| Spoken dates | `builder/oracle.py` (`day_words_to_digits`, `parse_spoken_date`) | "twenty-sixth" was read as the 6th (→ 6 Sep 2027 / 6 Nov); "October the first" → 1 Nov; number words ("the four of November") → nothing; "in about five weeks" → nothing. Now: compounds, cardinals, years in words ("nineteen eighty-eight", "two thousand and five"), filler "the", relative periods ("in N days/weeks/months"). |
| Times | `oracle.parse_time` | `"noon" in "afternoon"` made every afternoon time 12:00; words ("seven in the evening", "half past ten") gave nothing. Rewritten. |
| Phone digits | `oracle.spoken_digits`, `codegen/verify_pre.py` | "four four seven one", "double four", "oh three…", "forty-four seventy-one" → digits. |
| Birth date w/o year | `codegen/verify_pre.py` | The model filled in the current year ("the 23rd of February" → 2026-02-23) and the bank counted wrong answers. A birth date in this/last year is now kept as a day+month piece and the agent asks for the year; nothing is sent. |
| Parallel tool calls | `codegen/common.py` (`sent_ok`, `promised_of`, `notes_of`), `promise_pre.py`, `note_pre.py` | Two `create_promise`/`add_note` calls in one message credited the same product (shared `w_*_pending` variable) → duplicate notes, or a "refused" note on a product WITH a promise. Pre-hooks now mark each product per key; the engine's `<tool>_success_count` / `_failure_count` decide. Any failure → old behaviour (safe side). |
| Floor formulas | `oracle.eval_amount_expr`, `oracle.build_product`, `generate.runtime_policy` | `products.types[t].floor_expr` (e.g. `max(minimum_payment, round_half_up(0.1 * past_due, 2))`) was extracted but **dropped from the runtime policy** → floor = full amount → every partial promise rejected (S4/S5). Now evaluated by a small safe parser (no `eval`). |
| Negotiation text | `oracle.account_facts_text`, `offer_text` | Attempts are labelled "only after a refusal or a non-answer"; floor offer only when the customer says the amount itself is too much ("a plain no or 'I can't' is a refusal"); after the last attempt no other offer; a floor offer keeps the day being discussed. |
| Triggers | `generate._trigger_lines`, `default_greet_task` | "Don't want to talk": what to do the first time before verification, and close at once the second time. Callbacks: call `schedule_callback` with every offered time; the hook decides (rule "ask once, then close no_callback" becomes deterministic). |
| Hung simulations | `freya.py` (`run_simulation`, `cancel_simulation`, `LAST_SIM`), `builder/selftest.py` | Freya's judge can leave a call in `analyzing` forever; the simulation never completes and blocks the workspace. Now cancelled when all calls ended and analysis has not moved for `SIM_ANALYSIS_STALL_S` (240 s), or nothing moved for `SIM_STALL_S` (900 s). The self-test cancels a simulation it abandons. Cancel endpoint: `POST /api/v2/simulations/{id}/cancel`. |
| Self-test bank (test mode) | `builder/testmode.py` (`_roles`, `spec_from_openapi`, fake bank) | With renamed API fields (S6: `member_ref`, `response`, `account_id`, `due_on`, `slot`, `desk`) the fake bank read `member_ref` as the identity answer → **every self-test identity check failed** (S6 self-test 1/10). Now: customer-id field = what `get_account` takes; product field = `get_account` item id; dates from descriptions; combined `slot` date-time; `get_account` answers in the bank's own list layout (`items`, `accounts`). |
| Case generator | `builder/casegen.py` | "Year of birth" was 4 random digits (3573); now a year 1950–2000 (wrong answers: year + 7). |
| Tests | `tests/test_generate.py`, `tests/test_freya_sim.py` | Regression tests per bug class, incl. all 77 answer-key promises of fixture banks S1–S7 through `check_offer`. Hook size guard raised 21000 → 23000 chars (sandbox limit is ~64 KB code + variables; if hooks grow more, compress them like `testmode.tool_code`). |

## Test tooling

- `tools/edge_cases.py` writes `results/edge-cases.json` (130), `-quick.json` (54 hand-written), `-focus.json` (28),
  **`-250.json`** (250, with conversation styles on ~half), **`-days.json`** (84: 5 call days incl. Friday, before a
  holiday, Christmas/New Year, evening callbacks; 30 of them Brief B). Day expectations come from an independent
  calculator written from the brief text (`_deadline`, `_attempt1`, `_promise_day`), not from the builder's oracle.
- `bash run_edge.sh [--quick|--focus|--days|--large]`; `AGENT_ID=<id>` to pick an agent. It now skips `-selftest` slots
  (the old version tested the self-test copy with the fake bank: the 21/99 "date of birth loop" run was that).

## Audit results (deterministic layers, all against independent references)

| Check | Result |
|---|---|
| Spoken promise dates, 1,753 forms | 618 wrong → **0** |
| Birth dates, 12,000 forms | 150 wrong → **0** |
| Callback times, 30 forms | 14 wrong → **0** |
| Deadlines / attempt dates / replacement days, 728 call-day × band cases (6 months) | **0** mismatches |
| Callback windows, 17,500 cases | **0** |
| Closing outcome + reason, 1,032 complete end states (1–3 products) | **0** |
| Answer-key promises of fixture banks S1–S7 (77) | 26 rejected → **0** |

## Live results so far

- 250 run, agent `brief-a-opt` before the latest fixes: **222/250** (Brief A 197/220, Brief B 25/30). Slowest-turn
  median 4 s. Every tool call costs ~1.5–3.5 s; turns without tools are instant → avoid unneeded tool calls.
- Re-run of the 28 failures (×2): **25/28 both runs**. Left: MIX-09 (fixed afterwards: rule-26 clause), ID-07 (bank records
  correct; case wording fixed), GEN-09-no-talk (simulator noise: talkative persona's sentences cut off).
- Classification of the 28: 20 agent bugs (all fixed), 3 case errors (fixed), 5 judge/simulator noise.

## Running when this was written (11 Oct ~06:15)

A chain on the workspace (results in `results/`): full 250 on the rebuilt agent → `results/r250b-report.txt`,
`run-250b.json`; then call days ×2 → `days-report.txt`; then an S6 rebuild with the fixed self-test bank →
`results/build-s6b.log`. Compare the 250 run with 222/250.

## How to continue (for the next session)

The chain above runs on its own; no Claude session is needed while it runs. When it is done:

```bash
cd build-the-builder-v3/build-the-builder && source env.sh
tail -3 results/r250b-report.txt results/days-report.txt; grep -E "stage .*:" results/build-s6b.log | tail -5
.venv/bin/python tools/fail_transcripts.py FAILED results/tx-250b results/run-250b.json   # one file per failed case
.venv/bin/python tools/turn_latency.py results/turns-250b.json results/run-250b.json      # turn times by node / tool
```

1. Compare: 250 run vs **222/250** (Brief A 197/220, B 25/30), slowest-turn median 4 s; days set: everything should pass
   (old build: 26/27 before it was stopped); S6 self-test vs **1/10** (broken test bank) — expect most to pass now.
2. For every failure read `results/tx-250b/<id>.txt` (speech + tool calls + results), not only the judge reason, and
   classify: agent bug / case expectation wrong / judge or simulator noise (cut-off sentences, persona off-script).
3. Fix agent bugs in shared generic code (hooks/oracle first, prompt lines last), add a regression test, rebuild
   (`--slot brief-a-opt`), re-run only the failed ids: `python tools/run_cases.py <agent> --cases results/edge-cases-250.json --only "id1,id2" --reps 2`.

## Gotchas learned

- **Never edit a shell script while it runs** (bash reads it incrementally: an edit re-ran a 250-case run).
- Only one simulation per workspace; a stuck one: `freya.cancel_simulation(sim_id)`.
- Code changes reach an agent only through a new build (`build.py` reads generator files at its generate stage).
- Generated code: no `{{`/`}}`, no leading-underscore names, no `null` in the workflow (tests enforce all three).
- Judge reasons are hints: read the transcript and the tool calls before changing the agent (5 of 28 were noise).

## Open items / next steps

1. Check the running chain's results; analyse each failure (transcript → root cause → agent / case / noise).
2. Brief B is under-tested in the builder's own self-test (1 of 10 cases has several products): consider more.
3. `verify_pre` keeps the last question in one variable: parallel verify calls can mislabel the next-question hint (harmless for routing).
4. Amounts in words are not parsed (the tool field is a JSON number, the model converts; fails safe).
5. A "both sides said goodbye but no close_call" safety net would protect every scenario (seen once: TP-09 before the callback fix).
6. Measure 3/3 (the final counts a customer only when all three runs pass): `--reps 3` on focus + failed + days.
