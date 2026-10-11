"""About 1000 diverse live cases, Brief A (one product) and Brief B (two or three products), for tools/run_cases.py.

    python tools/gen_cases_1000.py --n 1000 --seed 7 --out results/cases-1000.json --split 250
    python tools/run_cases.py <agent_id> --cases results/cases-1000-part1.json

The bank mock knows only the 13 practice customers (base_id), so diversity comes from the call day and time (`now`),
the situation (family), the way amounts and dates are said, and the customer's way of talking (style).
Every expected record comes from the small calculator in tools/edge_cases.py (written from the brief's text, not from
the builder's oracle) plus the per-product helpers below. Situations whose bank record is not certain under the brief
are not generated. Test tooling only: nothing here is used by the builder. Nothing here calls Freya or the bank.
"""
import argparse, collections, copy, datetime as dt, json, pathlib, random, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from edge_cases import (BASE, CALL_DAYS, HOLIDAYS_A, VERIFY, WD, P, money, _attempt1, _bday, _d, _deadline,  # noqa: E402
                        _next_bday, _promise_day, _weekday_next_week)

# ---------------------------------------------------------------- calendar
# The organisers' call days (CALL_DAYS) plus the practice day and more days next to weekends and bank holidays.
DAYS = sorted(set(CALL_DAYS) | {"2026-10-14", "2026-10-22", "2026-10-27", "2026-11-25", "2026-12-31"})
TIMES = ["09:30", "11:00", "14:15", "16:45"]                  # time of day of the call; matters only for callbacks
CB_TIMES = ["09:30", "11:00", "13:15", "16:45", "18:30"]      # third-party calls: the callback window depends on it
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
A_BASES = [f"PUB-{i:02d}" for i in range(1, 11)]
B_BASES = ["PUB-11", "PUB-12", "PUB-13"]
FAILS_FIRST = {b for b, c in BASE.items() if any(f.get("tool") == "create_promise" for f in c.get("faults", []))}

def iso(d): return d.isoformat()
def long_day(d): return f"{WD[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
def floor_of(p): return float(p.get("minimum_payment", p.get("instalment_amount")))
def is_late(p): return p["days_late"] >= 8
def deadline(t, p): return _deadline(t, p["days_late"])
def attempt1(t): return _attempt1(t)
def weekday_this_week(t, wd): return t - dt.timedelta(days=t.weekday()) + dt.timedelta(days=wd)   # rule 22
def callback_ok(t, now_hm, d, hm):
    """Rule 6: today or the next business day, 10:00 to 19:00 both included, later than now."""
    if not ("10:00" <= hm <= "19:00"): return False
    if d == t: return hm > now_hm
    return d == _next_bday(t + dt.timedelta(days=1))

def self_check():
    """Pin the calculator to dates the brief and tools/edge_cases.py state."""
    t = _d("2026-10-14")
    assert iso(_deadline(t, 6)) == "2026-11-02" and iso(_deadline(t, 8)) == "2026-10-23"
    assert iso(_attempt1(t)) == "2026-10-16" and iso(_attempt1(_d("2026-10-16"))) == "2026-10-23"
    assert iso(_weekday_next_week(_d("2026-10-16"), 4)) == "2026-10-23" and iso(_promise_day(t, _d("2026-10-26"), 6)) == "2026-10-27"
    assert iso(_promise_day(t, _d("2026-11-20"), 6)) == "2026-11-02" and iso(_promise_day(t, _d("2026-10-17"), 6)) == "2026-10-19"
    assert iso(weekday_this_week(t, 4)) == "2026-10-16"
    assert callback_ok(_d("2026-10-23"), "12:00", _d("2026-10-27"), "10:00") and not callback_ok(_d("2026-10-23"), "12:00", _d("2026-10-26"), "10:00")
    assert not callback_ok(t, "18:30", t, "18:00") and callback_ok(t, "18:30", t, "19:00") and not callback_ok(t, "11:00", t, "19:30")
    for day in DAYS: assert _bday(_d(day)), day

# ---------------------------------------------------------------- words
ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORD = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
       11: "eleventh", 12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth", 16: "sixteenth", 17: "seventeenth",
       18: "eighteenth", 19: "nineteenth", 20: "twentieth", 30: "thirtieth"}

def words(n):
    n = int(n)
    if n < 20: return ONES[n]
    if n < 100: return TENS[n // 10] + ("" if n % 10 == 0 else "-" + ONES[n % 10])
    if n < 1000: return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " and " + words(n % 100))
    return words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else (" and " if n % 1000 < 100 else " ") + words(n % 1000))

def ordinal_words(n):
    if n in ORD: return ORD[n]
    return TENS[n // 10] + "-" + ORD[n % 10]

def ordinal(n): return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"

def date_phrases(t, d):
    """Ways a customer names day d on call day t: (phrase, kind). Only phrases with one reading under rule 22."""
    out = [(f"on {long_day(d)}", "digits"), (f"on the {ordinal_words(d.day)} of {MONTHS[d.month - 1]}", "words")]
    if d == t + dt.timedelta(days=1): out.append(("tomorrow", "relative"))
    if t.weekday() < d.weekday() and weekday_this_week(t, d.weekday()) == d: out.append((f"this {WD[d.weekday()]}", "relative"))
    if _weekday_next_week(t, d.weekday()) == d: out.append((f"next {WD[d.weekday()]}", "relative"))
    return out

def amount_phrases(x, tail=""):
    """Ways a customer says amount x followed by tail: (text, kind). Words and pieces only for whole amounts."""
    out = [(money(x) + tail, "digits")]
    if x == int(x):
        out.append((words(x) + tail, "words"))
        if x >= 100 and x % 100 != 0:
            hundreds = int(x) // 100 * 100
            out.append((f"'{words(hundreds)}...' and stops; when the agent asks, adds 'and {words(int(x) - hundreds)}{tail}'", "pieces"))
    return out

def dob_words(y, m, d): return f"{d} {MONTHS[m - 1]} {y}"
def yy_words(y): return ("oh " + ONES[y % 100]) if y % 100 < 10 else words(y % 100)
def num_words(n): return ("oh " + ONES[n]) if n < 10 else words(n)

# ---------------------------------------------------------------- people
def pron(c):
    m = c["variables"]["title"] == "Mr"
    return {"he": "he" if m else "she", "He": "He" if m else "She", "his": "his" if m else "her", "him": "him" if m else "her",
            "hers": "his" if m else "hers", "first": c["variables"]["first_name"], "full": c["variables"]["first_name"] + " " + c["variables"]["last_name"]}

PNAME = {"credit_card": "the credit card", "personal_loan": "the personal loan", "car_loan": "the car loan"}
Y, Y1 = "Confirms at the read-back.", "Confirms the read-back once."
NO3P = ["nothing about the account is said", "the agent does not mention an account, a debt, a product or a payment"]
ACCEPT_OTHER = "If the agent offers another day instead, accepts it."

def E(outcome, reason=None, promises=(), notes=(), callbacks=(), transfers=()):
    return {"outcome": outcome, "reason": reason, "promises": list(promises), "notes": list(notes), "callbacks": list(callbacks), "transfers": list(transfers)}
def N(pid, reason): return {"product_id": pid, "reason": reason}
def TR(q): return [{"queue": q}]

def partial_amounts(p):
    """Amounts from the floor up to the full amount the customer may propose (rule 27): (amount, note)."""
    full, fl = p["past_due"], floor_of(p)
    out = [(fl, "exactly the floor")]
    mid = float(round((fl + full) / 2))
    if fl < mid < full: out.append((mid, "between the floor and the full amount"))
    r = float(int(fl / 50 + 1) * 50)       # a round number just above the floor
    if fl < r < full and r != mid: out.append((r, "between the floor and the full amount"))
    return out

def below_amounts(p):
    fl = floor_of(p)
    out = []
    for x in (float(round(fl * 0.6)), float(int(fl / 2 / 10) * 10)):
        if 0 < x < fl and x not in out: out.append(x)
    return out

def date_buckets(t, p):
    """Days the customer can name for product p on call day t, by what the agent must do with them."""
    dl = deadline(t, p)
    ok, moved, late = [], [], []
    d = t + dt.timedelta(days=1)
    while d <= dl + dt.timedelta(days=12):
        if d > dl: late.append(d)
        elif _bday(d): ok.append(d)
        else: moved.append(d)
        d += dt.timedelta(days=1)
    return ok, moved, late

# ---------------------------------------------------------------- Brief A templates
def a_templates(b, t):
    """(family, key, script, expected, checks, style_ok) for one-product customer b called on day t."""
    c = BASE[b]; p = c["products"][0]; x = pron(c)
    full, fl, late = p["past_due"], floor_of(p), is_late(p)
    dl, a1, T = deadline(t, p), attempt1(t), iso(t)
    ok, moved, too_late = date_buckets(t, p)
    band = "late band: at most two persuasion attempts" if late else "early band: one persuasion attempt"
    R = []
    def add(fam, key, script, exp, checks, style_ok=True): R.append((fam, key, script.format(**x), exp, checks, style_ok))
    prom = lambda amt, d: E("promise", None, [P("p1", float(amt), iso(d))])

    # --- pays the full amount
    add("Pay full", "today", f"{VERIFY} Can pay the full amount today. {Y}", prom(full, t), ["promise for today", "text-message sentence said after the promise"])
    add("Pay full", "today-branch", f"{VERIFY} Says {{he}} will pay the full amount in cash at a branch today. {Y}", prom(full, t), ["promise for today"])
    add("Pay full", "today-done", f"{VERIFY} Pays the full amount today and confirms. When asked whether there is anything else, says no.", prom(full, t),
        ["the agent says goodbye and ends the call"])
    for ph, kind in amount_phrases(full, " today")[:2]:
        add("Pay full", f"today-says-{kind}", f"{VERIFY} Says {{he}} can pay {ph}, the whole amount. {Y}", prom(full, t), [f"{money(full)} today is accepted"])

    # --- dates
    for d in ok[:6] + ok[-2:]:
        for ph, kind in date_phrases(t, d):
            add("Dates", f"ok-{kind}-{iso(d)}", f"{VERIFY} Cannot pay today. Can pay the full amount {ph}. {ACCEPT_OTHER} {Y}", prom(full, d),
                [f"the promise is for {long_day(d)}"])
    for d in moved[:4]:
        for ph, kind in date_phrases(t, d)[:2]:
            nd = _promise_day(t, d, p["days_late"])
            add("Dates", f"moved-{kind}-{iso(d)}", f"{VERIFY} Cannot pay today. Wants to pay the full amount {ph}. Accepts the day the agent offers instead. {Y}", prom(full, nd),
                [f"{long_day(d)} is not a business day and is not accepted", f"{long_day(nd)}, the next business day, is offered"])
    for d in too_late[2:5:2] + too_late[-1:]:
        ph, kind = date_phrases(t, d)[len(R) % 2]
        add("Dates", f"late-{kind}-{iso(d)}", f"{VERIFY} Cannot pay today. Says {{he}} can pay {ph}. Accepts the latest date the agent can offer. {Y}", prom(full, dl),
            [f"the agent says the latest date it can record is {long_day(dl)} and offers it"])
    add("Dates", "five-weeks", f"{VERIFY} Cannot pay today. Says {{he}} can pay in about five weeks. Accepts the latest date the agent can offer. {Y}", prom(full, dl),
        [f"the agent offers its latest date, {long_day(dl)}"])
    for ph, kind in date_phrases(t, dl)[:2]:
        add("Dates", f"deadline-{kind}", f"{VERIFY} Cannot pay today. Can pay the full amount {ph}. {Y}", prom(full, dl), [f"{long_day(dl)}, the deadline itself, is accepted"])
    add("Dates", "salary-unknown", f"{VERIFY} Cannot pay today. Says 'when my salary comes'. When asked for an exact day, says {{he}} does not know. Accepts the date the agent then proposes. {Y}",
        prom(full, dl), ["the agent asks once for the exact day", f"the agent proposes the deadline, {long_day(dl)}"])
    for wd in (0, 2, 3):
        d = _weekday_next_week(t, wd); nd = _promise_day(t, d, p["days_late"])
        add("Dates", f"next-week-{WD[wd]}", f"{VERIFY} Cannot pay today. Says 'sometime next week'. When asked for the exact day, says {WD[wd]}. {ACCEPT_OTHER} {Y}", prom(full, nd),
            ["the agent asks once for the exact day", f"the promise is for {long_day(nd)}"])

    # --- partial amounts (rule 27)
    for amt, why in partial_amounts(p):
        for ph, kind in amount_phrases(amt, " today"):
            add("Partial", f"today-{money(amt)}-{kind}", f"{VERIFY} Says the full amount is not possible but {{he}} can pay {ph}. {Y}", prom(amt, t),
                [f"{money(amt)}, {why}, is accepted without argument"])
        for d in ok[1:4]:
            ph, _ = date_phrases(t, d)[0]
            add("Partial", f"date-{money(amt)}-{iso(d)}", f"{VERIFY} Says the full amount is not possible but {{he}} can pay {money(amt)} {ph}. {Y}", prom(amt, d),
                [f"{money(amt)} on {long_day(d)} is accepted"])

    # --- below the floor, or the full amount is too much (rules 26, 27)
    for amt in below_amounts(p):
        for ph, kind in amount_phrases(amt, " today")[:2]:
            add("Below floor", f"below-today-{money(amt)}-{kind}", f"{VERIFY} Says {{he}} can only pay {ph}. Accepts the amount the agent offers instead, today. {Y}", prom(fl, t),
                [f"{money(amt)} is not accepted", f"the agent offers {money(fl)}"])
        for d in ok[1:3]:
            ph, _ = date_phrases(t, d)[0]
            add("Below floor", f"below-date-{money(amt)}-{iso(d)}", f"{VERIFY} Says {{he}} can only pay {money(amt)}, {ph}. Accepts the amount the agent offers instead, on that day. {Y}",
                prom(fl, d), [f"{money(amt)} is not accepted", f"the agent offers the floor, {money(fl)}"])
    add("Below floor", "too-much-today", f"{VERIFY} Says the full amount is too much. If the agent offers a smaller amount for today, accepts it. {Y}", prom(fl, t),
        [f"the agent offers {money(fl)} and says the rest stays overdue"])
    for d in ok[1:4]:
        ph, _ = date_phrases(t, d)[-1]
        add("Below floor", f"too-much-date-{iso(d)}", f"{VERIFY} Says the full amount is too much. Accepts the smaller amount the agent offers, but only {ph}. {Y}", prom(fl, d),
            [f"the agent offers {money(fl)} and says the rest stays overdue", f"the promise is for {long_day(d)}"])

    # --- refusals and persuasion (rules 30, 31)
    add("Refusal", "flat", f"{VERIFY} Flatly refuses to pay, today or any other day, whatever is offered. Gives no date and no amount.", E("no_promise", "refused"),
        [band, f"attempt 1: the full amount on {long_day(a1)}", "no offer is repeated"])
    add("Refusal", "too-much-then-no", f"{VERIFY} Says the full amount is too much. Refuses the smaller amount too, and every offer after that. Gives no date and no amount.",
        E("no_promise", "refused"), [f"the floor {money(fl)} is offered and the agent says the rest stays overdue", "no offer is repeated"])
    add("Refusal", "hmm-then-no", f"{VERIFY} When asked whether {{he}} can pay today, says only 'hmm, I don't know'. Then flatly refuses every offer, without giving a date or amount.",
        E("no_promise", "refused"), [band, "no offer is repeated after the last attempt"])
    add("Refusal", "not-interested", f"{VERIFY} Says {{he}} is not going to pay this, and says no to every offer. Gives no date and no amount.", E("no_promise", "refused"), [band, "no offer is repeated"])
    if a1 <= dl:
        add("Persuasion", "first-offer", f"{VERIFY} Says no to paying today. Accepts the agent's first offer. {Y}", prom(full, a1),
            [f"the agent offers {money(full)} on {long_day(a1)}"])
        if late:
            add("Persuasion", "second-offer", f"{VERIFY} Says no to paying today. Says no to the first offer. Accepts the second offer. {Y}", prom(fl, dl),
                [f"attempt 1: {money(full)} on {long_day(a1)}", f"attempt 2: {money(fl)} on {long_day(dl)}"])
            add("Persuasion", "hmm-second-offer", f"{VERIFY} When asked whether {{he}} can pay today, says 'I really can't'. Says no to the first offer too. Accepts the second offer. {Y}",
                prom(fl, dl), [f"attempt 1: {money(full)} on {long_day(a1)}", f"attempt 2: {money(fl)} on {long_day(dl)}"])

    # --- other things customers say (rules 39-45)
    add("Already paid", "yesterday", f"{VERIFY} Says {{he}} already paid this amount yesterday in the app.", E("paid"), ["payments can take one business day to appear", "no promise taken"], False)
    add("Already paid", "this-morning", f"{VERIFY} Says {{he}} paid the overdue amount this morning at a branch.", E("paid"), ["payments can take one business day to appear", "no promise taken"], False)
    add("Dispute", "not-mine", f"{VERIFY} Says this debt is not {{hers}} and {{he}} never had this product.", E("no_promise", "dispute"), ["the agent does not argue", "no promise taken"], False)
    add("Dispute", "closed", f"{VERIFY} Says {{he}} closed this product years ago and paid it off then.", E("no_promise", "dispute"), ["treated as a dispute", "no promise taken"], False)
    add("Dispute", "fraud", f"{VERIFY} After hearing the debt, says someone else opened this in {{his}} name and these are not {{his}} charges.", E("no_promise", "dispute"),
        ["the agent does not argue", "no promise taken"], False)
    for k, why in (("job", "{he} lost {his} job last month"), ("hospital", "{he} has been in hospital and is ill"), ("death", "a close family member died last week")):
        add("Hardship", f"no-{k}", f"{VERIFY} Says {why}, and {{he}} cannot pay anything. Declines the offer of help from the support team.", E("no_promise", "hardship"),
            ["one sentence of sympathy", "no persuasion attempt"], False)
        add("Hardship", f"yes-{k}", f"{VERIFY} Says {why}. Accepts help from the support team.", E("transferred", "support", transfers=TR("support")),
            ["one sentence of sympathy", "no persuasion attempt", "transfer to support"], False)
    for amt, _ in partial_amounts(p)[:2]:
        d = ok[min(2, len(ok) - 1)]
        add("Hardship", f"own-offer-{money(amt)}", f"{VERIFY} Says {{he}} lost {{his}} job. Declines the offer of help from the support team. Then says {{he}} can still pay {money(amt)} {date_phrases(t, d)[0][0]}. {Y}",
            prom(amt, d), ["one sentence of sympathy", "no persuasion attempt", "the customer's own offer is recorded"], False)
    add("Transfer", "restructure", f"{VERIFY} Asks for a new payment plan with lower instalments. Agrees to be transferred.", E("transferred", "restructuring", transfers=TR("restructuring")),
        ["no plan is described", "transfer to restructuring"], False)
    add("Transfer", "human", f"{VERIFY} After hearing the debt, wants a real person. When the agent asks whether it can help first, insists.", E("transferred", "human", transfers=TR("general")),
        ["the agent asks once whether it can help first", "transfer to a person"], False)
    add("Transfer", "human-before-id", "Confirms {his} name. Before any security question, says {he} wants to talk to a real person. When the agent asks whether it can help first, insists on a person.",
        E("transferred", "human", transfers=TR("general")), ["the agent asks once whether it can help first", "nothing about the account is said"], False)
    add("No talk", "verified", f"{VERIFY} After hearing the debt, says {{he}} does not want to talk about it. When asked again, says again {{he}} does not want to talk.",
        E("no_promise", "refused"), ["the call ends after the second refusal to talk", "after the debt is stated the agent never mentions security questions"], False)
    add("No talk", "before-id", "Confirms {his} name. When asked the first security question, says {he} does not want to talk. After the agent explains, says again {he} does not want to talk. Never gives any security answer.",
        E("not_verified", "declined"), ["the agent explains once that the questions protect the account", "the debt is not mentioned"], False)

    # --- mid-call changes (rules 32, 35, 36, 41, 42)
    for amt, _ in partial_amounts(p)[:2]:
        add("Mid-call change", f"readback-amount-{money(amt)}", f"{VERIFY} Agrees to pay the full amount today. At the read-back says 'actually, make it {money(amt)}'. Confirms the new read-back.",
            prom(amt, t), [f"the agent reads back again with {money(amt)}", "a single promise is recorded"])
    for d in ok[1:3]:
        ph, _ = date_phrases(t, d)[-1]
        add("Mid-call change", f"readback-date-{iso(d)}", f"{VERIFY} Agrees to pay the full amount today. At the read-back says 'actually, make it {ph}'. Confirms the new read-back.",
            prom(full, d), [f"the agent reads back again with {long_day(d)}", "a single promise is recorded"])
    for d in moved[:1]:
        nd = _promise_day(t, d, p["days_late"])
        add("Mid-call change", f"readback-weekend-{iso(d)}", f"{VERIFY} Agrees to pay the full amount today. At the read-back says 'make it {long_day(d)} instead'. Accepts the day the agent then offers. Confirms the new read-back.",
            prom(full, nd), [f"{long_day(d)} is not accepted, {long_day(nd)} is offered", "a single promise is recorded"])
    for k, ask in (("move", "asks to move it to next week instead"), ("cancel", "asks to cancel it"), ("amount", "asks to make it smaller instead")):
        add("Mid-call change", f"recorded-{k}", f"{VERIFY} Pays the full amount today and confirms. After the agent says it is recorded, {ask}.", prom(full, t),
            ["the agent says a recorded promise can be changed in the mobile app", "after the customer asks for the change, no new or changed promise is recorded"])
    add("Mid-call change", "agrees-then-job", f"{VERIFY} Agrees to pay the full amount today. Before confirming the read-back, says {{he}} has just lost {{his}} job and cannot pay after all. Declines the offer of help from the support team.",
        E("no_promise", "hardship"), ["one sentence of sympathy", "no persuasion attempt after the job loss", "no promise recorded"], False)
    add("Mid-call change", "no-then-ill", f"{VERIFY} Says no to paying today. Then says {{he}} is seriously ill and in hospital. Accepts help from the support team.",
        E("transferred", "support", transfers=TR("support")), ["no persuasion attempt after the illness", "transfer to support"], False)
    add("Mid-call change", "no-then-plan", f"{VERIFY} Says no to paying today and no to the first offer. Then asks for a new payment plan with lower instalments. Agrees to be transferred.",
        E("transferred", "restructuring", transfers=TR("restructuring")), ["no plan is described", "transfer to restructuring"], False)
    add("Mid-call change", "recorded-then-human", f"{VERIFY} Pays the full amount today and confirms. After the agent says it is recorded, asks to speak to a real person. When the agent asks whether it can help first, insists.",
        E("transferred", "human", [P("p1", full, T)], transfers=TR("general")), ["the recorded promise stays", "closed as transferred / human"], False)

    # --- the customer asks something back (rules 33, 43, 44, 4)
    QS = [("interest", "asks what interest rate is charged on the overdue amount", ["the agent says it does not have that information", "no figure is invented"]),
          ("discount", "asks whether {he} gets a discount for paying today", ["no discount or waiver is offered"]),
          ("waiver", "asks whether the bank can waive the late fees", ["no discount, waiver or instalment plan is offered"]),
          ("credit-score", "asks whether this will affect {his} credit score", ["the agent says it does not have that information", "no consequence is invented"]),
          ("scam", "asks whether this call is a scam", ["the agent says the call can be verified by phoning Freya Bank's customer service number"]),
          ("robot", "asks whether {he} is talking to a robot", ["the agent says it is Freya Bank's digital assistant"])]
    for k, q, ck in QS:
        add("Asks back", f"{k}-today", f"{VERIFY} After hearing the debt, {q}. After the answer, pays the full amount today. {Y}", prom(full, t), ck + ["the call continues normally"])
        d = ok[min(1, len(ok) - 1)]; ph, _ = date_phrases(t, d)[-1]
        add("Asks back", f"{k}-date", f"{VERIFY} After hearing the debt, {q}. After the answer, says {{he}} can pay the full amount {ph}. {Y}", prom(full, d), ck + [f"the promise is for {long_day(d)}"])
    return R

# ---------------------------------------------------------------- identity (A and B)
def wrong_dobs(dob, n):
    y, m, d = (int(v) for v in dob.split("-"))
    cands = [(y + 1, m, d), (y, m % 12 + 1, min(d, 28)), (y - 2, m, min(d + 1, 28)), (y + 3, (m + 5) % 12 + 1, 10), (y - 1, m, d)]
    out = []
    for c in cands:
        if c != (y, m, d) and c != (y, d, m) and (c[0], c[2], c[1]) != (y, m, d) and c not in out: out.append(c)
    return [dob_words(*c) for c in out[:n]]

def wrong_phones(l4, n):
    cands = [l4[1:] + l4[0], l4[::-1], "".join(str((int(ch) + 1) % 10) for ch in l4), "1234"]
    out = [c for i, c in enumerate(cands) if c != l4 and c not in cands[:i]]
    return out[:n]

def id_templates(b, t, pay_script, pay_exp, pay_ck):
    """Identity answers in awkward forms, then the payment part. pay_* is what happens after verification."""
    c = BASE[b]; x = pron(c); dob, l4 = c["identity"]["date_of_birth"], c["identity"]["phone_last4"]
    y, m, d = (int(v) for v in dob.split("-"))
    R = []
    def add(key, script, exp, checks): R.append(("Identity", key, script.format(**x), exp, checks, False))
    nv = lambda r: E("not_verified", r)
    add("dob-numbers", f"Confirms {{his}} name. Gives {{his}} date of birth only as numbers: '{words(d)}, {num_words(m)}, {yy_words(y)}'. Last four digits {l4}. {pay_script}",
        pay_exp, [f"the date of birth is sent as {dob} (day, then month, then year)"] + pay_ck)
    if d > 12:
        add("dob-month-first", f"Confirms {{his}} name. Gives {{his}} date of birth month first, as numbers: '{num_words(m)}, {words(d)}, {yy_words(y)}'. When asked to say the month as a word, says 'the {ordinal_words(d)} of {MONTHS[m - 1]} {y}'. Last four digits {l4}. {pay_script}",
            pay_exp, ["the swapped answer is not counted as a wrong answer"] + pay_ck)
    add("dob-no-year", f"Confirms {{his}} name. For the date of birth says only 'the {ordinal(d)} of {MONTHS[m - 1]}'. When asked for the year, says {y}. Last four digits {l4}. {pay_script}",
        pay_exp, ["the agent asks only for the missing year", "the half answer is not counted as a wrong answer"] + pay_ck)
    add("phone-pieces", f"Confirms {{his}} name. Date of birth {dob_words(y, m, d)}. For the phone digits says '{' '.join(words(int(ch)) for ch in l4[:2])}...' and stops. When asked for the rest, says '{' '.join(words(int(ch)) for ch in l4[2:])}'. {pay_script}",
        pay_exp, [f"the two pieces are joined into {l4}", "the half answer is not counted as a wrong answer"] + pay_ck)
    w = wrong_dobs(dob, 3); wp = wrong_phones(l4, 2)
    add("wrong-once", f"Confirms {{his}} name. Gives {{his}} date of birth as {w[0]}, then the right one, {dob_words(y, m, d)}. Last four digits {l4}. {pay_script}",
        pay_exp, ["one wrong answer does not end the check", "the agent never says the right answer"] + pay_ck)
    add("wrong-twice", f"Confirms {{his}} name. Gives {{his}} date of birth as {w[0]}, then the right one, {dob_words(y, m, d)}. For the phone digits says {wp[0]}, then the right ones, {l4}. {pay_script}",
        pay_exp, ["two wrong answers do not end the check", "the agent never says the right answer"] + pay_ck)
    add("asks-why", f"Confirms {{his}} name. When asked for {{his}} date of birth, asks why the bank needs it. After the explanation, answers: {dob_words(y, m, d)}, last four digits {l4}. {pay_script}",
        pay_exp, ["the agent explains once that the questions protect the account"] + pay_ck)
    add("who-is-this", f"Answers the first question with 'Who is this?'. After the agent answers, says 'Speaking.' {VERIFY} {pay_script}", pay_exp,
        ["'who is this' is not taken as a confirmation"] + pay_ck)
    add("hello", f"Answers the first question with 'Hello?'. When asked again, says 'Yes, that's me.' {VERIFY} {pay_script}", pay_exp, ["'hello' is not taken as a confirmation"] + pay_ck)
    add("robot-opening", f"Confirms {{his}} name. Asks 'Am I talking to a robot?'. After the answer, cooperates: {VERIFY} {pay_script}", pay_exp,
        ["the agent says it is Freya Bank's digital assistant", "the call continues normally"] + pay_ck)
    add("fail-three-dob", f"Confirms {{his}} name. Gives wrong dates of birth: {w[0]}, then {w[1]}, then {w[2]}. Does not know the right one.", nv("failed"),
        ["the check ends after three wrong answers", "the agent mentions the mobile app or a branch", "the debt is not stated"])
    add("fail-mixed", f"Confirms {{his}} name. Gives {{his}} date of birth as {w[0]}, then the right one, {dob_words(y, m, d)}. For the phone digits says {wp[0]}, then {wp[1]}. Does not know the right digits.",
        nv("failed"), ["three wrong answers across both questions end the check", "the debt is not stated", "the agent mentions the mobile app or a branch"])
    add("declines", "Confirms {his} name. Will not give {his} date of birth. After the agent explains why, still refuses.", nv("declined"),
        ["the agent explains once that the questions protect the account", "the debt is not mentioned"])
    add("what-about", "Confirms {his} name. When asked for {his} date of birth, asks 'What is this about?' instead. After the agent's explanation, again asks what the call is about instead of answering.",
        nv("declined"), ["the agent explains once that the questions protect the account", "the reason for the call is not given"])
    add("declines-phone", f"Confirms {{his}} name. Gives {{his}} date of birth correctly, {dob_words(y, m, d)}. Refuses to give the phone digits. After the agent explains why, still refuses.",
        nv("declined"), ["the debt is not mentioned"])
    add("unclear", "Answers 'Hello?' to the first question. To the second ask, says 'Who is calling?'. After the answer, only says 'Go ahead'. Never confirms the name.",
        E("not_reached", "unclear"), ["the agent does not treat 'hello' or 'go ahead' as a confirmation", "nothing about the account is said"])
    return R

# ---------------------------------------------------------------- someone else answers (A and B)
REL_M = ["wife", "son", "daughter", "flatmate", "partner", "brother"]
REL_F = ["husband", "son", "daughter", "flatmate", "partner", "sister"]
HOURS = [("nine in the morning", "09:00"), ("ten in the morning", "10:00"), ("eleven in the morning", "11:00"), ("noon", "12:00"),
         ("three in the afternoon", "15:00"), ("half past four in the afternoon", "16:30"), ("six in the evening", "18:00"),
         ("seven in the evening", "19:00"), ("half past seven in the evening", "19:30"), ("nine in the evening", "21:00")]

def cb_days(t):
    """(phrase, date) a relative may name: today, tomorrow and the coming weekdays."""
    out = [("today", t), ("tomorrow", t + dt.timedelta(days=1))]
    for k in range(2, 6):
        d = t + dt.timedelta(days=k)
        if d.weekday() < 5: out.append((f"on {WD[d.weekday()]}", d))
    return out

def third_templates(b, t, now_hm, rng):
    c = BASE[b]; x = pron(c); first = x["first"]
    rels = REL_M if c["variables"]["title"] == "Mr" else REL_F
    R = []
    def add(fam, key, script, exp, checks): R.append((fam, key, script, exp, checks, False))
    slots = [(dp, d, hp, hm) for dp, d in cb_days(t) for hp, hm in HOURS]
    good = [s for s in slots if callback_ok(t, now_hm, s[1], s[3])]
    bad = [s for s in slots if not callback_ok(t, now_hm, s[1], s[3])]
    rng.shuffle(good); rng.shuffle(bad)
    why = lambda s: ("is not today or the next business day" if s[1] != t and s[1] != _next_bday(t + dt.timedelta(days=1))
                     else "is outside 10:00 to 19:00" if not ("10:00" <= s[3] <= "19:00") else "has already passed")
    for i, s in enumerate(good[:6]):
        rel = rels[i % len(rels)]
        intro = ["", f" Asks what it is about.", " Offers to pay whatever is owed, right now, by card.",
                 f" Offers to answer the security questions for {first}.", " Asks twice what the call is about.", ""][i]
        add("Callback", f"ok-{iso(s[1])}-{s[3]}", f"Is {first}'s {rel}.{intro} Says {first} can be reached {s[0]} at {s[2]}.",
            E("not_reached", "callback", callbacks=[{"date": iso(s[1]), "time": s[3]}]), NO3P + [f"callback booked for {iso(s[1])} {s[3]}"])
    for i, (s1, s2) in enumerate(zip(bad[:4], good[6:10])):
        add("Callback", f"second-{iso(s1[1])}-{s1[3]}-{iso(s2[1])}-{s2[3]}", f"Is {first}'s {rels[(i + 2) % len(rels)]}. First says {first} can be reached {s1[0]} at {s1[2]}. When asked for another time, says {s2[0]} at {s2[2]}.",
            E("not_reached", "callback", callbacks=[{"date": iso(s2[1]), "time": s2[3]}]), NO3P + [f"{s1[0]} at {s1[3]} {why(s1)} and is not booked", "the agent asks once for another time"])
    for i, (s1, s2) in enumerate(zip(bad[4:7], bad[7:10])):
        add("Third party", f"no-cb-{iso(s1[1])}-{s1[3]}-{iso(s2[1])}-{s2[3]}", f"Is {first}'s {rels[(i + 4) % len(rels)]}. First says {first} can be reached {s1[0]} at {s1[2]}. When asked for another time, says {s2[0]} at {s2[2]}. Has no other time.",
            E("not_reached", "no_callback"), NO3P + ["the agent asks once for another time", "no callback is booked"])
    add("Third party", "abroad", f"Is {first}'s {rels[3]}. Says {first} is abroad for a month. When asked for another time, says not before next month.",
        E("not_reached", "no_callback"), NO3P + ["no callback is booked"])
    add("Third party", "wrong-number", f"Says nobody called {x['full']} lives here: wrong number.", E("not_reached", "wrong_number"), NO3P)
    add("Third party", "wrong-number-work", f"Says this is a company phone and there is no {x['full']} here. Has never heard of {x['him']}.", E("not_reached", "wrong_number"), NO3P)
    add("Third party", "deceased", f"Is {first}'s {rels[1]}. Says, upset, that {first} passed away last month.", E("not_reached", "deceased"),
        ["the agent says 'I am sorry for your loss.'", "the agent asks nothing else"])
    return R

# ---------------------------------------------------------------- Brief B
def b_whole(b, t):
    """Situations that treat every product the same way."""
    c = BASE[b]; ps = c["products"]; x = pron(c); ids = [f"p{i + 1}" for i in range(len(ps))]; T = iso(t)
    them = "both" if len(ps) == 2 else "all three"
    a1 = attempt1(t)
    R = []
    def add(fam, key, script, exp, checks, style_ok=True): R.append((fam, key, script.format(**x), exp, checks, style_ok))
    allp = lambda f: [P(i, float(f(p)[0]), iso(f(p)[1])) for i, p in zip(ids, ps)]
    add("Pay full", "all-today", f"{VERIFY} Can pay {them} in full today. {Y1}", E("promise", None, allp(lambda p: (p["past_due"], t))),
        ["all products stated before asking", "one read-back for all promises"])
    # one date for everything (rule B6): each product on that day, its next business day, or its own deadline
    days = sorted({d for p in ps for d in sum(date_buckets(t, p), [])})
    picks = [d for d in days if d <= max(deadline(t, p) for p in ps) + dt.timedelta(days=6)]
    for d in picks[::2]:
        ph, kind = date_phrases(t, d)[(d.day + len(ps)) % len(date_phrases(t, d))]
        exp = E("promise", None, [P(i, p["past_due"], iso(_promise_day(t, d, p["days_late"]))) for i, p in zip(ids, ps)])
        cut = [PNAME[p["type"]] for p in ps if _promise_day(t, d, p["days_late"]) != d]
        add("Dates", f"one-date-{kind}-{iso(d)}", f"{VERIFY} Cannot pay today. Wants to pay {them} in full {ph}. Accepts any day the agent says it must use instead for a product. {Y1}", exp,
            [", ".join(f"{PNAME[p['type']]} on {long_day(_promise_day(t, d, p['days_late']))}" for p in ps)] + ([f"{' and '.join(cut)}: the agent says why the day differs from {long_day(d)}, in the read-back"] if cut else []))
    add("Refusal", "refuses-all", f"{VERIFY} Flatly refuses to pay any of them, whatever is offered.", E("no_promise", "refused", notes=[N(i, "refused") for i in ids]),
        ["a refused note for each product", "persuasion attempts are counted per product", "no offer is repeated"])
    if all(a1 <= deadline(t, p) for p in ps):
        add("Persuasion", "first-offer-all", f"{VERIFY} Says no to paying today. For each product, accepts the agent's first offer. {Y1}",
            E("promise", None, [P(i, p["past_due"], iso(a1)) for i, p in zip(ids, ps)]), [f"each product is offered in full on {long_day(a1)}", "today is not asked again for each product"])
    add("Already paid", "all-paid", f"{VERIFY} Says {{he}} already paid {them} yesterday.", E("paid", None, notes=[N(i, "paid") for i in ids]), ["a paid note for each product", "closed as paid"], False)
    add("Dispute", "all-dispute", f"{VERIFY} Says none of these debts are {{hers}}; {{he}} never had any of these products.", E("no_promise", "dispute", notes=[N(i, "dispute") for i in ids]),
        ["the agent does not argue", "a dispute note for each product"], False)
    add("Hardship", "no", f"{VERIFY} Says {{he}} lost {{his}} job and cannot pay anything. Declines the offer of help from the support team.",
        E("no_promise", "hardship", notes=[N(i, "hardship") for i in ids]), ["one sentence of sympathy", "no persuasion attempt", "a hardship note for each product"], False)
    add("Hardship", "yes", f"{VERIFY} Says {{he}} has cancer treatment and cannot think about money. Accepts help from the support team.",
        E("transferred", "support", transfers=TR("support")), ["no persuasion attempt", "transfer to support", "no notes at all"], False)
    add("Transfer", "restructure", f"{VERIFY} As soon as the debts are stated, asks for lower instalments on all of them. Agrees to be transferred.",
        E("transferred", "restructuring", transfers=TR("restructuring")), ["no plan is described", "no notes"], False)
    add("Transfer", "human", f"{VERIFY} After hearing the debts, wants a real person. When the agent asks whether it can help first, insists.",
        E("transferred", "human", transfers=TR("general")), ["the agent asks once whether it can help first", "no notes at all"], False)
    add("Transfer", "human-before-id", "Confirms {his} name. Before any security question, says {he} wants to talk to a real person. When the agent asks whether it can help first, insists on a person.",
        E("transferred", "human", transfers=TR("general")), ["the agent asks once whether it can help first", "nothing about the account is said"], False)
    add("No talk", "verified", f"{VERIFY} After hearing the debts, says {{he}} does not want to talk about it. When asked again, says again {{he}} does not want to talk.",
        E("no_promise", "refused", notes=[N(i, "refused") for i in ids]), ["the call ends after the second refusal to talk", "a refused note for each product"], False)
    add("No talk", "before-id", "Confirms {his} name. When asked the first security question, says {he} does not want to talk. After the agent explains, says again {he} does not want to talk. Never gives any security answer.",
        E("not_verified", "declined"), ["the agent explains once that the questions protect the account", "the debts are not mentioned"], False)
    full_all = E("promise", None, allp(lambda p: (p["past_due"], t)))
    add("Mid-call change", "recorded-move", f"{VERIFY} Can pay {them} in full today. {Y1} After the agent says they are recorded, asks to move one of them to next week.", full_all,
        ["the agent says a recorded promise can be changed in the mobile app", "no new or changed promise is recorded"])
    add("Mid-call change", "plan-after-first", f"{VERIFY} Says {{he}} can pay {PNAME[ps[0]['type']]} in full today. Then, before any read-back, asks for lower instalments on the others. Agrees to be transferred.",
        E("transferred", "restructuring", transfers=TR("restructuring")), ["no plan is described", "the agreed but unrecorded promise is not recorded", "no notes"], False)
    for k, q, ck in (("interest", "asks what interest is charged", ["the agent says it does not have that information"]),
                     ("scam", "asks whether this call is a scam", ["the agent says the call can be verified by phoning Freya Bank's customer service number"]),
                     ("discount", "asks whether {he} gets a discount for paying everything today", ["no discount or waiver is offered"])):
        add("Asks back", f"{k}-all-today", f"{VERIFY} After hearing the debts, {q}. After the answer, pays {them} in full today. {Y1}", full_all, ck + ["one read-back for all promises"])
    return R

def b_actions(t, i, p):
    """What the customer can do with one product: (key, sentence, promise or None, note reason or None, check)."""
    pid, name, full, fl = f"p{i + 1}", PNAME[p["type"]], p["past_due"], floor_of(p)
    ok, moved, _ = date_buckets(t, p)
    a1 = attempt1(t)
    out = [("full-today", f"Pays {name} in full today.", P(pid, full, iso(t)), None, f"{name}: {money(full)} today")]
    if ok:
        d = ok[min(2, len(ok) - 1)]; ph, _ = date_phrases(t, d)[0]
        out.append(("full-date", f"Pays {name} in full {ph}.", P(pid, full, iso(d)), None, f"{name}: {money(full)} on {long_day(d)}"))
    if moved:
        d = moved[0]; nd = _promise_day(t, d, p["days_late"])
        out.append(("full-moved", f"Wants to pay {name} in full on {long_day(d)}; accepts the day the agent offers instead for it.", P(pid, full, iso(nd)), None,
                    f"{name}: {long_day(d)} is not accepted, {long_day(nd)} is offered"))
    pa = partial_amounts(p)
    amt = pa[1][0] if len(pa) > 1 else pa[0][0]
    out.append(("partial", f"For {name}, says the full amount is not possible but can pay {money(amt)} today.", P(pid, amt, iso(t)), None, f"{name}: {money(amt)} today is accepted"))
    out.append(("too-much", f"For {name}, says the full amount is too much; accepts the smaller amount the agent offers for it, today.", P(pid, fl, iso(t)), None,
                f"{name}: the agent offers the floor {money(fl)}"))
    for bl in below_amounts(p)[:1]:
        out.append(("below", f"For {name}, says only {money(bl)} is possible today; accepts the amount the agent offers instead for it, today.", P(pid, fl, iso(t)), None,
                    f"{name}: {money(bl)} is not accepted, {money(fl)} is offered"))
    if a1 <= deadline(t, p):
        out.append(("first-offer", f"For {name}, cannot name a day; accepts the agent's first offer for it.", P(pid, full, iso(a1)), None,
                    f"{name}: the agent offers {money(full)} on {long_day(a1)}"))
    out += [("refused", f"Flatly refuses to pay {name}, whatever is offered.", None, "refused", f"a refused note for {name}"),
            ("dispute", f"Says {name} is not {{hers}}, {{he}} never had it.", None, "dispute", f"a dispute note for {name}"),
            ("paid", f"Says {name} was already paid yesterday.", None, "paid", f"a paid note for {name}")]
    return out

def b_mixes(b, t):
    """Each product gets a different outcome (rule B8): promises, notes and the closing computed per product."""
    c = BASE[b]; ps = c["products"]; x = pron(c); ids = [f"p{i + 1}" for i in range(len(ps))]
    acts = [b_actions(t, i, p) for i, p in enumerate(ps)]
    R = []
    import itertools
    for combo in itertools.product(*acts):
        keys = [a[0] for a in combo]
        if len(set(keys)) < len(keys): continue                     # every product does something different
        prs = [a[2] for a in combo if a[2]]; notes = [N(ids[i], a[3]) for i, a in enumerate(combo) if a[3]]
        if prs: exp = E("promise", None, prs, notes)
        elif all(n["reason"] == "paid" for n in notes): exp = E("paid", None, notes=notes)
        else: exp = E("no_promise", next(n["reason"] for n in notes if n["reason"] != "paid"), notes=notes)
        lead = "Cannot pay everything today. " if any(k != "full-today" for k in keys) else ""
        script = f"{VERIFY} {lead}" + " ".join(a[1] for a in combo) + (f" {Y1}" if prs else "")
        ck = [a[4] for a in combo] + (["one read-back for all promises"] if len(prs) > 1 else [])
        if notes and not prs: ck.append(f"closed as {exp['outcome']}" + (f" / {exp['reason']}" if exp["reason"] else ""))
        R.append(("Multi-product mix", "mix-" + "+".join(keys), script.format(**x), exp, ck, True))
    return R

def b_partial(b, t):
    """Brief B versions of the amount families: one product partial / below floor, the others in full today."""
    c = BASE[b]; ps = c["products"]; x = pron(c); ids = [f"p{i + 1}" for i in range(len(ps))]; T = iso(t)
    R = []
    for j, p in enumerate(ps):
        others = [P(i, q["past_due"], T) for k, (i, q) in enumerate(zip(ids, ps)) if k != j]
        rest = " and ".join(PNAME[q["type"]] for k, q in enumerate(ps) if k != j)
        for amt, why in partial_amounts(p):
            for ph, kind in amount_phrases(amt, " today"):
                R.append(("Partial", f"{ids[j]}-{money(amt)}-{kind}", f"{VERIFY} Pays {rest} in full today. For {PNAME[p['type']]}, says the full amount is not possible but can pay {ph}. {Y1}".format(**x),
                          E("promise", None, others + [P(ids[j], amt, T)]), [f"{PNAME[p['type']]}: {money(amt)}, {why}, is accepted", "one read-back for all promises"], True))
        for amt in below_amounts(p):
            R.append(("Below floor", f"{ids[j]}-below-{money(amt)}", f"{VERIFY} Pays {rest} in full today. For {PNAME[p['type']]}, says only {money(amt)} is possible today; accepts the amount the agent offers instead for it, today. {Y1}".format(**x),
                      E("promise", None, others + [P(ids[j], floor_of(p), T)]), [f"{money(amt)} is not accepted", f"the agent offers the floor of {PNAME[p['type']]}, {money(floor_of(p))}"], True))
    return R

# ---------------------------------------------------------------- assembling
def make_case(b, t, now_hm, fam, key, script, exp, checks, style_ok):
    c = copy.deepcopy(BASE[b])
    c["variables"] = dict(c["variables"], now=f"{iso(t)}T{now_hm}")
    c.update({"id": None, "base_id": b, "brief": "B" if len(c["products"]) > 1 else "A", "family": fam, "level": "gen",
              "title": f"{fam}: {key} ({WD[t.weekday()][:3]} {iso(t)})", "script": script,
              "checks": [f"the call is on {WD[t.weekday()]} {t.day} {MONTHS[t.month - 1]} {t.year}"] + checks,
              "style": None, "third_party": fam in ("Callback", "Third party"), "expected": exp})
    c["_key"], c["_style_ok"] = key, style_ok
    c["_kind"] = "-".join(w for w in key.split("-") if not w[:1].isdigit())   # the situation without its dates and amounts
    return c

def pool(seed):
    """Every candidate case, in a fixed order."""
    rng = random.Random(seed)
    out = []
    for day in DAYS:
        t = _d(day)
        for b in A_BASES + B_BASES:
            hm = TIMES[(DAYS.index(day) + int(b[4:])) % len(TIMES)]
            cb_hm = CB_TIMES[(DAYS.index(day) * 3 + int(b[4:])) % len(CB_TIMES)]
            c = BASE[b]
            if len(c["products"]) == 1:
                rows = a_templates(b, t)
                pay = (f"Pays the full amount today. {Y}", E("promise", None, [P("p1", c["products"][0]["past_due"], day)]), ["promise for today"])
            else:
                rows = b_whole(b, t) + b_partial(b, t) + b_mixes(b, t)
                them = "both" if len(c["products"]) == 2 else "all three"
                pay = (f"Pays {them} in full today. {Y1}", E("promise", None, [P(f"p{i + 1}", p["past_due"], day) for i, p in enumerate(c["products"])]), ["one read-back for all promises"])
            rows += id_templates(b, t, *pay)
            for r in rows: out.append(make_case(b, t, hm, *r))
            for r in third_templates(b, t, cb_hm, rng): out.append(make_case(b, t, cb_hm, *r))
    return out

STYLES = ["pieces", "corrects", "talkative", "asks_back", "in_words"]
FAMILY_WEIGHT = {"Multi-product mix": 3, "Dates": 2, "Partial": 1.5, "Identity": 1.5, "Mid-call change": 1.5, "Callback": 1.5}

def spread(cands, rng):
    """Shuffle, then interleave by (base, day) so a family's picks cover many customers and call days."""
    rng.shuffle(cands)
    buckets = collections.OrderedDict()
    for c in cands: buckets.setdefault((c["base_id"], c["variables"]["now"][:10]), []).append(c)
    keys = list(buckets); rng.shuffle(keys)
    out = []
    while keys:
        nxt = []
        for k in keys:
            out.append(buckets[k].pop())
            if buckets[k]: nxt.append(k)
        keys = nxt
    return out

def pick_group(cands, n, rng):
    """n cases from one group (A, B with two products, B with three): families by weight, keys and days spread out."""
    by_fam = collections.defaultdict(list)
    for c in cands: by_fam[c["family"]].append(c)
    fams = sorted(by_fam)
    w = {f: FAMILY_WEIGHT.get(f, 1.0) for f in fams}
    quota = {f: min(len(by_fam[f]), max(1, int(n * w[f] / sum(w.values())))) for f in fams}
    while sum(quota.values()) < n:                                     # top up the families that still have cases
        room = [f for f in fams if quota[f] < len(by_fam[f])]
        if not room: break
        f = min(room, key=lambda f: (quota[f] / w[f], f)); quota[f] += 1
    while sum(quota.values()) > n:
        f = max(fams, key=lambda f: (quota[f] / w[f], f)); quota[f] -= 1
    out = []
    for f in fams:
        by_key = collections.defaultdict(list)
        for c in by_fam[f]: by_key[c["_kind"]].append(c)
        ks = sorted(by_key); rng.shuffle(ks)
        lists = {k: spread(by_key[k], rng) for k in ks}
        got, i = [], 0
        while len(got) < quota[f]:
            k = ks[i % len(ks)]
            if lists[k]: got.append(lists[k].pop(0))
            i += 1
        out += got
    return out

def assign_styles(cases):
    k = 0
    for c in cases:
        if not c["_style_ok"]: continue
        dates = json.dumps(c["expected"]["promises"]) + c["script"]
        options = [None] + STYLES
        s = options[k % len(options)]; k += 1
        if s == "talkative" and ("-30" in dates or "30th" in dates or "thirtieth" in dates): s = None   # the talkative persona's wedding is on the 30th
        c["style"] = s
    return cases

def generate(n=1000, seed=7, share_a=0.6, share_b2=0.4):
    self_check()
    rng = random.Random(seed)
    seen, cands = set(), []
    for c in pool(seed):
        k = (c["base_id"], c["variables"]["now"][:10], c["script"])
        if k in seen: continue
        seen.add(k); cands.append(c)
    na = round(n * share_a); nb2 = round((n - na) * share_b2); nb3 = n - na - nb2
    groups = [("A", [c for c in cands if c["brief"] == "A"], na),
              ("B2", [c for c in cands if len(c["products"]) == 2], nb2),
              ("B3", [c for c in cands if len(c["products"]) == 3], nb3)]
    out = []
    for _, g, k in groups: out += pick_group(g, k, rng)
    out.sort(key=lambda c: (c["brief"], c["family"], c["variables"]["now"], c["base_id"], c["_key"]))
    assign_styles(out)
    for i, c in enumerate(out):
        c["id"] = f"G{i + 1:04d}-{c['brief']}-{c['base_id'][4:]}-{c['variables']['now'][5:10].replace('-', '')}-{c['_key']}"[:90]
    for c in out: del c["_key"], c["_style_ok"], c["_kind"]
    return out

def split(cases, parts):
    """Deal each (brief, family) group across the parts in turn, so every part mixes A and B and all families."""
    out = [[] for _ in range(parts)]
    k = 0
    for c in sorted(cases, key=lambda c: (c["brief"], c["family"], c["id"])):
        out[k % parts].append(c); k += 1
    return out

def summary(cases):
    lines = []
    def table(title, key):
        cnt = collections.Counter(key(c) for c in cases)
        lines.append(f"\n{title}")
        for k, v in sorted(cnt.items(), key=lambda kv: str(kv[0])): lines.append(f"  {str(k):<28} {v:>5}")
    table("brief / products", lambda c: f"{c['brief']} ({len(c['products'])} product{'s' if len(c['products']) > 1 else ''})")
    table("family (brief)", lambda c: f"{c['family']} ({c['brief']})")
    table("outcome", lambda c: f"{c['expected']['outcome']} / {c['expected']['reason']}")
    table("style", lambda c: c["style"] or "none")
    table("call day", lambda c: f"{c['variables']['now'][:10]} {WD[_d(c['variables']['now'][:10]).weekday()][:3]}")
    table("base customer", lambda c: c["base_id"])
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=1000); ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "results" / "cases-1000.json")); ap.add_argument("--split", type=int, default=0, help="cases per part file (0: no parts)")
    a = ap.parse_args()
    cases = generate(a.n, a.seed)
    out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(cases, open(out, "w"), indent=1)
    print(f"{len(cases)} cases written to {out}")
    if a.split:
        parts = split(cases, max(1, -(-len(cases) // a.split)))
        for i, part in enumerate(parts, 1):
            pth = out.with_name(f"{out.stem}-part{i}.json"); json.dump(part, open(pth, "w"), indent=1)
            print(f"  part {i}: {len(part)} cases ({sum(c['brief'] == 'A' for c in part)} A, {sum(c['brief'] == 'B' for c in part)} B) -> {pth}")
    print(summary(cases))

if __name__ == "__main__": main()
