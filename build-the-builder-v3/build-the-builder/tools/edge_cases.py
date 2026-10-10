"""Edge cases: the bank's practice customers with new scripts, and the bank records Brief A/B require for them.
    python tools/edge_cases.py                  # writes results/edge-cases.json
    python tools/run_cases.py <agent_id> --cases results/edge-cases.json --reps 2
All calls happen on Wednesday 14 Oct 2026, 11:00 (the practice day). Early band deadline: Mon 2 Nov. Late band deadline: Fri 23 Oct.
Test tooling only: nothing here is used by the builder."""
import copy, json, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = "2026-10-14"
VERIFY = "Confirms who they are and answers both security questions correctly."

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
]

def build():
    base = {c["id"]: c for c in json.load(open(ROOT / "public-test-set.json"))}
    out = []
    for cid, b, fam, script, oc, rs, pr, notes, cbs, trs, checks in EDGE:
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
    for c in cs: print(c["id"], c["family"], "->", c["expected"]["outcome"], c["expected"]["reason"] or "")
    print(len(cs), "edge cases written to results/edge-cases.json")
