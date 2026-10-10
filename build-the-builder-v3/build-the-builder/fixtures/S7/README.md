# S7: Tidewater Bank, conversation-behaviour bank

Clean specification (`brief.md`, Brief A + B structure with different numbers, phrases and outcome reasons) plus 40 cases that stress how customers talk: numbers in pieces or words, self-corrections, off-topic chatter, questions back, hesitation, third parties and tool failures. `openapi.json` is the kit tool API (`tools/openapi.json`) with only the title changed, so the cases can be run against the kit's fake bank. Calendar and amount arithmetic (S5) and document conflicts (S6) are covered elsewhere.

Key numbers: bands gentle 1-5 / standard 6-25 / last 26-60 with deadlines 9th / 5th / 2nd business day after today (today not counted) and 1 / 2 / 2 attempts; attempt 1 is the full amount on the 2nd business day (or the deadline if earlier), attempt 2 the minimum on the deadline; identity is date of birth then phone last four, 3 wrong in total; callbacks 08:30 to 17:30 both included; closing days 2026-10-27, 2026-11-13, 2026-12-08, 2026-12-25, 2026-12-28, 2027-01-01; notes only on multi-product calls; multi-product close reason precedence hardship > dispute > refused > service_error.

Case format: same as `public-test-set.json` / S1 (`id`, `brief` S7, `family`, `level`, `title`, `variables`, `identity`, `products`, `script`, `expected`, `checks`, optional `style` and `faults`). Hint: dates in `expected` were computed by a throwaway script and verified (business days, not before `now`, not after the deadline, amounts between minimum and full). Product ids are `p1`..`p3` in list order.

Notes for runners: the `faults` cases use the kit's `fail_first_n`; S7-38 (two products, `create_promise` failing twice) assumes the agent retries the failed product immediately, in `get_account` order. Third-party cases use `family: Third party` so the run_cases persona plays someone else; the hand-over case is in the Identity family because the same persona turns into the customer.

## Cases by category

### Identity (8)
- S7-01 (easy, now 2026-10-14T11:00): Date of birth and phone digits said in words, one digit at a time
- S7-02 (medium, now 2026-11-05T09:30): Date of birth and phone digits given in two pieces
- S7-03 (hard, now 2026-10-21T10:15): Date of birth said in numbers in US order
- S7-04 (medium, now 2026-11-16T14:00): Corrects one phone digit mid-answer
- S7-05 (medium, now 2026-11-17T09:00): Refuses the date of birth once, then agrees after the explanation
- S7-06 (medium, now 2026-11-18T16:20): Asks what the call is about instead of answering, twice
- S7-07 (hard, now 2026-11-19T11:05): Wrong answers across both questions until the limit
- S7-08 (hard, now 2026-10-22T12:00): Spouse answers, asks what it is about, then hands the phone over

### Third party (7)
- S7-09 (hard, now 2026-10-22T15:00): Sister offers to pay on her behalf
- S7-10 (easy, now 2026-11-02T10:00): Wrong number
- S7-11 (medium, now 2026-11-03T13:00): Son says the customer has died
- S7-12 (medium, now 2026-11-10T14:00): Callback offered outside the window twice
- S7-13 (hard, now 2026-11-17T16:00): First callback time too late, second is the first allowed minute
- S7-14 (hard, now 2026-10-23T11:00): Vague callback time 'after lunch'
- S7-15 (hard, now 2026-10-28T14:00): Callback time offered is not later than now

### Negotiation (16)
- S7-16 (hard, now 2026-11-04T10:30): Agrees to the full amount, then lowers the amount at the read-back
- S7-17 (hard, now 2026-11-03T09:40): Moves the date past the deadline at the read-back
- S7-18 (medium, now 2026-10-19T10:00): 'Maybe' and 'I'll try' use up both attempts
- S7-19 (medium, now 2026-11-19T15:40): One-word and silent replies in the gentle stage
- S7-20 (medium, now 2026-11-23T10:00): Asks for a discount, is refused, then names the minimum
- S7-21 (medium, now 2026-12-01T11:10): Asks for a payment plan over six months
- S7-22 (medium, now 2026-11-25T09:50): Rude and sarcastic but still agrees
- S7-23 (medium, now 2026-12-04T10:00): Asks if it is a scam, then if the agent is a robot
- S7-24 (medium, now 2026-12-07T11:00): Insists on a person
- S7-25 (hard, now 2026-12-09T10:00): Says one product is paid and disputes the other
- S7-26 (hard, now 2026-12-10T10:20): Mentions losing his job after the first offer, accepts the welfare team
- S7-27 (hard, now 2026-12-14T10:00): Bereavement, declines the welfare team, still names an amount and a day
- S7-28 (hard, now 2026-12-15T09:45): Asks for a payment plan after a promise was recorded
- S7-29 (medium, now 2026-12-16T10:10): Asks the agent to repeat the amount and days late
- S7-30 (hard, now 2026-11-02T11:00): Amount in words with cents, date in words
- S7-31 (hard, now 2026-11-24T10:00): 'The 3rd' without a month

### Multi-product (4)
- S7-32 (hard, now 2026-12-02T10:00): One date for all three products, past one deadline
- S7-33 (hard, now 2026-11-12T10:00): Promises one, refuses one, disputes one
- S7-34 (hard, now 2026-12-11T09:30): Nothing agreed, different reasons, precedence on the close reason
- S7-35 (hard, now 2026-12-17T10:30): Agrees one promise, then asks for a plan on the other product

### Tool failure (5)
- S7-36 (medium, now 2026-10-29T10:00): create_promise fails once, works on the second try
- S7-37 (hard, now 2026-10-30T11:00): create_promise fails twice
- S7-38 (hard, now 2026-11-30T10:00): One of two promises fails twice, the other is recorded
- S7-39 (medium, now 2026-12-01T09:00): schedule_callback fails once
- S7-40 (hard, now 2026-12-07T10:00): add_note fails once with two products refused
