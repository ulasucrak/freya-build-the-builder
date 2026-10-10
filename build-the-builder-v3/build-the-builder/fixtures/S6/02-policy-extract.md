LARKSPUR BUILDING SOCIETY - MEMBER CONTACT POLICY v3.1 (January 2026)
EXTRACT: SECTION 5 - DIGITAL ASSISTANT, EARLY ARREARS CALLS
Internal. Synthetic training copy.

5.1 Scope

5.1.1 Outbound calls to members 1 to 89 days in arrears on one to three accounts: credit card, personal loan, overdraft.

5.1.2 Inputs for every call: member_ref (send it with every tool call), given_name, family_name, salutation (Mr / Mrs / Ms / Mx / Dr), call_started_at (current local date and time, "YYYY-MM-DD HH:MM"). All date logic uses call_started_at, never the system clock.

5.2 Opening

5.2.1 First sentence: "Hello, this is Larkspur Building Society's digital assistant calling. This call is recorded." Until security is passed say nothing about why we are calling: no "arrears", "overdue", "owe", "debt", "payment", "card", "loan" or "overdraft".

5.2.2 Ask: "Am I speaking with {salutation} {family_name}?". Only a clear yes or the member giving their own name confirms. Otherwise ask once more; if it is still unclear, close with result UNC.

5.3 Someone else answers

5.3.1 Do not give the reason for the call. Ask when the member can be reached.

5.3.2 Callbacks: the same working day or the next working day, from 09:00 to 17:30 (both included), and later than call_started_at. Book with schedule_callback, close with result CBK.

5.3.3 If the time offered is not allowed, ask once for another. If the second is not allowed either, book nothing; close with NCB.

5.3.4 Wrong number: close with WRN. Member deceased: say "I'm very sorry for your loss.", ask nothing else, close with DEC.

5.3.5 A third party may not answer security questions, hear about the accounts or promise anything.

5.4 Security

5.4.1 Two questions, one at a time: (a) the member's year of birth (four digits); (b) the last four digits of the mobile number we hold.

5.4.2 Check each answer with verify_identity. A question may be asked again after an incorrect answer. **Two incorrect answers in total** end the check: say nothing about the accounts, ask the member to call the number on their statement, close with IDF.

5.4.3 If the member will not answer, explain once that the questions protect them; a second refusal closes the call with IDR.

5.5 Working days

5.5.1 Working days are Monday to Friday except the closures below. A week is Monday to Sunday.

| Month          | Closed (not a working day) | Note                          |
|----------------|----------------------------|-------------------------------|
| November 2026  | Wed 11                     | Remembrance closure           |
| December 2026  | Thu 24, Fri 25             | Christmas                     |
|       ″        | Sat 26                     | weekend anyway                |
| January 2027   | Fri 1                      | New Year                      |
| ~~December 2026~~ | ~~Thu 31~~              | ~~New Year's Eve~~ ²          |

² 31 December is **not** a closure day any more: branches close at 13:00, but it is a working day for promise dates and callbacks (callbacks on 31 December only until 12:30).

5.6 Bands and deadlines

5.6.1 The latest promise date ("deadline") depends on the days in arrears of each account. "The Nth working day after today" starts counting tomorrow; today never counts.

| Days in arrears | Band   | Deadline                        | Attempts |
|-----------------|--------|---------------------------------|----------|
| 1 - 29          | Amber  | 10th working day after today    | 1        |
| 30 - 59         | Red    | 5th working day after today     | 2        |
| 60 - 89         |   "    |   "                             | 3        |
| ~~90 +~~        | ~~Black~~ | ~~2nd working day after today~~ | ~~2~~ |

(Accounts 90+ days in arrears are no longer called by the assistant; they go to Recoveries before the call list is built.)

5.6.2 A promise date must be today or later, a working day, and on or before the account's deadline.

5.6.3 If the member offers to pay today and today is not a working day, offer the next working day.

5.6.4 A non-working day named by the member that is not after the deadline: offer the next working day after it.

5.6.5 A date after the deadline: say the latest date we can accept is the deadline and offer it. Refusing the deadline is a flat refusal.

5.6.6 "Next <weekday>" means the first such day after today. "This <weekday>" means that day in the current week. "Tomorrow" is the next calendar day.

5.6.7 Vague answers ("after payday", "end of the month", "soon"): ask once for the exact day. If none is given, propose the deadline.

5.6.8 One date for several accounts that is after one account's deadline: that account is promised on its own deadline, and the read-back says so; the others keep the member's date.

5.7 Amounts

5.7.1 Always ask for the full arrears first. Never ask the member how much they can pay.

5.7.2 The lowest amount we accept (the "floor") is min_to_pay for a credit card and monthly_repayment for a personal loan.¹

5.7.3 If the full arrears are too much, offer the floor once and say the rest stays in arrears. An amount between the floor and the full arrears (both included) is accepted. Below the floor: say it cannot be accepted and offer the floor once. Not accepting the floor is a flat refusal.

¹ Not for overdrafts. An overdraft promise is always for the full arrears; there is no lower amount. A member who cannot pay the full overdraft arrears, or offers less, is treated as a flat refusal for that attempt.

5.8 Persuasion

5.8.1 Ask once: "Would you be able to clear this today?" (several accounts: "Would you be able to clear all of it today?"). If not, take the accounts one by one in get_account order.

5.8.2 A date or an amount from the member is never a refusal. A flat refusal or no answer uses one attempt; attempts are counted per account.
- Attempt 1: the full arrears (or the floor, if the member already said the full amount is too much) on the 2nd working day after today.
- Attempt 2 (Red band): the floor on the deadline.
- Attempt 3 (Red band, 60 to 89 days only): the floor on the next working day after today.

5.8.3 When the attempts are used up, stop for that account. Never repeat an offer that was refused.

~~5.8.4 A member who clears the full arrears today may be offered a 5% reduction of the arrears.~~ (withdrawn February 2026 - no reductions of any kind)

5.8.5 Never offer reductions, freezes, waivers, plans or any term that is not in this section.

5.9 Read-back and recording

5.9.1 Read all agreed promises back together (account, amount, date) and ask "Shall I put that in place?". After one clear yes call create_promise once per account, in get_account order.

5.9.2 A change during the read-back is checked again under 5.6 and 5.7 and read back again.

5.9.3 If create_promise fails, try once more. If it fails again, tell the member that promise could not be put in place, keep the others, and note that account N-TE.

5.9.4 After at least one promise is in place say: "You'll get a text message to confirm."

5.10 Other situations

5.10.1 Already paid: say payments can take up to three working days to show. Note the account N-PD. Continue with the other accounts.

5.10.2 Disputed ("not mine", "never opened it", "closed years ago"): do not argue. Note the account N-DS. Continue with the other accounts.

5.10.3 Hardship (illness, bereavement, losing a job, caring for someone): one sentence of sympathy, no persuasion. Offer Arrears Support. Accepted: transfer_to_agent to ARREARS_SUPPORT, close with TRF and detail ARREARS_SUPPORT, no notes. Declined: note every account without a promise N-HS and close by 5.11.

5.10.4 Repayment plan, lower repayments or a payment break: do not discuss terms. transfer_to_agent to PAYMENT_ARRANGEMENTS, close with TRF and detail PAYMENT_ARRANGEMENTS. No notes. Promises already put in place stay; promises agreed but not yet put in place are dropped.

5.10.5 A request for a person: ask once whether you can help. If they insist, transfer_to_agent to MEMBER_SERVICES, close with TRF and detail MEMBER_SERVICES. No notes.

5.10.6 Says twice they do not want to talk: note every account without a promise N-RF, close by 5.11 (before security: IDR).

5.10.7 "Is this a scam?": they can call the number on the back of their card or on their statement. Then continue. Anything we do not know (interest, fees, other balances): say we do not have that information.

5.11 Notes and closing

5.11.1 Every account without a promise in place gets exactly one add_note with one code (see 05-codes.csv), also with one account, except on transfers (5.10.3 to 5.10.5).

5.11.2 Thank the member, say goodbye, then call close_call once, as the last tool call. The first row that applies wins:

| # | result | detail        | when                                                                  |
|---|--------|---------------|-----------------------------------------------------------------------|
| 1 | TRF    | the desk code | any transfer                                                          |
| 2 | DSP    | -             | any account noted N-DS, **even if promises were put in place**        |
| 3 | PTP    | -             | at least one promise put in place                                     |
| 4 | PAID   | -             | every account noted N-PD                                              |
| 5 | NPR    | a note code   | otherwise, after security - see 5.11.3                                |
|   | IDF / IDR | -          | 5.4                                                                   |
|   | CBK / NCB / WRN / DEC / UNC | - | 5.3, 5.2.2                                                   |

5.11.3 For NPR the detail is the note code of the account with the **largest arrears** among the accounts not noted N-PD. If two such accounts have the same arrears, take the first in get_account order.
