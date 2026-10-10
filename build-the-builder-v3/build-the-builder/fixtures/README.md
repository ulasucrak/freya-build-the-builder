# Synthetic holdout set (fixtures/)

Four fictional banks that look like the unseen final briefs. They exist to test that the builder is generic: nothing in here may be copied into `builder/` or `build.py`. All names, people and amounts are synthetic.

| Bank | Dir | Form | Brief files | Tool API vs kit | Cases |
|---|---|---|---|---|---|
| Nordlicht Bank | `S1/` | clean spec, Brief A structure + a multi-product section 13 | `brief.md` | identical fields and enums (runnable live) | 12 |
| Harbourline Savings Bank | `S2/` | raw material: email, policy extract, memo that overrides 4 rules, code list | `01-email.md`, `02-policy-extract.md`, `03-memo-2026-10-02.md`, `04-codes.csv` | heavily changed (see below) | 11 |
| Kestrel Credit Union | `S3/` | clean spec, multi-product, own closing precedence and note reasons | `brief.md` | changed enums, extra product type | 12 |
| Brightwater Mutual | `S4/` | raw messy markdown: odd tables, holidays in prose, numbers in words, struck-through rule | `brightwater-collections-notes.md` | changed enums, extra queue | 12 |

Each bank dir holds the brief file(s), its own `openapi.json` (same 7 tool names as `tools/openapi.json`), `cases.json` and `facts.json`. Pass the brief files in the order listed (S2: email, policy, memo, codes; the memo is the latest and wins).

## What varies

- **S1**: three bands (soft 1-10 / firm 11-30 / final 31-60; deadlines 10th / 6th / 3rd business day after today), attempt 1 = next business day, identity order phone digits then date of birth, callback 09:00 to 18:00 with 18:00 excluded, "next <weekday>" = first one after today, renamed close reasons (`callback_set`, `unconfirmed`, `security_failed`, `plan`, `care`, `declined`, `system_error`...) and a note-reason-to-close-reason mapping for multi-product calls.
- **S2**: call time variable `call_time`; `verify_identity` takes `question` (`dob` as `DD-MM-YYYY`, `postcode` in capitals) and `value`, no swap hint; `get_account` returns `items` with `kind`, `amount_overdue`, `days_overdue`, `min_due`, `instalment`; `create_promise` uses `pay_date`; `add_note` uses `code` (`N-REF`...); `close_call` takes a single `code` (`PTP`, `NRF`, `CBK`...) and no reason. The memo changes the Stage 1 deadline (8th -> 10th working day), cancels the 11 November closure and adds 13 November, widens callbacks to 08:30-19:30 inclusive, and lowers max wrong answers from 3 to 2.
- **S3**: deadlines count **today as day 1**; tier C has 3 attempts (3rd = floor on the next business day); identity is date of birth + `debit_card_last4` with max 1 wrong answer **per question**; product type `bnpl` with floor `next_instalment` and a `merchant`; Friday callbacks end at 16:00; notes on every product without a promise, even with one product; a `restructuring` note before a plan transfer; outcome `partial_promise`; `no_promise` reason by severity (disputed > hardship > technical > declined), not by product order.
- **S4**: variables `cust_ref`, `surname`, `honorific`, `now_local` in `DD/MM/YYYY HH:MM`; holidays only in prose (moved holidays, one explicit non-holiday); band table with words and a missing pipe; attempt 1 = third working day after today; overdraft floor is "fifty euros, or the whole past due if less" (no tool field); a total-balance rule (> three thousand -> transfer `senior_collections`, `handed_over` / `high_balance`); max wrong answers 4 (struck-through 3); outcome names `ptp`, `no_ptp`, `handed_over`, `id_fail`, `no_contact`, `already_paid`.

All banks keep the weekend on Saturday and Sunday.

## cases.json

Same shape as `public-test-set.json`: `id`, `brief` (`S1`..`S4`), `family`, `level`, `title`, `variables`, `identity`, `products`, `script`, `expected` {`outcome`, `reason`, `promises`, `notes`, `callbacks`, `transfers`}, `checks`, optional `faults` ({`tool`, `fail_first_n`}).

- `variables` use the bank's own call variable names (S2 `call_time`, S4 `surname`/`honorific`/`now_local`); `customer_id` is not listed, as in the public set.
- `identity` keys are the bank's `verify_identity` field enum values, values in the format the tool expects.
- `products` use the bank's own `get_account` field names. Product ids are `p1`, `p2`, `p3` in list order.
- `expected` keeps the canonical ledger keys (`outcome`, `reason`, `promises[].amount/date`, `notes[].reason`, `transfers[].queue`), with values from the bank's own enums. In S2 the close code is in `outcome` and `reason` is null; the note code is in `notes[].reason`.
- Faults cover `create_promise` (S1-09, S3-10), `verify_identity` (S2-08) and `add_note` (S4-09).

Families covered across the set: happy path, third party (callback incl. pre-holiday / Friday limits, wrong number, deceased), identity (fail, per-question limit, 2 vs 4 max), dates (deadline capping, holiday roll, vague date, "this/next weekday"), amounts (floor, below floor, small overdraft, amounts in words), hardship, already paid, dispute, restructuring, human request, high balance, tool failure, multi-product mixed outcomes, talkative / correcting customers. `now` values fall on every weekday incl. Fridays and days before closing days.

## facts.json

The gold facts a correct extraction must contain, independent of the `policy.json` schema: calendar (weekend, holidays, non-holidays), clock variable and format, bands with `deadline.n` and `counted_from` ("tomorrow" = today never counts, "today" = today is day 1), attempts, identity questions (tool field, order, format, max wrong and its scope), floor field per product type, callback window, queues, note reasons, the outcome table, multi-product closing rule, key phrases, memo `overrides`, and `deadline_examples` (per case and product: band, deadline, attempt 1 date; S3 attempt 3 date; S4 floor and total balance). Use the examples to score a generated date engine directly.

## Running

- Offline: feed the brief files + `openapi.json` to the extractor and compare against `facts.json`; check the generator's computed dates against `deadline_examples` and `expected`.
- Live: the hosted bank mock only serves the public case ids (`run_cases.py` asks it for a practice session per case id), so these cases cannot be simulated as they are. S1 keeps the kit's tool API exactly, so an S1-built agent can at least be run against the public cases' API and chatted with turn by turn.

Every expected date was computed by a throwaway script (not committed) and checked by hand: promise dates are business days, not before `now`, not after the product's deadline, and amounts lie between floor and full.
