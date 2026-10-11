# 250-new test set

250 unique call scenarios (seed 25) for the collections voice agent:
- 150 Brief A
- 40 Brief B2 (PUB-13)
- 60 Brief B3

Families: Pay full, Partial, Below floor, Dates, Counter-offer, Persuasion, Refusal, Asks back,
Multi-product mix, Dispute, Already paid, Hardship, Callback, No talk, Third party,
Mid-call change, Transfer, Identity. All personas, DOBs and phone digits are synthetic.

## Files
- `cases-250-new.json` - the corrected set (with the 3 judge-check fixes below).
- `baseline-v5/run-250new-v5.json`, `baseline-v5/fails-250new-v5.txt` - our v5 reference result,
  run on the ORIGINAL set (before the check fixes).

## How it was generated
    python tools/gen_cases_250.py --n 250 --seed 25 --out <file>

## How to run against any agent
From the kit dir:

    source env.sh && python tools/run_cases.py <agent_id> --cases testsets/250-new/cases-250-new.json --reps 1 --out results/run-250.json

`run_cases.py` batches personas by 100, auto-retries empty calls, and uses one simulation per workspace.

## Scoring
A case passes only if both the judge checks and the ledger checks (promises, outcome, close_call) pass.
Scoring is done by `tools/score.py`.

## Baseline (v5, original set)
- 159/250 overall: Brief A 108/150, Brief B 51/100.
- About 96 failures were EMPTY calls (infra, 0 turns). Excluding them: about 159/190 (84%).
- Weakest families (passed/total): Counter-offer 4/19, Persuasion 3/12, Refusal 4/10, Asks back 6/14,
  Multi-product mix 12/25, Dispute 5/9, Already paid 5/9, Dates 17/25. Many of these are inflated
  by empty calls. Strongest: Identity 19/20, Pay full 9/10, Mid-call change 17/20.

## Judge-check fixes made after the v5 run
1. Dropped the "call is on <date>" check.
2. Fixed the floor-as-attempt-2 wording.
3. "Asks once for exact day" now applies only to style None.

Expect the corrected set to score somewhat higher than v5 on the same agent behaviour.
