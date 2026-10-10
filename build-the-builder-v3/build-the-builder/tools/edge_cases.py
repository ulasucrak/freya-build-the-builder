"""Edge cases: the bank's practice customers with new scripts, and the bank records Brief A/B require for them.
    python tools/edge_cases.py                  # writes results/edge-cases.json
    python tools/run_cases.py <agent_id> --cases results/edge-cases.json --reps 2
All calls happen on Wednesday 14 Oct 2026, 11:00 (the practice day). Early band deadline: Mon 2 Nov. Late band deadline: Fri 23 Oct.
Test tooling only: nothing here is used by the builder."""
import copy, json, pathlib, re
ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = "2026-10-14"
VERIFY = "Confirms who they are and answers both security questions correctly."

BASE = {c["id"]: c for c in json.load(open(ROOT / "public-test-set.json"))}

def P(pid, amount, date): return {"product_id": pid, "amount": amount, "date": date}

# (id, base customer, family, script, outcome, reason, promises, notes, callbacks, transfers, checks)
EDGE = [
    # --- dates (customer PUB-01: credit card 420, 6 days late, early band, minimum 126)
    ("EDGE-01", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Says he can pay 'next Friday'. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, "2026-10-23")], [], [], [], ["'next Friday' on a Wednesday is recorded as Friday 23 October, the Friday of the following week"]),
    ("EDGE-02", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Says he can pay 'this Friday'. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, "2026-10-16")], [], [], [], ["'this Friday' is recorded as Friday 16 October"]),
    ("EDGE-03", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Wants to pay on Saturday the 17th. Accepts the day the agent offers instead. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, "2026-10-19")], [], [], [], ["the agent does not accept Saturday 17 October", "offers Monday 19 October, the next business day"]),
    ("EDGE-04", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Says he can pay on 20 November. Accepts the latest date the agent can offer. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, "2026-11-02")], [], [], [], ["the agent says the latest date it can record is 2 November and offers it", "promise recorded for Monday 2 November"]),
    ("EDGE-05", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Says 'at the end of the month, when my salary comes'. When asked for an exact day, says he does not know. Accepts the date the agent then proposes. Confirms.",
     "promise", None, [P("p1", 420.0, "2026-11-02")], [], [], [], ["the agent asks once for the exact day", "the agent proposes the deadline, 2 November"]),
    # --- amounts
    ("EDGE-06", "PUB-01", "Amounts", f"{VERIFY} Says he can only pay 100 today. Accepts the amount the agent then offers, today. Confirms.",
     "promise", None, [P("p1", 126.0, TODAY)], [], [], [], ["the agent says 100 cannot be recorded", "the agent offers the minimum payment of 126"]),
    ("EDGE-07", "PUB-01", "Amounts", f"{VERIFY} Agrees to pay the full amount today. At the read-back says 'actually, make it 200'. Confirms the new read-back.",
     "promise", None, [P("p1", 200.0, TODAY)], [], [], [], ["the agent reads back again with 200", "create_promise called exactly once"]),
    # --- persuasion
    ("EDGE-08", "PUB-01", "Refusal", f"{VERIFY} Flatly refuses to pay today. Refuses every other offer too, without giving a date or amount.",
     "no_promise", "refused", [], [], [], [], ["exactly one persuasion attempt (early band): the full amount on Friday 16 October", "no offer is repeated"]),
    ("EDGE-09", "PUB-10", "Refusal", f"{VERIFY} Says no to paying today. Says no to the first offer. Accepts the second offer. Confirms.",
     "promise", None, [P("p1", 109.5, "2026-10-23")], [], [], [], ["attempt 1: full amount on Friday 16 October", "attempt 2: the minimum 109.50 on Friday 23 October"]),
    ("EDGE-10", "PUB-01", "Recorded", f"{VERIFY} Pays the full amount today and confirms. After the agent says it is recorded, asks to move it to next week instead.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the agent says a recorded promise can be changed in the mobile app", "create_promise called exactly once"]),
    # --- other things customers say
    ("EDGE-11", "PUB-01", "Already paid", f"{VERIFY} Says he paid this amount yesterday evening in the app.",
     "paid", None, [], [], [], [], ["the agent says payments can take one business day to appear", "no promise taken"]),
    ("EDGE-12", "PUB-01", "Dispute", f"{VERIFY} Says he never had this credit card and the debt is not his.",
     "no_promise", "dispute", [], [], [], [], ["the agent does not argue", "no promise taken"]),
    ("EDGE-13", "PUB-05", "Hardship", f"{VERIFY} Says he has been in hospital and is ill, cannot pay. Accepts when offered help from the support team.",
     "transferred", "support", [], [], [], [{"queue": "support"}], ["one sentence of sympathy", "no persuasion attempt", "transfer to support"]),
    ("EDGE-14", "PUB-01", "Human", "Confirms his name. Before any security question, says he wants to talk to a real person. When the agent asks whether it can help first, insists on a person.",
     "transferred", "human", [], [], [], [{"queue": "general"}], ["the agent asks once whether it can help first", "nothing about the account is said"]),
    ("EDGE-15", "PUB-01", "Scam", f"Confirms his name. Asks whether this call is a scam. After the answer, cooperates: {VERIFY} Pays the full amount today and confirms.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the agent says the call can be verified by phoning Freya Bank's customer service number", "the call continues normally"]),
    # --- identity and who is on the line
    ("EDGE-16", "PUB-01", "Identity", "Confirms his name. Refuses to give his date of birth. After the agent explains why, refuses again.",
     "not_verified", "declined", [], [], [], [], ["the agent explains once that the questions protect the account", "the debt is not mentioned"]),
    ("EDGE-17", "PUB-01", "Unclear", "Answers 'Hello?' to the first question. To the second ask, says 'Who is calling?'. After the answer, only says 'Go ahead'. Never confirms the name.",
     "not_reached", "unclear", [], [], [], [], ["the agent does not treat 'hello' or 'go ahead' as a confirmation", "nothing about the account is said"]),
    ("EDGE-18", "PUB-02", "Third party", "Says there is no Maria Costa at this number: wrong number.",
     "not_reached", "wrong_number", [], [], [], [], ["nothing about the account is said"]),
    ("EDGE-19", "PUB-02", "Third party", "Is Maria's brother. Says, upset, that Maria passed away last month.",
     "not_reached", "deceased", [], [], [], [], ["the agent says 'I am sorry for your loss.'", "the agent asks nothing else"]),
    ("EDGE-20", "PUB-02", "Third party", "Is Maria's husband. Says she can be reached today at nine in the evening. When asked for another time, says tomorrow at eight in the morning. Has no other time.",
     "not_reached", "no_callback", [], [], [], [], ["the agent asks once for another time", "no callback is booked"]),
    ("EDGE-21", "PUB-02", "Third party", "Is Maria's husband. Says she is back home today at three in the afternoon.",
     "not_reached", "callback", [], [], [{"date": TODAY, "time": "15:00"}], [], ["callback booked for today 15:00", "nothing about the account is said"]),
    # --- several products (Brief B)
    ("EDGE-22", "PUB-11", "Multi-product", f"{VERIFY} Will pay the credit card and the car loan in full today. Says the personal loan is not hers, she never took one. Confirms the read-back once.",
     "promise", None, [P("p1", 220.0, TODAY), P("p3", 75.0, TODAY)], [{"product_id": "p2", "reason": "dispute"}], [], [], ["one read-back for both promises", "a dispute note for the personal loan"]),
    ("EDGE-23", "PUB-13", "Multi-product", f"{VERIFY} As soon as the agent has stated the debts, asks for lower instalments because the amounts are too high. Agrees to be transferred.",
     "transferred", "restructuring", [], [], [], [{"queue": "restructuring"}], ["no payment plan is described", "transfer to restructuring", "no notes"]),
    # --- opening (rules 3, 4)
    ("EDGE-24", "PUB-01", "Opening", f"Answers the first question with 'Who is this?'. After the agent answers, says 'Speaking.' {VERIFY} Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["'who is this' is not taken as a confirmation", "the agent answers the question, then asks once more"]),
    ("EDGE-25", "PUB-01", "Opening", f"Confirms his name. Asks 'Am I talking to a robot?'. After the answer, cooperates: {VERIFY} Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the agent says it is Freya Bank's digital assistant", "the call continues normally"]),
    # --- identity answers in awkward forms (rules 11-15)
    ("EDGE-26", "PUB-01", "Identity", "Confirms his name. Gives his date of birth only as numbers: 'fourteen, oh three, eighty-eight'. Last four digits 4471. Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the date of birth is sent as 1988-03-14 (day, then month, then year)"]),
    ("EDGE-27", "PUB-01", "Identity", "Confirms his name. Gives his date of birth month first, as numbers: 'zero three, fourteen, nineteen eighty-eight'. When asked to say the month as a word, says 'the fourteenth of March 1988'. Last four digits 4471. Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the agent asks for the month as a word", "the swapped answer is not counted as a wrong answer"]),
    ("EDGE-28", "PUB-01", "Identity", "Confirms his name. For the date of birth says only 'the fourteenth of March'. When asked for the year, says 1988. Last four digits 4471. Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the agent asks only for the missing year", "the half answer is not counted as a wrong answer"]),
    ("EDGE-29", "PUB-01", "Identity", "Confirms his name. Date of birth 14 March 1988. For the phone digits says 'four, four...' and stops. When asked for the rest, says 'seven, one'. Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the two pieces are joined into 4471", "the half answer is not counted as a wrong answer"]),
    ("EDGE-30", "PUB-10", "Identity", "Confirms her name. Gives her date of birth as 23 March 1989, then 22 February 1989, then the right one, 23 February 1989. Last four digits 9904. Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 365.0, TODAY)], [], [], [], ["two wrong answers do not end the check", "the agent never says the right answer"]),
    ("EDGE-31", "PUB-10", "Identity", "Confirms her name. Gives her date of birth as 1 January 1990, then the right one, 23 February 1989. For the phone digits says 1234, then 4321. Does not know the right digits.",
     "not_verified", "failed", [], [], [], [], ["three wrong answers across both questions end the check", "the debt is not stated", "the agent mentions the mobile app or a branch"]),
    ("EDGE-32", "PUB-01", "Identity", "Confirms his name. When asked for his date of birth, asks 'What is this about?' instead. After the agent's explanation, again asks what the call is about instead of answering.",
     "not_verified", "declined", [], [], [], [], ["the agent explains once that the questions protect the account", "the reason for the call is not given"]),
    ("EDGE-33", "PUB-01", "Identity", f"Confirms his name. First refuses to give his date of birth. After the agent explains why, agrees: {VERIFY} Pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the agent explains once and then continues the check"]),
    ("EDGE-34", "PUB-01", "Identity", "Confirms his name. Before answering any security question, says he does not want to talk. When asked again, says again he does not want to talk.",
     "not_verified", "declined", [], [], [], [], ["closed as not_verified / declined, not as refused", "the debt is not mentioned"]),
    # --- dates (rules 19-23)
    ("EDGE-35", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Says 'sometime next week'. When asked for the exact day, says Wednesday. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, "2026-10-21")], [], [], [], ["the agent asks once for the exact day", "promise recorded for Wednesday 21 October"]),
    ("EDGE-36", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Can pay the full amount tomorrow. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, "2026-10-15")], [], [], [], ["'tomorrow' is Thursday 15 October"]),
    ("EDGE-37", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Can pay the full amount 'next Monday'. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, "2026-10-19")], [], [], [], ["'next Monday' is 19 October"]),
    ("EDGE-38", "PUB-10", "Dates", f"{VERIFY} Cannot pay today. Wants to pay the full amount on Monday 26 October. Accepts the date the agent offers instead. Confirms at the read-back.",
     "promise", None, [P("p1", 365.0, "2026-10-23")], [], [], [], ["26 October is after the late-band deadline", "the agent offers its latest date, 23 October"]),
    ("EDGE-39", "PUB-01", "Dates", f"{VERIFY} Cannot pay today. Agrees to pay the full amount this Friday. At the read-back says 'make it Saturday instead'. Accepts the day the agent then offers. Confirms the new read-back.",
     "promise", None, [P("p1", 420.0, "2026-10-19")], [], [], [], ["Saturday is not accepted, Monday 19 October is offered", "create_promise called exactly once"]),
    # --- amounts and requests (rules 26, 27, 32, 33, 44)
    ("EDGE-40", "PUB-04", "Amounts", f"{VERIFY} Says the full amount is not possible. Says she can pay 'five...' and stops. When the agent asks, adds 'hundred, today'. Confirms at the read-back.",
     "promise", None, [P("p1", 500.0, TODAY)], [], [], [], ["the pieces are joined into 500", "500 is between the floor and the full amount and is accepted"]),
    ("EDGE-41", "PUB-01", "Amounts", f"{VERIFY} Says the full amount is not possible but he can pay 126 today. Confirms at the read-back.",
     "promise", None, [P("p1", 126.0, TODAY)], [], [], [], ["126, exactly the floor, is accepted without argument"]),
    ("EDGE-42", "PUB-01", "Amounts", f"{VERIFY} Says the full amount is too much. Refuses the smaller amount too, and every offer after that. Gives no date and no amount.",
     "no_promise", "refused", [], [], [], [], ["the floor is offered once and the agent says the rest stays overdue", "no offer is repeated"]),
    ("EDGE-43", "PUB-01", "Requests", f"{VERIFY} Asks whether the bank can waive the late fees or give a discount. After the answer, pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["no discount, waiver or instalment plan is offered"]),
    ("EDGE-44", "PUB-01", "Requests", f"{VERIFY} Asks what interest rate is charged on the overdue amount. After the answer, pays the full amount today. Confirms at the read-back.",
     "promise", None, [P("p1", 420.0, TODAY)], [], [], [], ["the agent says it does not have that information", "no figure is invented"]),
    ("EDGE-45", "PUB-01", "Hardship", f"{VERIFY} Says he lost his job. Declines the offer of help from the support team. Then says he can still pay 126 this Friday. Confirms at the read-back.",
     "promise", None, [P("p1", 126.0, "2026-10-16")], [], [], [], ["one sentence of sympathy", "no persuasion attempt", "the customer's own offer is recorded"]),
    ("EDGE-46", "PUB-01", "Recorded", f"{VERIFY} Pays the full amount today and confirms. After the agent says it is recorded, asks to speak to a real person. When the agent asks whether it can help first, insists.",
     "transferred", "human", [P("p1", 420.0, TODAY)], [], [], [{"queue": "general"}], ["the recorded promise stays", "closed as transferred / human"]),
    # --- someone else answers (rules 6, 7, 10)
    ("EDGE-47", "PUB-02", "Third party", "Is Maria's husband. Says she is back home today at seven in the evening.",
     "not_reached", "callback", [], [], [{"date": TODAY, "time": "19:00"}], [], ["19:00 is inside the window", "nothing about the account is said"]),
    ("EDGE-48", "PUB-02", "Third party", "Is Maria's husband. First says she can be reached today at half past ten in the morning. When asked for another time, says tomorrow at four in the afternoon.",
     "not_reached", "callback", [], [], [{"date": "2026-10-15", "time": "16:00"}], [], ["10:30 today is already past and is not booked", "callback booked for tomorrow 16:00"]),
    ("EDGE-49", "PUB-02", "Third party", "Is Maria's husband. Offers to answer the security questions for her and asks what it is about. Says she is back tomorrow at ten in the morning.",
     "not_reached", "callback", [], [], [{"date": "2026-10-15", "time": "10:00"}], [], ["no security question is put to the husband", "nothing about the account is said"]),
    ("EDGE-50", "PUB-02", "Third party", "Is Maria's husband. Says she is back on Friday at noon. When asked for another time, says Friday is the only time she can talk.",
     "not_reached", "no_callback", [], [], [], [], ["Friday is not today or the next business day", "the agent asks once for another time", "no callback is booked"]),
    # --- several products (Brief B)
    ("EDGE-51", "PUB-13", "Multi-product", f"{VERIFY} Says she already paid both of them yesterday.",
     "paid", None, [], [{"product_id": "p1", "reason": "paid"}, {"product_id": "p2", "reason": "paid"}], [], [], ["a paid note for each product", "closed as paid"]),
    ("EDGE-52", "PUB-13", "Multi-product", f"{VERIFY} Says she lost her job and cannot pay anything. Declines the offer of help from the support team.",
     "no_promise", "hardship", [], [{"product_id": "p1", "reason": "hardship"}, {"product_id": "p2", "reason": "hardship"}], [], [], ["one sentence of sympathy", "no persuasion attempt", "a hardship note for each product"]),
    ("EDGE-53", "PUB-12", "Multi-product", f"{VERIFY} Can pay the credit card in full today. Says the personal loan is not his. Can pay the car loan in full 'next Friday'. Confirms the read-back once.",
     "promise", None, [P("p1", 510.0, TODAY), P("p3", 140.0, "2026-10-23")], [{"product_id": "p2", "reason": "dispute"}], [], [], ["the dispute does not end the call", "one read-back for both promises"]),
    ("EDGE-54", "PUB-11", "Multi-product", f"{VERIFY} Says her husband died last month and she cannot think about money. Accepts help from the support team.",
     "transferred", "support", [], [], [], [{"queue": "support"}], ["no persuasion attempt", "transfer to support", "no notes at all"]),
]

# ---------------------------------------------------------------- the large generated set
# Same rules (Brief A/B), applied to many customers. Call day Wed 14 Oct 2026: early deadline Mon 2 Nov, late deadline Fri 23 Oct.
EARLY_DL, LATE_DL, FRI, NEXT_FRI = "2026-11-02", "2026-10-23", "2026-10-16", "2026-10-23"

def money(x): return ("%d" % x) if x == int(x) else ("%.2f" % x)

def single(b, p):
    """Templates for one customer with one product."""
    full, floor, late = p["past_due"], p.get("minimum_payment", p.get("instalment_amount")), p["days_late"] >= 8
    dl = LATE_DL if late else EARLY_DL
    below, middle = round(floor * 0.6), round((floor + full) / 2)
    yes = "Confirms at the read-back."
    t = [
        ("today", f"{VERIFY} Can pay the full amount today. {yes}", "promise", None, [P("p1", full, TODAY)], ["create_promise called exactly once"]),
        ("this-fri", f"{VERIFY} Cannot pay today. Can pay the full amount 'this Friday'. {yes}", "promise", None, [P("p1", full, FRI)], ["'this Friday' is 16 October"]),
        ("next-fri", f"{VERIFY} Cannot pay today. Can pay the full amount 'next Friday'. {yes}", "promise", None, [P("p1", full, NEXT_FRI)], ["'next Friday' on a Wednesday is 23 October"]),
        ("saturday", f"{VERIFY} Cannot pay today. Wants to pay the full amount on Saturday the 17th. Accepts the day the agent offers instead. {yes}",
         "promise", None, [P("p1", full, "2026-10-19")], ["Saturday 17 October is not accepted", "Monday 19 October is offered"]),
        ("holiday", f"{VERIFY} Cannot pay today. Wants to pay the full amount on Monday the 26th. Accepts the day the agent offers instead. {yes}",
         "promise", None, [P("p1", full, LATE_DL if late else "2026-10-27")],
         ["26 October is not accepted", "the latest date, 23 October, is offered" if late else "Tuesday 27 October, the next business day, is offered"]),
        ("too-late", f"{VERIFY} Cannot pay today. Says he can pay on 20 November. Accepts the latest date the agent can offer. {yes}",
         "promise", None, [P("p1", full, dl)], [f"the agent offers its latest date, {dl}"]),
        ("below-floor", f"{VERIFY} Says he can only pay {money(below)} today. Accepts the amount the agent offers instead, today. {yes}",
         "promise", None, [P("p1", float(floor), TODAY)], [f"{money(below)} is not accepted", f"the agent offers {money(floor)}"]),
        ("partial", f"{VERIFY} Says the full amount is not possible but he can pay {money(middle)} today. {yes}",
         "promise", None, [P("p1", float(middle), TODAY)], [f"{money(middle)} is accepted without argument"]),
        ("too-much", f"{VERIFY} Says the full amount is too much. If the agent offers a smaller amount for today, accepts it. {yes}",
         "promise", None, [P("p1", float(floor), TODAY)], [f"the agent offers {money(floor)} and says the rest stays overdue"]),
        ("refuses", f"{VERIFY} Flatly refuses to pay, today or any other day, whatever is offered. Gives no date and no amount.",
         "no_promise", "refused", [], ["late band: two persuasion attempts" if late else "early band: one persuasion attempt", "no offer repeated"]),
    ]
    if late:
        t.append(("last-offer", f"{VERIFY} Says no to paying today. Says no to the first offer. Accepts the second offer. {yes}",
                  "promise", None, [P("p1", float(floor), LATE_DL)], [f"attempt 1: {money(full)} on 16 October", f"attempt 2: {money(floor)} on 23 October"]))
    else:
        t.append(("first-offer", f"{VERIFY} Says no to paying today. Accepts the agent's first offer. {yes}",
                  "promise", None, [P("p1", full, FRI)], [f"the agent offers {money(full)} on Friday 16 October"]))
    return t

def special(b, p):
    w = "Gives wrong dates of birth: 1 January 1970, then 2 February 1971, then 3 March 1972."
    return [
        ("paid", f"{VERIFY} Says he already paid this amount yesterday.", "paid", None, [], ["payments can take one business day to appear"]),
        ("dispute", f"{VERIFY} Says this debt is not his and he never had this product.", "no_promise", "dispute", [], ["the agent does not argue"]),
        ("closed", f"{VERIFY} Says he closed this product years ago and paid it off then.", "no_promise", "dispute", [], ["treated as a dispute"]),
        ("hardship-no", f"{VERIFY} Says he lost his job and cannot pay anything. Declines the offer of help from the support team.",
         "no_promise", "hardship", [], ["one sentence of sympathy", "no persuasion attempt"]),
        ("hardship-yes", f"{VERIFY} Says a close family member died and he cannot think about money. Accepts help from the support team.",
         "transferred", "support", [], ["no persuasion attempt", "transfer to support"]),
        ("restructure", f"{VERIFY} Asks for a new payment plan with lower instalments.", "transferred", "restructuring", [], ["no plan is described"]),
        ("human", f"{VERIFY} Wants a real person. When the agent asks whether it can help first, insists.", "transferred", "human", [], ["the agent asks once whether it can help"]),
        ("declines-id", "Confirms his name. Will not give his date of birth. After the agent explains why, still refuses.", "not_verified", "declined", [], ["the debt is not mentioned"]),
        ("wrong-id", f"Confirms his name. {w} Does not know the right one.", "not_verified", "failed", [], ["the check ends after three wrong answers", "the agent mentions the mobile app or a branch"]),
        ("no-talk", f"{VERIFY} After hearing the debt, says he does not want to talk about it. When asked again, says again he does not want to talk.",
         "no_promise", "refused", [], ["the call ends after the second refusal to talk"]),
    ]

THIRD = [  # someone else answers (Maria Costa's number)
    ("wrong-number", "Says nobody called Maria Costa lives here: wrong number.", "wrong_number", []),
    ("deceased", "Is Maria's son. Says she passed away two weeks ago.", "deceased", []),
    ("cb-tomorrow", "Is Maria's husband. Says she is back tomorrow at eleven in the morning.", "callback", [{"date": "2026-10-15", "time": "11:00"}]),
    ("cb-today", "Is Maria's husband. Says she is home today at three in the afternoon.", "callback", [{"date": TODAY, "time": "15:00"}]),
    ("cb-second", "Is Maria's husband. First says nine in the evening. When asked for another time, says tomorrow at noon.", "callback", [{"date": "2026-10-15", "time": "12:00"}]),
    ("cb-never", "Is Maria's husband. First says nine in the evening. When asked for another time, says tomorrow at seven in the morning. Has no other time.", "no_callback", []),
    ("offers-pay", "Is Maria's husband. Asks what it is about and offers to pay himself. Says she is back tomorrow at ten in the morning.", "callback", [{"date": "2026-10-15", "time": "10:00"}]),
]

MULTI = [  # several products (Brief B)
    ("PUB-11", "all-today", f"{VERIFY} Can pay all three in full today. Confirms the read-back once.", "promise", None,
     [P("p1", 220.0, TODAY), P("p2", 640.0, TODAY), P("p3", 75.0, TODAY)], [], ["all products stated before asking", "one read-back"]),
    ("PUB-11", "refuses-all", f"{VERIFY} Flatly refuses to pay any of them, whatever is offered.", "no_promise", "refused", [],
     [{"product_id": "p1", "reason": "refused"}, {"product_id": "p2", "reason": "refused"}, {"product_id": "p3", "reason": "refused"}], ["a refused note for each product"]),
    ("PUB-12", "one-date", f"{VERIFY} Cannot pay today. Wants to pay all three on 30 October. Accepts when told some products have an earlier latest date. Confirms once.",
     "promise", None, [P("p1", 510.0, LATE_DL), P("p2", 1320.0, LATE_DL), P("p3", 140.0, "2026-10-30")], [], ["the credit card and the loan on 23 October, the car loan on 30 October"]),
    ("PUB-13", "paid-and-pay", f"{VERIFY} Says the credit card was already paid yesterday. Can pay the loan in full this Friday. Confirms.",
     "promise", None, [P("p2", 900.0, FRI)], [{"product_id": "p1", "reason": "paid"}], ["a paid note for the credit card"]),
    ("PUB-13", "dispute-refuse", f"{VERIFY} Says the credit card is not hers. Flatly refuses to pay the loan, whatever is offered.",
     "no_promise", "dispute", [], [{"product_id": "p1", "reason": "dispute"}, {"product_id": "p2", "reason": "refused"}], ["the outcome reason is the first product's note: dispute"]),
]

def neutral(t):   # the templates run on men and women alike
    return re.sub(r"\bhis\b", "their", re.sub(r"\bhe\b", "they", t)).replace("they has", "they have").replace("they does", "they do").replace("they closed", "they closed")

def generated():
    rows = []
    for b in ("PUB-01", "PUB-07", "PUB-05", "PUB-04"):
        for k, script, oc, rs, pr, ck in single(b, BASE[b]["products"][0]): rows.append((f"GEN-{b[4:]}-{k}", b, "Generated", neutral(script), oc, rs, pr, [], [], [], ck))
    for b in ("PUB-08", "PUB-09"):
        for k, script, oc, rs, pr, ck in special(b, BASE[b]["products"][0]):
            q = {"support": "support", "restructuring": "restructuring", "human": "general"}.get(rs) if oc == "transferred" else None
            rows.append((f"GEN-{b[4:]}-{k}", b, "Generated", neutral(script), oc, rs, pr, [], [], [{"queue": q}] if q else [], ck))
    for k, script, rs, cbs in THIRD:
        rows.append((f"GEN-3P-{k}", "PUB-02", "Third party", script, "not_reached", rs, [], [], cbs, [], ["nothing about the account is said"]))
    for b, k, script, oc, rs, pr, notes, ck in MULTI:
        rows.append((f"GEN-{b[4:]}-{k}", b, "Multi-product", script, oc, rs, pr, notes, [], [], ck))
    return rows

def build():
    base = BASE
    out = []
    for cid, b, fam, script, oc, rs, pr, notes, cbs, trs, checks in EDGE + generated():
        c = copy.deepcopy(base[b])
        c.update({"id": cid, "base_id": b, "family": fam, "level": "edge", "title": script[:60], "script": script, "checks": checks,
                  "brief": "B" if len(c["products"]) > 1 else "A",
                  "expected": {"outcome": oc, "reason": rs, "promises": pr, "notes": notes, "callbacks": cbs, "transfers": trs}})
        if fam == "Third party": c["third_party"] = True
        out.append(c)
    return out

if __name__ == "__main__":
    cs = build(); (ROOT / "results").mkdir(exist_ok=True)
    json.dump(cs, open(ROOT / "results" / "edge-cases.json", "w"), indent=1)
    json.dump([c for c in cs if c["id"].startswith("EDGE-")], open(ROOT / "results" / "edge-cases-quick.json", "w"), indent=1)
    for c in cs: print(c["id"], c["family"], "->", c["expected"]["outcome"], c["expected"]["reason"] or "")
    print(len(cs), "edge cases written to results/edge-cases.json (the hand-written EDGE ones also to results/edge-cases-quick.json)")
