# Edge-case catalogue for the generic builder

This is a list of the situations a generic builder (extract, then generate, then prompt) and the phone agent it produces are most likely to get wrong on an unseen brief. Each item says why it is hard, which layer has to handle it, and which fixture case covers it.

**Layer** codes:
- **E**: the extractor (brief files + openapi -> `policy.json`) must capture it.
- **G**: the deterministic oracle/generator code (code nodes, routes, hooks) must compute or enforce it.
- **P**: the prompt has to make the agent say or ask it correctly.

Case ids refer to `fixtures/S*/cases.json`. "S7" means the conversation-behaviour bank built by a separate task (T25). Those items are only listed here and are not duplicated in S5/S6.

## The two banks added here

| Bank | Dir | Form | Brief files (pass in this order) | Tool API | Cases |
|---|---|---|---|---|---|
| Corvane Bank | `S5/` | clean spec that stresses the calendar and amounts | `brief.md` | identical to `tools/openapi.json` (runnable live) | 25 |
| Larkspur Building Society | `S6/` | raw material: email, policy extract, memo A, memo B (partly reverts A), codes CSV | `01-email.md`, `02-policy-extract.md`, `03-memo-2026-09-14.md`, `04-memo-2026-10-05.md`, `05-codes.csv` | own `openapi.json`: renamed fields and enums on all 7 tools | 24 |

- **S5** has a **Sunday-to-Thursday** working week (weekend = ISO 5, 6) and Sunday-Saturday weeks. Its closures are a bridging Sunday, a Monday holiday, a five-day Christmas chain (24, 25, 26 weekend, 27, 28), New Year's Eve, and New Year's Day on a weekend with a substitute Sunday. One public holiday (8 Dec) has the bank open. Deadline counting is **mixed**: early = 12th business day after today, while mid/late = 7th/3rd with today as day 1 when it is a business day. Attempt 1 is "last business day of the week, else next week's, capped at the deadline". Floors are formulas: card `max(minimum_payment, 10 % past_due)` rounded half up, loan `min(instalment_amount, past_due)`, car loan `ceil(25 % past_due)` with `instalment_amount` ignored. Amounts are written `1.234,56 €`. Relative dates include "the Tuesday after next", dates without a year, and dates that have already passed. Callbacks run Sunday to Wednesday 08:00-<19:00 and Thursday 08:00-14:00 inclusive, and "today" counts only if it is a business day. `now` covers every weekday and includes a holiday, the bridging day, a Friday (weekend), 24 Dec, 30 Dec and 31 Dec.
- **S6** renames the call variables (`member_ref`, `given_name`, `family_name`, `salutation`, `call_started_at` as `YYYY-MM-DD HH:MM`) and every tool field. Identity is `check` = `birth_year` and `house_number`, with `response`. The enum still contains the legacy `mobile_last4`, which must not be used. `get_account` returns `accounts` with `product`, `arrears`, `days_in_arrears`, `min_to_pay`, `monthly_repayment`, `overdraft_limit`. `create_promise` takes `amount_eur` and `due_on`, `add_note` takes `code`, `schedule_callback` takes a single `slot`, `transfer_to_agent` takes `desk`, and `close_call` takes `result` plus `detail`. The documents conflict on purpose. Memo A changes the Amber deadline (10th -> 8th), callback hours, max wrong answers (2 -> 3), the identity question and a closure (16 Nov). Memo B withdraws the callback and max-wrong changes, keeps the deadline and identity changes, moves the closure to 20 Nov, and adds N-PL notes before a plan transfer. The policy also has a struck-through band row, a struck-through discount rule, a struck closure row with a footnote (31 Dec is a working day, callbacks only until 12:30), and a footnote exception (overdrafts have no floor). The band table uses ditto/merged cells. The CSV has unused codes (`LGL`, `N-LG`) and the openapi an unused desk (`RECOVERIES`). In the closing order, DSP beats PTP, and NPR detail comes from the largest-arrears account (ties broken by list order).
- In both banks, `cases.json` uses the public-test-set shape (canonical ledger keys in `expected`). `facts.json` uses the S1-S4 keys (`max_wrong`, `close_reasons`, `deadline_examples` with band, deadline, attempt 1 date, floor and full per product). Every date and amount was produced and asserted by a throwaway script (business day, not before `now`, not after the product deadline, floor <= amount <= full, callback inside its window and after `now`). The relative-date and memo-A-vs-memo-B values were checked by hand.

## A. Calendar and dates

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| A1 | Weekend is not Saturday/Sunday (Friday/Saturday) | Every date helper defaults to Mon-Fri; the weekend must come from the brief | E, G | S5-01, S5-04, S5-22, S5-25 |
| A2 | Week runs Sunday-Saturday vs Monday-Sunday | "this/next <weekday>" and "last business day of the week" depend on the week start | E, G | S5-06, S5-13, S6-04 |
| A3 | `now` is a holiday | "today" is not a business day: no pay-today date, no callback today, counting starts later | G, P | S5-03, S5-05, S5-14, S6-13 |
| A4 | `now` is a weekend day | Same as A3 but on the weekend; callback "today" is not allowed | G | S5-04, S5-10 |
| A5 | `now` is a bridging day | Bridging days are listed in prose next to real holidays | E, G | S5-13 |
| A6 | Pay "today" on a non-business day | Must be recorded on the next business day and the customer told | G, P | S5-03, S6-13 |
| A7 | Nth business day counted from tomorrow | Off-by-one if today is included | G | S5-01, S6-10, S1-01 |
| A8 | Nth business day counted with today as day 1 | Off-by-one the other way; when today is closed, day 1 is the next business day | E, G | S5-03, S5-13, S3-01 |
| A9 | Mixed counting methods in one table | The extractor must keep `counted_from` per band, not globally | E, G | S5-04, S5-24 |
| A10 | Deadline chain across several consecutive closures + weekend | Naive "+N days" or a single holiday skip fails | G | S5-06, S5-05, S6-06, S6-23 |
| A11 | Holiday that falls on a weekend + observed substitute day | The substitute day is the real closure | E, G | S5-05, S5-10 |
| A12 | Public holiday on which the bank is open | Must NOT be treated as closed | E, G | S5-04, S5-18 |
| A13 | Closure cancelled / moved by a later document | Must use the latest calendar | E | S6-01, S6-02, S6-16, S6-19, S2-01 |
| A14 | Struck-through closure row + footnote saying it is a working day | Strikethrough markup must be read as "not in force" | E | S6-09, S6-23, S6-24 |
| A15 | Dec 31 / New Year: year rollover in counting | Date arithmetic across years, attempt dates in January | G | S5-05, S5-06, S5-16, S6-09 |
| A16 | Attempt 1 = "last business day of the week; if today, next week's" | Today being Thursday (or the last open day before a holiday) shifts a week | E, G | S5-06, S5-11, S5-16, S3-08 |
| A17 | Attempt date capped at the deadline | A short late-band deadline is earlier than end of week | G | S5-04, S5-11 |
| A18 | Attempt 1 = "2nd working day after today" across a reopened day | Differs depending on which memo's calendar is applied | E, G | S6-16, S6-19 |
| A19 | Customer names a date after the deadline | Offer the deadline (not a roll) | G, P | S5-07, S5-09, S1-04 |
| A20 | Customer names a date after the deadline that is also a holiday | Must cap to the deadline, not roll forward from the holiday | G | S5-07 |
| A21 | Customer names a closed day within the deadline | Roll to the next business day, possibly over several closures | G | S5-05, S5-09, S6-23, S1-05 |
| A22 | "the <weekday> after next" | Two weeks out, week-start dependent | G, P | S5-01, S5-06, S5-09 |
| A23 | "next <weekday>": following week vs first one after today | The two definitions differ on most days; the brief decides | E, G | S5-08, S5-09, S6-07, S6-11, S1-05 |
| A24 | "this <weekday>" already in the past | Must ask again, not treat it as refusal or as next week | G, P | S5-16 |
| A25 | Date without a year / "the 24th" | Resolve to the next occurrence on or after today | G | S5-25, S6-23 |
| A26 | Vague date twice -> propose the deadline | The deadline itself may fall on an unusual weekday (Sunday) | G, P | S5-22, S6-10 |
| A27 | Callback "today" after the day's window has closed | Next business day may be several days away | G | S5-02, S6-12, S1-02 |
| A28 | Callback window differs per weekday / per date | Thursday shorter (S5), 31 Dec until 12:30 (S6), Friday (S3) | E, G | S5-15, S6-24, S3-09 |
| A29 | Callback end inclusive vs exclusive | 19:00 excluded (S5), 17:30 / 14:00 included | E, G | S5-10, S5-15, S6-12 |
| A30 | Callback time earlier than `now` on the same day | Must be later than now | G | S5-15 |
| A31 | Callback "tomorrow" when tomorrow is weekend/closed | Reject, ask once more | G | S5-02, S5-10, S6-02, S2-02 |
| A32 | `now` variable renamed / other format (`call_started_at`, `DD/MM/YYYY HH:MM`) | Clock parsing must follow the brief | E, G | S6 (all), S4, S2 |

## B. Amounts

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| B1 | Floor = larger of a field and a percentage | Not a plain field lookup; needs an expression | E, G | S5-04, S5-07, S5-18, S5-21 |
| B2 | Floor = smaller of field and past due | The floor can equal the full amount | E, G | S5-05, S5-09 |
| B3 | Floor = percentage rounded **up** to whole euros, tool field ignored | Rounding direction + a decoy field | E, G | S5-17 |
| B4 | Half-cent rounding (half up) | 201,005 -> 201,01; float maths gives 201.00 | G | S5-07 |
| B5 | European number format in the brief (`1.234,56`) | The extractor must not read 1.234 as 1.234 euros | E | S5 (brief), S5-01 |
| B6 | Customer amount with cents, in words or with "comma" | "two hundred and forty comma five" = 240.50 | P, G | S5-05, S5-18, S4-01 |
| B7 | Customer amount below the floor | Offer the floor once; a refusal then uses an attempt | G, P | S5-05, S5-09, S5-21, S2-06 |
| B8 | Product with no floor (footnote: full only) | "Too much" or a lower offer is a refusal | E, G | S6-05, S4-12 |
| B9 | Floor defined in prose/words without a tool field | "fifty euros, or the whole past due if less" | E, G | S4-02, S4-12 |
| B10 | Never ask "how much can you pay" | LLM default is to ask | P | all happy paths |
| B11 | Total-balance threshold triggers a transfer | Sum across products | E, G | S4-03 |

## C. Identity

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| C1 | Identity questions other than DOB/phone (`birth_year`, `house_number`, postcode, card digits) | Field names and normalisation come from openapi + brief | E, G | S6-01, S6-10, S6-16, S2, S3 |
| C2 | Legacy enum value present but forbidden (`mobile_last4`) | Openapi enum is not the list of questions to ask | E, P | S6-19 |
| C3 | Max wrong total vs per question | Scope differs between briefs | E, G | S3-06, S3-07, S1-03 |
| C4 | Max wrong changed by memo and changed back | The latest document wins, even when it reverts | E | S6-03 |
| C5 | Day/month swap hint does not count as wrong | Counter must ignore it | G | S5-12, S5-23 |
| C6 | Normalising answers ("fourteen b" -> `14B`, DD-MM-YYYY) | Tool-specific format | E, P | S6-10, S6-16, S2-03 |
| C7 | Customer corrects an answer before it is sent | Send only the corrected value | P | S6-10 |
| C8 | Refusal twice before verification | Different close code than refusal after verification | E, G | S6-15 |

## D. Multi-product

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| D1 | One date for all, after one product's deadline | That product goes on its own deadline; read-back says so | G, P | S5-08, S5-24, S6-22 |
| D2 | Two products capped to two different deadlines | Each product needs its own band | G | S5-08 |
| D3 | Band boundaries (15/16, 29/30, 45/46) | Inclusive ranges | E, G | S5-24, S6-22 |
| D4 | Paid + dispute + promise mixed | Notes per product and the right outcome | G | S5-11, S1-10, S3-02 |
| D5 | Hardship declined, customer still promises on another product | Notes only on products without a promise | G, P | S5-19 |
| D6 | Attempts counted per product | A refusal on one product does not use another's attempts | G | S5-06, S6-16 |
| D7 | Product list field renamed (`accounts`, `items`) | Hooks reading the list must follow openapi | E, G | S6 (all), S2 |

## E. Closing, notes and transfers

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| E1 | Dispute outranks recorded promises | Opposite of Brief B's "promise wins" | E, G | S6-04, S6-18 |
| E2 | `no_promise` reason from the largest-arrears product | Not the first product, not severity | E, G | S6-09, S6-20 |
| E3 | Tie on the largest arrears -> list order | Tie-break rule | G | S6-21 |
| E4 | `no_promise` reason from the first non-paid product with a mapping | Note reason != close reason (`dispute` -> `disputed`) | E, G | S5-12, S1-10 |
| E5 | `no_promise` reason by severity | Third precedence variant | E, G | S3-04 |
| E6 | Notes before a plan transfer, none before others | Per-queue note rule added by a memo | E, G | S6-07, S6-08, S3-05 |
| E7 | Agreed but unrecorded promise dropped on a transfer | The agent must not record it | G, P | S5-20, S6-07, S2-07 |
| E8 | Transfer after recorded promises: TRF wins, promises stay | Outcome precedence | G | S6-11 |
| E9 | All products paid -> `paid`/`PAID` | Needs all notes `paid` | G | S6-17, S1-07 |
| E10 | Single product still gets a note | Some briefs only note in multi-product calls | E, G | S5-16, S5-21, S6-05 |
| E11 | Close code with no reason vs code + detail field | `result`/`detail` naming, `detail` only for some codes | E, G | S6 (all), S2 |
| E13 | Close codes with no reason field for third-party outcomes (DEC, CBK) | Code from CSV, verbatim phrase from policy | E, P | S6-14, S6-02 |
| E12 | Unused close/note codes and desks in CSV/openapi | Must not be invented as triggers | E | S6 (`LGL`, `N-LG`, `RECOVERIES`) |

## F. Tool failures

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| F1 | `create_promise` fails twice on the first product, others succeed | Retry once, then note and continue | G, P | S5-08, S3-10 |
| F2 | `create_promise` fails twice and that note decides the close reason | Interacts with E2/E3 | G | S6-09, S6-21 |
| F3 | `create_promise` fails twice on a single product | Close reason system error | G | S1-09 |
| F4 | `verify_identity` service failure (500) | Does not count as a wrong answer | G | S2-08 |
| F5 | `add_note` fails once | Retry, the note still has to be written | G | S4-09 |

## G. Document conflicts, overrides and raw material

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| G1 | Memo overrides policy | The later document wins | E | S6-10, S2-04 |
| G2 | Second memo withdraws part of the first | "Withdrawn" means back to the policy value, not the memo A value | E | S6-02, S6-03, S6-12 |
| G3 | Second memo keeps other parts of the first | The extractor must not revert everything | E | S6-10, S6-19 |
| G4 | Closure moved by memo B (opened one day, closed another) | Both holiday removal and addition | E | S6-01, S6-02, S6-16 |
| G5 | Struck-through rule (discount) | Must never be offered | E, P | S6-06 |
| G6 | Struck-through table row (band 90+) | Must not create a band | E | S6 (facts `bands`) |
| G7 | Footnote exception to a rule (overdraft has no floor) | Footnote markers are easy to drop | E | S6-05 |
| G8 | Ditto/merged table cells (`"` meaning "same as above") | Row must inherit band and deadline | E | S6-06 |
| G9 | Code list as CSV with extra columns and unused codes | Parsing + not inventing triggers | E | S6 (`05-codes.csv`) |
| G10 | Unwritten rules in an email | Email content is policy too | E, P | S6-01 (speak-as forms), S2 |
| G11 | Holidays only in prose / in a table by month | Need date normalisation | E | S6, S4-04 |
| G12 | Same 7 tool names, renamed fields and enums | The generator must bind to openapi, not the kit | E, G | S6 (all), S2, S3, S4 |
| G13 | Policy table names a rule that the memo changes only for one desk | Scope of an override | E | S6-07 vs S6-08 |

## H. Conversation styles (covered by S7)

These depend on how the customer talks rather than on the brief's numbers. They are listed for completeness, and the cases live in S7 (task T25).

| # | Situation | Why it is hard | Layer | Covered by |
|---|---|---|---|---|
| H1 | Answers in pieces (date of birth over two turns) | Assemble before calling the tool | P | S7, S2-10 |
| H2 | Corrects a date/amount at the read-back | Re-check and read back again | P, G | S7, S1-12, S5-09 |
| H3 | Talkative, off-topic | Stay on the script without dropping rules | P | S7, S3-12, S1-11 |
| H4 | Asks questions back (interest, fees, "why") | "No information" answer, no invention | P | S7, S4-10 |
| H5 | Numbers and times in words | Normalise | P | S7, S4-11 |
| H6 | "Is this a scam?" / "Are you a robot?" | Verbatim phrases, continue | P | S7 |
| H7 | Unclear confirmation of identity of the person | Ask twice, then close | P | S7 |
| H8 | Refuses to talk twice | Notes + close | P, G | S7 |
| H9 | Asks for a person, then accepts help | Ask once before transferring | P | S7, S4-10 |
| H10 | Third party offers to pay or answer security | Must refuse | P | S7 |
| H11 | Mixed refusals and dates in one sentence | "A date is never a refusal" | P, G | S7 |
| H12 | Silence / non-answer | Uses an attempt | P | S7 |
