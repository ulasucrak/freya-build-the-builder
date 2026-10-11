"""250 live cases in which every case is a different scenario, Brief A (one product) and Brief B (two or three products).

    python tools/gen_cases_250.py --n 250 --seed 25 --out results/cases-250-new.json
    python tools/run_cases.py <agent_id> --cases results/cases-250-new.json

Built on tools/gen_cases_1000.py (calendar, calculator, words, base customers) with new, freshly worded scripts.
A scenario is (brief, family, sub-variant, per-product actions for B, call day, style, phrasing kind); no two cases share
one, and even without the call day and the style no two cases share (brief, family, sub-variant, actions, phrasing kind).
No script repeats one of the existing banks (results/edge-cases*.json, the 1000 set). Expected bank records come from
the calculator; situations whose record is not certain under the briefs are not generated. The set leans on what the
v4 enforcement decides in code: counter-offers and "no, but ..." replies, hardship that is not a refusal, the offer
ladder per product in Brief B, a dispute naming one product versus "the loan" when there are two loans, "I don't want to
talk" said twice in other words, callbacks outside the window twice, corrections, amounts in words, vague periods.
Test tooling only: nothing here is used by the builder, calls Freya or calls the bank.
"""
import argparse, collections, copy, itertools, json, pathlib, random, re, sys, zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import gen_cases_1000 as g1  # noqa: E402
from gen_cases_1000 import (BASE, DAYS, CB_TIMES, TIMES, MONTHS, WD, P, E, N, TR, PNAME, FAILS_FIRST, STYLES, HOURS,  # noqa: E402
                            iso, long_day, floor_of, is_late, deadline, attempt1, callback_ok, words, ordinal_words, ordinal,
                            pron, partial_amounts, below_amounts, date_buckets, money, cb_days, wrong_dobs, wrong_phones,
                            weekday_this_week, _d, _bday, _next_bday, _promise_day, _weekday_next_week, HOLIDAYS_A)

KINDS = ("digits", "words", "mixed", "relative", "correction")      # how amounts and dates are said; "none" when no value is said
A_BASES, B_BASES = g1.A_BASES, g1.B_BASES
FAMILIES = ["Pay full", "Dates", "Partial", "Below floor", "Refusal", "Persuasion", "Counter-offer", "Dispute", "Already paid", "Hardship",
            "Transfer", "No talk", "Identity", "Mid-call change", "Asks back", "Callback", "Third party"]
FAMILIES_B = FAMILIES + ["Multi-product mix"]
V4_TAGS_A = {"counter", "no-but", "hardship-job", "no-talk", "no-talk-other", "cb-max-asks", "correction", "amount-words", "vague"}
V4_TAGS_B = {"counter", "no-but", "hardship-job", "no-talk", "no-talk-other", "cb-max-asks", "ladder", "dispute-named", "too-much", "correction", "amount-words"}
V4_TAGS_B3 = {"dispute-which"}                                      # "the loan" with two loans: only the three-product customers have two
V4_TAGS_B2 = {"dispute-the-loan"}                                   # "the loan" with one loan: unambiguous

def h(*parts): return zlib.crc32("|".join(map(str, parts)).encode())

# ---------------------------------------------------------------- ways to say a value
def rel_phrase(t, d):
    """'tomorrow', 'this <weekday>' or 'next <weekday>' when it has one reading under rule 22, else None."""
    for ph, kind in g1.date_phrases(t, d):
        if kind == "relative": return ph
    return None

def day_digits(d): return f"on {long_day(d)}"
def day_words(d): return f"on the {ordinal_words(d.day)} of {MONTHS[d.month - 1]}"

def wrong_day(d):
    """A day said by mistake before the correction: next to d, never a 30th (the talkative persona's wedding)."""
    for k in (1, -1, 2, -2):
        w = d + g1.dt.timedelta(days=k)
        if w.day not in (30, 31) and w != d: return w
    return d + g1.dt.timedelta(days=3)

def wrong_amount(x, lo, hi):
    """An amount said by mistake before the correction, of the same kind as x: inside [lo, hi] and not x itself, so the
    slip never turns a partial amount into a below-floor one or the other way round."""
    for k in (40, -40, 20, -20, 10, -10, 5, -5, 2, -2, 1, -1):
        if lo <= x + k <= hi: return float(x + k)
    raise ValueError((x, lo, hi))

def spoken(t, amt, d, kind, lo=None, hi=None):
    """How the customer says amount amt (None: no amount) for day d (None: today), in phrasing kind. None if that kind
    cannot say it (words for an amount with pence, relative for a day without a one-reading relative name, mixed
    without both values). lo and hi bound the mistaken first amount of a correction."""
    when = "today" if d is None else None
    if kind == "digits":
        a = money(amt) if amt is not None else None; w = when or day_digits(d)
    elif kind == "words":
        if amt is not None and amt != int(amt): return None
        a = words(amt) if amt is not None else None; w = when or day_words(d)
    elif kind == "relative":
        if d is None: return None
        r = rel_phrase(t, d)
        if r is None: return None
        a = money(amt) if amt is not None else None; w = r
    elif kind == "mixed":
        if amt is None or d is None: return None
        if amt == int(amt) and d.day % 2: a, w = words(amt), day_digits(d)
        else: a, w = money(amt), day_words(d)
    elif kind == "correction":
        if d is not None:
            a = money(amt) if amt is not None else None
            w = f"{day_digits(wrong_day(d))}, no, sorry, {long_day(d)}"
        else:
            return f"{money(wrong_amount(amt, lo, hi))}, sorry, I mean {money(amt)}, today"
    else:
        raise ValueError(kind)
    return f"{a} {w}" if a is not None else w

# ---------------------------------------------------------------- scenario rows
class Rows(list):
    """Scenario rows for one customer on one call day. Each row: family, sub-variant, phrasing kind, script, expected,
    checks, whether a talking style may be added, v4 tags, per-product actions (Brief B)."""
    def __init__(self, b):
        super().__init__(); self.b = b; self.x = pron(BASE[b])

    def add(self, fam, sub, phr, body, exp, checks, style_ok=True, tags=(), actions=None, verified=True):
        if body is None: return
        n = len(BASE[self.b]["products"])
        if actions is None: actions = () if n == 1 else ("-",) * n
        pre = VER[h(self.b, fam, sub, phr) % len(VER)] + " " if verified else ""
        self.append({"family": fam, "sub": sub, "phr": phr, "script": (pre + body).format(**self.x), "expected": exp, "checks": list(checks),
                     "style_ok": style_ok, "tags": set(tags), "actions": tuple(actions)})

VER = ["Says yes, it is {him}, and gives the right date of birth and phone digits when asked.",
       "Confirms {he} is {full} and answers the date of birth and the last four phone digits correctly.",
       "Answers to {his} name and gets both security questions right.",
       "Says 'yes, speaking' and passes both security checks without trouble."]
YES = ["Says yes when the agent reads it back.", "Agrees when the details are read back.", "Answers the read-back with a clear yes."]
YES_B = ["Says one clear yes to the combined read-back.", "Agrees once when all the promises are read back together."]

def yes(*k): return YES[h(*k) % len(YES)]
def yes_b(*k): return YES_B[h(*k) % len(YES_B)]
def prom(amt, d, pid="p1"): return E("promise", None, [P(pid, float(amt), iso(d))])

def first_with(days, t, amt, kind, start=0):
    """The first day in days (from start, wrapping) the kind can say, with its phrase."""
    for i in range(len(days)):
        d = days[(start + i) % len(days)]
        s = spoken(t, amt, d, kind)
        if s is not None: return d, s
    return None, None

def band_text(p): return "late band: at most two persuasion attempts" if is_late(p) else "early band: one persuasion attempt"

# ---------------------------------------------------------------- Brief A
def a_rows(b, t):
    c = BASE[b]; p = c["products"][0]
    full, fl, late = p["past_due"], floor_of(p), is_late(p)
    dl, a1 = deadline(t, p), attempt1(t)
    ok, moved, too_late = date_buckets(t, p)
    weekend = [d for d in moved if d.weekday() >= 5]; hols = [d for d in moved if d.weekday() < 5]
    pa = partial_amounts(p)
    mid = pa[1][0] if len(pa) > 1 else None
    rnd = pa[2][0] if len(pa) > 2 else mid
    bl = below_amounts(p)
    R = Rows(b); add = R.add
    Y = lambda *k: yes(b, *k)

    # --- pays the full amount
    add("Pay full", "yes-today", "none", f"When asked about today, says yes, {{he}} will clear the whole balance today. {Y('pf1')}", prom(full, t),
        ["the full amount is recorded for today", "the text-message sentence is said after the promise is recorded"])
    for k in ("digits", "words"):
        s = spoken(t, full, None, k)
        add("Pay full", "names-amount", k, s and f"Asked about today, answers '{s}, that is fine.' {Y('pf2', k)}", prom(full, t),
            [f"{money(full)} today is recorded"], tags=["amount-words"] if k == "words" else ())
    add("Pay full", "self-correct", "correction", f"Asked about today, answers '{money(fl)} today, no wait, all of it, {money(full)}, today.' {Y('pf3')}", prom(full, t),
        [f"the corrected amount, {money(full)}, is recorded for today"], tags=["correction"])
    add("Pay full", "checks-app", "none", f"Asks for a moment to look at {{his}} banking app, then says yes, the whole amount today. {Y('pf4')}", prom(full, t),
        ["the agent waits and does not invent anything", "the full amount is recorded for today"])
    add("Pay full", "repeat-amount", "none", f"Asks the agent to say the amount once more, then agrees to pay all of it today. {Y('pf5')}", prom(full, t),
        [f"the agent repeats {money(full)}", "the full amount is recorded for today"])

    # --- dates (rules 19-23)
    for i, k in enumerate(("digits", "words", "mixed", "correction")):
        d, s = first_with(ok, t, full if k == "mixed" else None, k, start=1 + i)
        if d is None: continue
        body = (f"Cannot pay today. Offers {s}, the whole balance. {Y('d1', k)}" if k == "mixed"
                else f"Cannot pay today. Says {{he}} can pay the whole amount {s}. {Y('d1', k)}")
        add("Dates", "own-day", k, body, prom(full, d), [f"the promise is for {long_day(d)}"], tags=["correction"] if k == "correction" else ())
    tom = t + g1.dt.timedelta(days=1)
    if tom in ok:
        add("Dates", "tomorrow", "relative", f"Cannot pay today. Says 'tomorrow, the full amount, I promise'. {Y('d2')}", prom(full, tom), [f"'tomorrow' is recorded as {long_day(tom)}"])
    for word in ("this", "next"):
        hits = [(d, rel_phrase(t, d)) for d in ok if d != tom and (rel_phrase(t, d) or "").startswith(word + " ")]
        if hits:
            d, s = hits[h(b, iso(t), word) % len(hits)]
            add("Dates", f"{word}-weekday", "relative", f"Cannot pay today. Says {{he}} can pay everything {s}. {Y('d3', word)}", prom(full, d), [f"'{s}' is recorded as {long_day(d)}"])
    for i, k in enumerate(("digits", "relative", "words")):
        d, s = first_with(weekend, t, None, k, start=i)
        if d is None: continue
        nd = _promise_day(t, d, p["days_late"])
        add("Dates", "weekend", k, f"Cannot pay today. Asks to pay everything {s}. Takes the business day the agent suggests instead. {Y('d4', k)}", prom(full, nd),
            [f"{long_day(d)} is not a business day and is not recorded", f"{long_day(nd)} is offered instead"])
    for d in hols[:1]:
        nd = _promise_day(t, d, p["days_late"])
        add("Dates", "bank-holiday", "digits", f"Cannot pay today. Wants to pay the full amount {day_digits(d)}. Takes the day the agent suggests instead. {Y('d5')}", prom(full, nd),
            [f"{long_day(d)} is a bank holiday and is not recorded", f"{long_day(nd)} is offered"])
    for k, idx in (("digits", 1), ("words", 4)):
        if len(too_late) > idx:
            d = too_late[idx]; s = spoken(t, None, d, k)
            add("Dates", "past-deadline", k, f"Cannot pay today. Says the earliest {{he}} can manage is {s}. When the agent names the latest day it can record, agrees to it. {Y('d6', k)}",
                prom(full, dl), [f"the agent says the latest day it can record is {long_day(dl)} and offers it"])
    add("Dates", "deadline-day", "words", f"Cannot pay today. Says {{he}} can pay it all {day_words(dl)}. {Y('d7')}", prom(full, dl), [f"{long_day(dl)}, the last allowed day, is accepted"])
    for sub, vague, wd, wdname in (("end-of-next-week", "towards the end of next week", 3, "the Thursday"), ("after-the-weekend", "some time after the weekend", 1, "Tuesday")):
        d = _weekday_next_week(t, wd); nd = _promise_day(t, d, p["days_late"])
        add("Dates", sub, "relative", f"Cannot pay today. Says '{vague}'. When asked for the exact day, says '{wdname}'. If the agent has to suggest another day, accepts it. {Y('d8', sub)}",
            prom(full, nd), ["the agent asks once for the exact day", f"the promise is for {long_day(nd)}"], tags=["vague"])
    if len(ok) > 2:
        d = ok[2]
        add("Dates", "in-a-few-days", "digits", f"Cannot pay today. Says 'in a few days'. When asked for the exact day, says '{long_day(d)}'. {Y('d9')}", prom(full, d),
            ["the agent asks once for the exact day", f"the promise is for {long_day(d)}"], tags=["vague"])
    add("Dates", "tax-refund", "none", f"Cannot pay today. Says {{he}} will pay once {{his}} tax refund comes in. Asked for the exact day, says {{he}} cannot say. Agrees to the day the agent then proposes. {Y('d10')}",
        prom(full, dl), ["the agent asks once for the exact day", f"the agent proposes the last allowed day, {long_day(dl)}"], tags=["vague"])
    add("Dates", "two-months", "none", f"Cannot pay today. Says {{he}} needs about two months. Agrees to the latest day the agent can take. {Y('d11')}", prom(full, dl),
        [f"the agent offers {long_day(dl)}, the latest day it can record"])

    # --- partial amounts (rule 27)
    if mid:
        for k, amt in (("digits", mid), ("words", rnd), ("correction", mid)):
            s = spoken(t, amt, None, k, fl, full - 1)
            add("Partial", "today", k, s and f"Says the whole amount is not possible today, but {{he}} can pay {s}. {Y('p1', k)}", prom(amt, t),
                [f"{money(amt)} is accepted without argument"], tags={"words": ["amount-words"], "correction": ["correction"]}.get(k, ()))
        for i, k in enumerate(("digits", "words", "mixed", "relative")):
            d, s = first_with(ok, t, rnd, k, start=1 + i)
            if d is None: continue
            add("Partial", "with-day", k, f"Says the whole amount is not possible, but {{he}} can pay {s}. {Y('p2', k)}", prom(rnd, d),
                [f"{money(rnd)} on {long_day(d)} is accepted"], tags=["amount-words"] if k in ("words", "mixed") else ())
    if fl == int(fl):
        add("Partial", "exact-floor", "words", f"Says {{he}} can manage {words(fl)} today, not a penny more. {Y('p4')}", prom(fl, t),
            [f"{money(fl)}, the floor, is accepted without argument"], tags=["amount-words"])
    add("Partial", "exact-floor", "digits", f"Says {{he}} can pay {money(fl)} today and no more. {Y('p3')}", prom(fl, t), [f"{money(fl)}, the floor, is accepted without argument"])

    # --- below the floor, or the full amount is too much (rules 26, 27)
    if bl:
        for k in ("digits", "words"):
            s = spoken(t, bl[0], None, k)
            add("Below floor", "today", k, s and f"Says {{he}} can only find {s}. Takes the amount the agent proposes instead, today. {Y('b1', k)}", prom(fl, t),
                [f"{money(bl[0])} is not recorded", f"the agent offers the floor, {money(fl)}"], tags=["amount-words"] if k == "words" else ())
        for i, k in enumerate(("relative", "mixed")):
            d, s = first_with(ok, t, bl[-1], k, start=2 + i)
            if d is None: continue
            add("Below floor", "with-day", k, f"Says {{he}} can only manage {s}. Takes the amount the agent proposes instead, on that same day. {Y('b2', k)}", prom(fl, d),
                [f"{money(bl[-1])} is not recorded", f"the floor, {money(fl)}, is recorded for {long_day(d)}"])
        if len(bl) > 1:
            add("Below floor", "corrects-still-below", "correction", f"Says {{he}} can pay '{money(bl[1])}, sorry, {money(bl[0])}, today'. Takes the amount the agent proposes instead, today. {Y('b3')}",
                prom(fl, t), [f"{money(bl[0])} is not recorded", f"the agent offers the floor, {money(fl)}"], tags=["correction"])
    add("Below floor", "minimum-first", "none", f"Says paying it all is too much and, before the agent proposes anything, asks to pay just the minimum today. {Y('b4')}", prom(fl, t),
        [f"the floor, {money(fl)}, is recorded for today"], tags=["too-much"])

    if len(ok) > 2:
        d = ok[2]
        add("Below floor", "minimum-on-a-day", "digits", f"Says the whole amount is too much and, before the agent proposes anything, asks to pay just the minimum {day_digits(d)}. {Y('b5')}",
            prom(fl, d), [f"the floor, {money(fl)}, is recorded for {long_day(d)}"], tags=["too-much"])

    # --- refusals (rules 23, 26, 27, 30, 31)
    ladder = [band_text(p), f"attempt 1: {money(full)} on {long_day(a1)}"] + ([f"attempt 2: {money(fl)} on {long_day(dl)}"] if late else []) + ["no proposal is repeated"]
    add("Refusal", "blunt", "none", "Answers 'no' about today and turns down every proposal after that, never naming a day or an amount.", E("no_promise", "refused"), ladder)
    add("Refusal", "non-answer", "none", "To every question about paying, says only 'I'd rather not say', never naming a day or an amount.", E("no_promise", "refused"),
        ["a non-answer counts as a refusal", band_text(p), "no proposal is repeated"])
    add("Refusal", "too-much-nothing", "none", "Says it is far too much. Turns down the smaller amount as well and every proposal after it, never naming a day or an amount.",
        E("no_promise", "refused"), [f"the agent offers the floor, {money(fl)}, and says the rest stays overdue", "no proposal is repeated"])
    if len(too_late) > 3:
        d = too_late[3]
        add("Refusal", "refuses-deadline", "digits", f"Cannot pay today. Says {{he}} can pay {day_digits(d)} and no earlier. Turns down the latest day the agent offers and every proposal after it.",
            E("no_promise", "refused"), [f"the agent says the latest day it can record is {long_day(dl)}", "no proposal is repeated"])
    add("Refusal", "question-then-no", "none", "After hearing the debt, asks whether the bank charges interest on it. After the answer, says no to today and to every proposal, never naming a day or an amount.",
        E("no_promise", "refused"), ["the agent says it does not have that information", band_text(p), "no proposal is repeated"])
    if bl:
        add("Refusal", "refuses-floor", "digits", f"Says {{he}} can only find {money(bl[-1])} today. Turns down the amount the agent proposes instead and every proposal after it.",
            E("no_promise", "refused"), [f"{money(bl[-1])} is not recorded", f"the agent offers the floor, {money(fl)}, once", "no proposal is repeated"])

    # --- persuasion (rule 30)
    if a1 <= dl:
        add("Persuasion", "takes-first", "none", f"Says {{he}} cannot pay today. Takes the first day the agent proposes. {Y('r1')}", prom(full, a1),
            [f"the agent proposes {money(full)} on {long_day(a1)}"])
        add("Persuasion", "unsure-then-first", "none", f"To the question about today, says only 'I'm not sure'. Takes the first proposal the agent makes after that. {Y('r2')}", prom(full, a1),
            [f"the agent proposes {money(full)} on {long_day(a1)}"])
        add("Persuasion", "question-then-first", "none", f"After hearing the debt, asks whether {{he}} is speaking to a person. After the answer, says {{he}} cannot pay today and takes the first day the agent proposes. {Y('r5')}",
            prom(full, a1), ["the agent says it is Freya Bank's digital assistant", f"the agent proposes {money(full)} on {long_day(a1)}"])
        if late:
            add("Persuasion", "takes-second", "none", f"Says no to today and no to the first proposal. Takes the second proposal. {Y('r3')}", prom(fl, dl),
                [f"attempt 1: {money(full)} on {long_day(a1)}", f"attempt 2: {money(fl)} on {long_day(dl)}"])
            add("Persuasion", "rather-not-twice", "none", f"Says {{he}} would rather not pay today, and would rather not on the first proposed day either. Agrees to the second proposal. {Y('r4')}",
                prom(fl, dl), [f"attempt 2 is {money(fl)} on {long_day(dl)}"])

    # --- counter-offers and "no, but ..." (rules 27, 29)
    if mid:
        for i, k in enumerate(KINDS):
            d, s = first_with(ok, t, rnd if k != "correction" else mid, k, start=2 + i)
            if d is None: continue
            amt = rnd if k != "correction" else mid
            add("Counter-offer", "no-but-today-question", k, f"Asked about today, answers 'No, but I can do {s}.' {Y('c1', k)}", prom(amt, d),
                [f"{money(amt)} on {long_day(d)} is recorded", "the counter-offer is not treated as a refusal"],
                tags=["counter", "no-but"] + (["amount-words"] if k in ("words", "mixed") else []) + (["correction"] if k == "correction" else []))
        if a1 <= dl:
            for i, k in enumerate(("digits", "relative")):
                d, s = first_with([d for d in ok if d != a1], t, mid, k, start=3 + i)
                if d is None: continue
                add("Counter-offer", "no-but-first-proposal", k, f"Says no to today. To the first proposal, answers 'No, but {s} works for me.' {Y('c2', k)}", prom(mid, d),
                    [f"{money(mid)} on {long_day(d)} is recorded", "the counter-offer is not treated as a refusal"], tags=["counter", "no-but"])
            if late and len(ok) > 1:
                d = ok[1]
                add("Counter-offer", "no-but-second-proposal", "digits", f"Says no to today and no to the first proposal. To the second proposal, answers 'No, but I can pay {money(mid)} {day_digits(d)}.' {Y('c3')}",
                    prom(mid, d), [f"{money(mid)} on {long_day(d)} is recorded"], tags=["counter", "no-but"])
        d, s = first_with(weekend, t, mid, "relative", start=1)
        if d:
            nd = _promise_day(t, d, p["days_late"])
            add("Counter-offer", "weekend-day", "relative", f"Asked about today, answers 'No, but I can do {s}.' Takes the business day the agent suggests instead. {Y('c4')}", prom(mid, nd),
                [f"{long_day(d)} is not recorded", f"{money(mid)} on {long_day(nd)} is recorded"], tags=["counter", "no-but"])
    if len(ok) > 3:
        d = ok[3]
        add("Counter-offer", "no-but-floor", "digits", f"Asked about today, answers 'No, but I can do {money(fl)} {day_digits(d)}.' {Y('c7')}", prom(fl, d),
            [f"{money(fl)}, the floor, on {long_day(d)} is recorded"], tags=["counter", "no-but"])
    if tom in ok:
        add("Counter-offer", "no-but-tomorrow", "relative", f"Asked about today, answers 'No, but tomorrow I can pay all of it.' {Y('c8')}", prom(full, tom),
            [f"the full amount is recorded for {long_day(tom)}"], tags=["counter", "no-but"])
    if bl and len(ok) > 2:
        d = ok[2]
        add("Counter-offer", "below-floor-day", "digits", f"Asked about today, answers 'No, but I could do {money(bl[0])} {day_digits(d)}.' Takes the amount the agent proposes instead, on that same day. {Y('c5')}",
            prom(fl, d), [f"{money(bl[0])} is not recorded", f"the floor, {money(fl)}, is recorded for {long_day(d)}"], tags=["counter", "no-but"])
    if len(too_late) > 2:
        d = too_late[2]
        add("Counter-offer", "full-too-late", "words", f"Asked about today, answers 'No, but I could clear it all {day_words(d)}.' Agrees to the latest day the agent can record. {Y('c6')}",
            prom(full, dl), [f"the agent offers {long_day(dl)}, the latest day it can record"], tags=["counter", "no-but"])

    # --- other things customers say (rules 39-45)
    nop = ["no promise is taken", "the agent does not argue"]
    add("Dispute", "identity-theft", "none", f"After hearing the debt, says someone must have used {{his}} details; {{he}} never opened this {PNAME[p['type']][4:]}.", E("no_promise", "dispute"), nop, False)
    add("Dispute", "closed-in-2023", "none", "Says {he} paid this off and closed it back in 2023, so there is nothing to pay.", E("no_promise", "dispute"), ["a product closed in the past is a dispute"] + nop, False)
    add("Dispute", "different-person", "none", "Says this must belong to a different {full}; it is not {his}.", E("no_promise", "dispute"), nop, False)
    add("Dispute", "never-banked", "none", f"Says {{he}} has never had a {PNAME[p['type']][4:]} with Freya Bank and will not pay for one.", E("no_promise", "dispute"), nop, False)
    theft = "the card was stolen months ago and these are not {his} charges" if p["type"] == "credit_card" else "an ex-partner took this loan out in {his} name without asking"
    add("Dispute", "taken-without-consent", "none", f"Says {theft}.", E("no_promise", "dispute"), nop, False)
    paid = ["the agent says payments can take one business day to appear", "no promise is taken"]
    add("Already paid", "app-last-night", "none", "Says {he} paid exactly this overdue amount in the app last night.", E("paid"), paid, False)
    add("Already paid", "phone-banking-morning", "none", "Says {he} paid the overdue amount by phone banking this morning.", E("paid"), paid, False)
    add("Already paid", "branch-yesterday", "none", "Says {he} paid the overdue amount in cash at a branch yesterday and has the receipt.", E("paid"), paid, False)
    add("Already paid", "partner-paid", "none", "Says {his} partner paid the overdue amount for {him} yesterday afternoon.", E("paid"), paid, False)

    # --- hardship (rule 32): never a refusal
    sym = ["one sentence of sympathy", "no persuasion attempt"]
    add("Hardship", "lost-job-decline", "none", "Says 'I can't pay, I lost my job.' Turns down the offer of the support team.", E("no_promise", "hardship"),
        sym + ["closed as hardship, not as a refusal"], False, tags=["hardship-job"])
    add("Hardship", "lost-job-accept", "none", "Says 'I can't pay, I lost my job.' Says yes when offered the support team.", E("transferred", "support", transfers=TR("support")),
        sym + ["transfer to support"], False, tags=["hardship-job"])
    add("Hardship", "illness-accept", "none", "Says {he} is off work with a serious illness and has no income at the moment. Accepts the support team.",
        E("transferred", "support", transfers=TR("support")), sym + ["transfer to support"], False)
    add("Hardship", "illness-decline", "none", "Says {he} has been in hospital for weeks and cannot pay anything. Says no to the support team.", E("no_promise", "hardship"), sym, False)
    add("Hardship", "bereavement-accept", "none", "Says {his} wife died last month and everything is a mess. Says yes to the support team." if BASE[b]["variables"]["title"] == "Mr"
        else "Says {his} husband died last month and everything is a mess. Says yes to the support team.", E("transferred", "support", transfers=TR("support")), sym + ["transfer to support"], False)
    add("Hardship", "bereavement-decline", "none", "Says {his} father died two weeks ago and {he} cannot deal with money right now. Says no to the support team.",
        E("no_promise", "hardship"), sym, False)
    if mid and len(ok) > 1:
        for k, amt in (("digits", mid), ("words", rnd)):
            d, s = first_with(ok, t, amt, k, start=1)
            if d is None: continue
            add("Hardship", "own-offer", k, f"Says {{he}} was made redundant last week. When offered the support team, says no thanks and, in the same answer, says {{he}} can still pay {s}. {Y('h1', k)}",
                prom(amt, d), sym + [f"the customer's own offer, {money(amt)} on {long_day(d)}, is recorded"], False, tags=["hardship-job"] + (["amount-words"] if k == "words" else []))

    # --- transfers (rules 41, 42)
    add("Transfer", "spread-payments", "none", "Asks for a new repayment plan with smaller monthly instalments. Agrees to be put through.",
        E("transferred", "restructuring", transfers=TR("restructuring")), ["no plan is described", "transfer to restructuring"], False)
    add("Transfer", "person-insists", "none", "After hearing the debt, asks for a human being. When the agent asks whether it can help first, says 'no, a person, please'.",
        E("transferred", "human", transfers=TR("general")), ["the agent asks once whether it can help first", "transfer to a person"], False)
    add("Transfer", "person-before-checks", "none", "Says 'yes, that's me' and straight away asks for a real person, before any security question. When the agent asks whether it can help first, still wants a person.",
        E("transferred", "human", transfers=TR("general")), ["the agent asks once whether it can help first", "nothing about the account is said"], False, verified=False)
    add("Transfer", "person-then-stays", "none", f"After hearing the debt, asks for a person. When the agent asks whether it can help first, says 'fine, go on', and then pays the whole amount today. {Y('t1')}",
        prom(full, t), ["the agent asks once whether it can help first", "no transfer", "the full amount is recorded for today"], False)

    # --- does not want to talk (rule 45)
    add("No talk", "says-it-twice", "none", "After the debt is stated, says 'I don't want to talk about this.' When the agent goes on, says it a second time.",
        E("no_promise", "refused"), ["the call ends after the second time", "after the debt is stated the agent never mentions security questions"], False, tags=["no-talk"])
    add("No talk", "other-words", "none", "After the debt is stated, says 'leave me alone, I'm not discussing this.' When the agent goes on, says 'I told you, I'm not having this conversation.'",
        E("no_promise", "refused"), ["the second refusal to talk closes the call", "closed as no_promise / refused"], False, tags=["no-talk", "no-talk-other"])
    add("No talk", "question-between", "none", "After the debt is stated, says {he} does not want to talk about it. Then asks whether this call is a scam. After the answer, says again {he} is not discussing it.",
        E("no_promise", "refused"), ["the agent answers the scam question", "the second refusal to talk closes the call"], False, tags=["no-talk", "no-talk-other"])
    add("No talk", "two-ways", "none", "After the debt is stated, says 'I'm not going into this with a machine.' When the agent goes on, says 'stop, I don't want to talk about it.'",
        E("no_promise", "refused"), ["the second refusal to talk closes the call", "closed as no_promise / refused"], False, tags=["no-talk", "no-talk-other"])
    add("No talk", "once-then-pays", "none", f"After the debt is stated, says once 'I'd rather not talk about this.' When the agent goes on, changes {{his}} mind and agrees to pay the whole amount today. {Y('n1')}",
        prom(full, t), ["one refusal to talk does not end the call", "the full amount is recorded for today"], False, tags=["no-talk"])
    add("No talk", "before-checks", "none", "Says 'yes' to {his} name. Asked for the date of birth, says 'I'm not talking to you.' After the agent explains, says 'I said no.' Never gives a security answer.",
        E("not_verified", "declined"), ["the agent explains once that the questions protect the account", "the debt is not mentioned"], False, tags=["no-talk", "no-talk-other"], verified=False)

    # --- changes during the call (rules 32, 35, 36, 41, 42)
    if mid:
        for k in ("digits", "words"):
            s = spoken(t, mid, None, k)
            add("Mid-call change", "readback-amount", k, s and f"Agrees to pay all of it today. At the read-back says 'can we make that {s} instead?'. Confirms the new read-back.",
                prom(mid, t), [f"the agent reads back again with {money(mid)}", "only the changed promise is recorded"], tags=["correction"] + (["amount-words"] if k == "words" else []))
    d, s = first_with(ok, t, None, "relative", start=3)
    if d:
        add("Mid-call change", "readback-day", "relative", f"Agrees to pay all of it today. At the read-back says 'actually, {s} suits me better'. Confirms the new read-back.",
            prom(full, d), [f"the agent reads back again with {long_day(d)}", "only the changed promise is recorded"], tags=["correction"])
    if weekend:
        d = weekend[-1]; nd = _promise_day(t, d, p["days_late"])
        add("Mid-call change", "readback-weekend", "digits", f"Agrees to pay all of it today. At the read-back asks to make it {long_day(d)} instead. Takes the day the agent then suggests. Confirms the new read-back.",
            prom(full, nd), [f"{long_day(d)} is not recorded, {long_day(nd)} is offered", "only the changed promise is recorded"], tags=["correction"])
    if too_late:
        d = too_late[0]
        add("Mid-call change", "readback-past-deadline", "words", f"Agrees to pay all of it today. At the read-back asks to move it to {day_words(d)[3:]}. Agrees to the latest day the agent can record. Confirms the new read-back.",
            prom(full, dl), [f"the agent offers {long_day(dl)}, the latest day it can record"], tags=["correction"])
    chg = ["the agent says a recorded promise can be changed in the mobile app", "no new or changed promise is recorded after the request"]
    for k, ask in (("move", "asks whether it can be pushed back a week"), ("cancel", "says {he} has changed {his} mind and wants it cancelled"), ("lower", "asks to lower the amount a bit")):
        add("Mid-call change", f"recorded-{k}", "none", f"Pays the whole amount today and confirms. Once the agent says it is recorded, {ask}.", prom(full, t), chg)
    add("Mid-call change", "agrees-then-job", "none", "Agrees to pay all of it today. Before confirming the read-back, says 'actually, I can't pay, I lost my job yesterday'. Says no to the support team.",
        E("no_promise", "hardship"), sym + ["no promise recorded"], False, tags=["hardship-job"])
    add("Mid-call change", "no-then-ill", "none", "Says no to today. Then explains {he} has been diagnosed with a serious illness. Accepts the support team.",
        E("transferred", "support", transfers=TR("support")), ["no persuasion attempt after the illness", "transfer to support"], False)
    add("Mid-call change", "no-then-plan", "none", "Says no to today and no to the first proposal. Then asks for a new plan with lower monthly payments. Agrees to be transferred.",
        E("transferred", "restructuring", transfers=TR("restructuring")), ["no plan is described", "transfer to restructuring"], False)
    add("Mid-call change", "recorded-then-person", "none", "Pays the whole amount today and confirms. Once it is recorded, asks for a human. When the agent asks whether it can help first, insists.",
        E("transferred", "human", [P("p1", full, iso(t))], transfers=TR("general")), ["the recorded promise stays", "closed as transferred / human"], False)
    if mid:
        add("Mid-call change", "readback-amount", "correction", f"Agrees to pay all of it today. At the read-back says 'make that {money(wrong_amount(mid, fl, full - 1))}, no, sorry, {money(mid)}'. Confirms the new read-back.",
            prom(mid, t), [f"the agent reads back again with {money(mid)}", "only the changed promise is recorded"], tags=["correction"])
    add("Mid-call change", "recorded-then-ill", "none", "Pays the whole amount today and confirms. Once it is recorded, says {he} is starting cancer treatment next week. Accepts the support team.",
        E("transferred", "support", [P("p1", full, iso(t))], transfers=TR("support")), ["the recorded promise stays", "closed as transferred / support"], False)
    add("Mid-call change", "recorded-then-plan", "none", "Pays the whole amount today and confirms. Once it is recorded, asks for a new plan with lower instalments for the future. Agrees to be transferred.",
        E("transferred", "restructuring", [P("p1", full, iso(t))], transfers=TR("restructuring")), ["the recorded promise stays", "no plan is described", "closed as transferred / restructuring"], False)

    # --- the customer asks something back (rules 4, 33, 43, 44)
    QS = [("interest", "asks how much interest is being added", ["the agent says it does not have that information", "no figure is invented"]),
          ("discount", "asks for a discount if {he} pays now", ["no discount or waiver is offered"]),
          ("fees", "asks whether the late fees can be dropped", ["no discount, waiver or instalment plan is offered"]),
          ("credit-file", "asks whether this goes on {his} credit file", ["the agent says it does not have that information", "no consequence is invented"]),
          ("scam", "asks how {he} can know this is not a scam", ["the agent says the call can be verified by phoning Freya Bank's customer service number"]),
          ("robot", "asks whether {he} is speaking to a machine", ["the agent says it is Freya Bank's digital assistant"]),
          ("how-late", "asks how many days late the payment is", [f"the agent says {p['days_late']} days"]),
          ("number-source", "asks where the bank got this phone number", ["the agent says it does not have that information"]),
          ("which-account", "asks which account this is about", [f"the agent names the {PNAME[p['type']][4:]}"])]
    for i, (k, q, ck) in enumerate(QS):
        add("Asks back", f"{k}-today", "none", f"After hearing the debt, {q}. After the answer, pays the whole amount today. {Y('q1', k)}", prom(full, t), ck + ["the call goes on normally"])
        kind = ("digits", "relative", "words")[i % 3]
        d, s = first_with(ok, t, None, kind, start=1 + i)
        if d: add("Asks back", f"{k}-day", kind, f"After hearing the debt, {q}. After the answer, says {{he}} can pay the whole amount {s}. {Y('q2', k)}", prom(full, d),
                  ck + [f"the promise is for {long_day(d)}"])
    return R

# ---------------------------------------------------------------- identity (A and B)
def id_rows(b, t, R, pay, pay_exp, pay_ck):
    c = BASE[b]; dob, l4 = c["identity"]["date_of_birth"], c["identity"]["phone_last4"]
    y, m, d = (int(v) for v in dob.split("-"))
    w = wrong_dobs(dob, 3); wp = wrong_phones(l4, 2); right = g1.dob_words(y, m, d)
    nv = lambda r: E("not_verified", r)
    def add(sub, body, exp, ck): R.add("Identity", sub, "none", body, exp, ck, False, verified=False)
    add("dob-as-numbers", f"Says 'yes, that's me'. Gives {{his}} date of birth as numbers only, '{d:02d}, {m:02d}, {y}'. Phone digits {l4}. {pay}",
        pay_exp, [f"the date of birth is sent as {dob}"] + pay_ck)
    if d > 12:
        add("dob-month-first", f"Says 'yes, that's me'. Gives {{his}} date of birth month first, '{m:02d}, {d:02d}, {y}'. When asked to say the month as a word, says '{MONTHS[m - 1]} the {ordinal(d)}, {y}'. Phone digits {l4}. {pay}",
            pay_exp, ["the swapped answer is not counted as wrong"] + pay_ck)
    add("dob-year-later", f"Says 'speaking'. For the date of birth says only 'the {ordinal_words(d)} of {MONTHS[m - 1]}', and gives the year, {y}, when asked. Phone digits {l4}. {pay}",
        pay_exp, ["the agent asks only for the year", "the half answer is not counted as wrong"] + pay_ck)
    add("phone-two-parts", f"Says 'speaking'. Date of birth {right}. For the phone digits gives '{l4[:2]}' and waits; when asked for the rest, says '{l4[2:]}'. {pay}",
        pay_exp, [f"the two parts are joined into {l4}", "the half answer is not counted as wrong"] + pay_ck)
    add("wrong-phone-once", f"Says 'yes'. Date of birth {right}. For the phone digits first says {wp[0]}, then corrects to {l4} when asked again. {pay}",
        pay_exp, ["one wrong answer does not end the check", "the agent never says the right answer"] + pay_ck)
    add("wrong-each-once", f"Says 'yes'. First gives {{his}} date of birth as {w[1]}, then {right}. First gives the phone digits as {wp[1]}, then {l4}. {pay}",
        pay_exp, ["two wrong answers do not end the check", "the agent never says the right answer"] + pay_ck)
    add("why-needed", f"Says 'yes'. Asked for the date of birth, asks what the bank needs it for. After the explanation gives {right}, and phone digits {l4}. {pay}",
        pay_exp, ["the agent explains once that the questions protect the account"] + pay_ck)
    add("whos-calling", f"Answers the first question with 'Who's calling, please?'. After the agent answers, says 'Yes, this is {{full}}.' Passes both security questions. {pay}",
        pay_exp, ["'who's calling' is not taken as a confirmation"] + pay_ck)
    add("go-ahead", f"Answers the first question with 'Go ahead.'. When asked again, says 'Yes, it's me.' Passes both security questions. {pay}",
        pay_exp, ["'go ahead' is not taken as a confirmation"] + pay_ck)
    add("scam-at-start", f"Says 'yes'. Asks straight away whether this is a scam call. After the answer, passes both security questions. {pay}",
        pay_exp, ["the agent says the call can be verified by phoning Freya Bank's customer service number"] + pay_ck)
    add("three-wrong-dobs", f"Says 'yes'. Gives three wrong dates of birth, one after the other: {w[2]}, {w[0]}, {w[1]}. Does not know the right one.", nv("failed"),
        ["the check ends after three wrong answers", "the agent mentions the mobile app or a branch", "the debt is not stated"])
    add("wrong-across-both", f"Says 'yes'. First gives the date of birth as {w[1]}, then {right}. Then gives the phone digits as {wp[1]}, then {wp[0]}. Cannot remember the right digits.",
        nv("failed"), ["three wrong answers across both questions end the check", "the debt is not stated", "the agent mentions the mobile app or a branch"])
    add("refuses-dob", "Says 'yes'. Says {he} does not give {his} date of birth over the phone. After the agent explains why, still says no.", nv("declined"),
        ["the agent explains once that the questions protect the account", "the debt is not mentioned"])
    add("asks-purpose-twice", "Says 'yes'. Asked for the date of birth, asks 'What is this call regarding?'. After the explanation, asks again what the call is regarding instead of answering.",
        nv("declined"), ["the agent explains once that the questions protect the account", "the reason for the call is not given"])
    add("refuses-phone", f"Says 'yes'. Gives the date of birth, {right}. Will not give the phone digits, and after the agent explains why, still will not.",
        nv("declined"), ["the debt is not mentioned"])
    add("never-confirms", "Answers 'Yes? Who is it?' to the first question. To the second ask, says only 'Mm-hm, I'm listening.' Never says it is {him}.",
        E("not_reached", "unclear"), ["'who is it' and 'I'm listening' are not taken as a confirmation", "nothing about the account is said"])

# ---------------------------------------------------------------- someone else answers (A and B)
REL_M, REL_F = g1.REL_M, g1.REL_F

def slot_cat(t, now_hm, d, hm):
    if callback_ok(t, now_hm, d, hm): return "ok"
    if d != t and d != _next_bday(t + g1.dt.timedelta(days=1)):
        return "weekend" if d.weekday() >= 5 else ("holiday" if iso(d) in HOLIDAYS_A else "far")
    if hm < "10:00": return "early"
    if hm > "19:00": return "late"
    return "past"

WHY = {"weekend": "is a weekend day", "holiday": "is a bank holiday", "far": "is not today or the next business day", "early": "is before 10:00",
       "late": "is after 19:00", "past": "has already passed"}

def third_rows(b, t, now_hm, R):
    c = BASE[b]; x = pron(c); first = x["first"]
    rels = REL_M if c["variables"]["title"] == "Mr" else REL_F
    slots = [(dp, d, hp, hm) for dp, d in cb_days(t) for hp, hm in HOURS]
    if t.weekday() == 4:                                     # on a Friday a relative may also name the weekend or Monday
        for k, nm in ((1, "on Saturday"), (2, "on Sunday"), (3, "on Monday")):
            slots += [(nm, t + g1.dt.timedelta(days=k), hp, hm) for hp, hm in HOURS]
    by = collections.defaultdict(list)
    for s in slots: by[slot_cat(t, now_hm, s[1], s[3])].append(s)
    pick = lambda cat, k=0: by[cat][(h(b, iso(t), cat, k) % len(by[cat]))] if by[cat] else None
    def add(fam, sub, body, exp, ck, tags=()): R.add(fam, sub, "none", body, exp, NO3P + ck, False, tags=tags, verified=False)
    cb = lambda s: E("not_reached", "callback", callbacks=[{"date": iso(s[1]), "time": s[3]}])
    today_ok = [s for s in by["ok"] if s[1] == t]; next_ok = [s for s in by["ok"] if s[1] != t]
    if today_ok:
        s = today_ok[h(b, iso(t), "today") % len(today_ok)]
        add("Callback", "today-in-window", f"Is {first}'s {rels[0]}. Says {first} will be back {s[0]} at {s[2]}.", cb(s), [f"callback booked for {iso(s[1])} {s[3]}"])
    if next_ok:
        s = next_ok[h(b, iso(t), "next") % len(next_ok)]
        add("Callback", "next-business-day", f"Is {first}'s {rels[1]}. Asks what the call is about, then says {first} is free {s[0]} at {s[2]}.", cb(s),
            [f"callback booked for {iso(s[1])} {s[3]}", "the agent does not say what the call is about"])
        s2 = next_ok[h(b, iso(t), "pay") % len(next_ok)]
        add("Callback", "offers-to-pay", f"Is {first}'s {rels[2]}. Offers to pay whatever {first} owes, right now. Then says {first} is around {s2[0]} at {s2[2]}.", cb(s2),
            ["the agent takes no payment from the relative", f"callback booked for {iso(s2[1])} {s2[3]}"])
    good = by["ok"]
    for cat in ("early", "late", "past", "far", "weekend", "holiday"):
        s1 = pick(cat)
        if not s1 or not good: continue
        s2 = good[h(b, iso(t), cat, "g") % len(good)]
        add("Callback", f"{cat}-then-ok", f"Is {first}'s {rels[3]}. First says {first} can talk {s1[0]} at {s1[2]}. When asked for another time, says {s2[0]} at {s2[2]}.",
            cb(s2), [f"{s1[0]} at {s1[3]} {WHY[cat]} and is not booked", "the agent asks once for another time", f"callback booked for {iso(s2[1])} {s2[3]}"])
    cats = [k for k in ("early", "late", "past", "far", "weekend", "holiday") if by[k]]
    for c1, c2 in itertools.combinations(cats, 2):
        s1, s2 = pick(c1, 1), pick(c2, 2)
        for a, z, sub in ((s1, s2, f"twice-outside-{c1}-{c2}"),):
            add("Third party", sub, f"Is {first}'s {rels[4]}. First says {first} is only free {a[0]} at {a[2]}. When asked for another time, says {z[0]} at {z[2]}, and that there is no other time.",
                E("not_reached", "no_callback"), ["the agent asks once for another time", "no callback is booked", "the agent does not ask a third time"], tags=["cb-max-asks"])
    add("Third party", "working-abroad", f"Is {first}'s {rels[5]}. Says {first} is working abroad for six weeks. When asked for another time, says not before then.",
        E("not_reached", "no_callback"), ["no callback is booked"], tags=["cb-max-asks"])
    add("Third party", "night-shifts", f"Is {first}'s {rels[0]}. Says {first} works nights and can only talk after ten in the evening. When asked for another time, says after ten in the evening is the only option.",
        E("not_reached", "no_callback"), ["22:00 is outside 10:00 to 19:00", "no callback is booked"], tags=["cb-max-asks"])
    add("Third party", "number-reassigned", f"Says this number has been theirs for two years and nobody called {x['full']} uses it.", E("not_reached", "wrong_number"), [])
    add("Third party", "shop-line", f"Says this is the line of a bakery and there is nobody called {x['full']} working there.", E("not_reached", "wrong_number"), [])
    add("Third party", "passed-away", f"Is {first}'s {rels[1]}. Says quietly that {first} died a few weeks ago.", E("not_reached", "deceased"),
        ["the agent says 'I am sorry for your loss.'", "the agent asks nothing else"])

NO3P = ["nothing about the account is said"]

# ---------------------------------------------------------------- Brief B
ACT_LADDER = ("first", "second", "refuse")

def b_action(t, i, p, act, kind):
    """One product's part of a Brief B script: (sentence, promise or None, note reason or None, check, spoken value or not).
    kind is the phrasing for the values this product's part says. None if the action does not fit this product or kind."""
    pid, name, full, fl = f"p{i + 1}", PNAME[p["type"]], p["past_due"], floor_of(p)
    ok, moved, _ = date_buckets(t, p)
    dl, a1 = deadline(t, p), attempt1(t)
    pa = partial_amounts(p); mid = pa[1][0] if len(pa) > 1 else None
    bl = below_amounts(p)
    if act == "today": return f"Pays {name} in full today.", P(pid, full, iso(t)), None, f"{name}: {money(full)} today", False
    if act == "first":
        if a1 > dl: return None
        return f"For {name}, accepts the agent's first offer for it.", P(pid, full, iso(a1)), None, f"{name}: attempt 1, {money(full)} on {long_day(a1)}", False
    if act == "second":
        if not is_late(p) or a1 > dl: return None
        return (f"For {name}, turns down the agent's first offer for it and accepts the second offer for it.", P(pid, fl, iso(dl)), None,
                f"{name}: attempt 2, the floor {money(fl)} on {long_day(dl)}", False)
    if act == "refuse":
        return (f"For {name}, turns down every offer for it.", None, "refused", f"{name}: a refused note after {'two attempts' if is_late(p) else 'one attempt'}", False)
    if act == "too-much":
        return (f"For {name}, says the full amount is too much and, before the agent proposes anything, asks to pay just the minimum today.",
                P(pid, fl, iso(t)), None, f"{name}: the floor {money(fl)} today", False)
    if act == "too-much-refuse":
        return (f"For {name}, says the full amount is too much, turns down the smaller amount too and every proposal after it.", None, "refused",
                f"{name}: the floor {money(fl)} is offered, then a refused note", False)
    if act == "dispute": return f"Says {name} is not {{hers}}; {{he}} never had it.", None, "dispute", f"{name}: a dispute note", False
    if act == "paid": return f"Says {name} was paid in full yesterday evening.", None, "paid", f"{name}: a paid note", False
    if act == "dispute-the-loan":     # the only loan of the customer: "the loan" has one reading
        if p["type"] == "credit_card": return None
        return "Says 'the loan is not mine, I never took one'.", None, "dispute", f"{name}: a dispute note ('the loan' is the only loan)", False
    if act == "dispute-which":        # one of two loans: the agent must ask which
        if p["type"] == "credit_card": return None
        return (f"When the loans come up, says only 'the loan is not mine'. When the agent asks which loan, says {name}; {{he}} never had it.", None, "dispute",
                f"{name}: the agent asks which loan, then a dispute note for {name} only", False)
    if act == "date":
        d, s = first_with(ok, t, None, kind, start=2) if ok else (None, None)
        if d is None: return None
        return f"Pays {name} in full {s}.", P(pid, full, iso(d)), None, f"{name}: {money(full)} on {long_day(d)}", True
    if act == "partial":
        if not mid: return None
        s = spoken(t, mid, None, kind, fl, full - 1)
        if s is None: return None
        return f"For {name}, says the full amount is not possible but can pay {s}.", P(pid, mid, iso(t)), None, f"{name}: {money(mid)} today is accepted", True
    if act == "below":
        if not bl: return None
        s = spoken(t, bl[0], None, kind, 1, fl - 1)
        if s is None: return None
        return (f"For {name}, says only {s} is possible; takes the amount the agent proposes instead, today.", P(pid, fl, iso(t)), None,
                f"{name}: {money(bl[0])} is not recorded, the floor {money(fl)} is", True)
    if act == "counter":
        if not mid or a1 > dl: return None
        d, s = first_with([d for d in ok if d != a1], t, mid, kind, start=1)
        if d is None: return None
        return (f"For {name}, answers the agent's first proposal with 'No, but I can do {s}.'", P(pid, mid, iso(d)), None,
                f"{name}: the counter-offer {money(mid)} on {long_day(d)} is recorded", True)
    raise ValueError(act)

ACTIONS = ["today", "first", "second", "refuse", "too-much", "too-much-refuse", "dispute", "paid", "date", "partial", "below", "counter", "dispute-the-loan", "dispute-which"]
SPEAKS = {"date", "partial", "below", "counter"}

def refusals(p, act):
    """How many proposals the customer turns down for one product on the ladder."""
    return {"first": 0, "second": 1, "refuse": 2 if is_late(p) else 1}.get(act)

def combo_family(acts):
    s = set(acts)
    if s == {"today"}: return "Pay full"
    if s <= set(ACT_LADDER):
        return "Refusal" if s == {"refuse"} else "Persuasion"
    other = [a for a in acts if a != "today"]
    if len(set(other)) == 1 and len(other) >= 1:
        o = other[0]
        fam = {"partial": "Partial", "below": "Below floor", "too-much": "Below floor", "counter": "Counter-offer", "dispute": "Dispute", "dispute-the-loan": "Dispute",
               "dispute-which": "Dispute", "paid": "Already paid", "date": "Dates", "refuse": "Refusal", "too-much-refuse": "Refusal"}.get(o)
        if fam: return fam
    return "Multi-product mix"

def b_combo(b, t, acts, kind):
    """One Brief B case where every product gets its own action (rule B8); expected record computed per product."""
    c = BASE[b]; ps = c["products"]; ids = [f"p{i + 1}" for i in range(len(ps))]
    if acts.count("dispute-which") > 1 or ("dispute-which" in acts and sum(p["type"] != "credit_card" for p in ps) < 2): return None
    if "dispute-the-loan" in acts and sum(p["type"] != "credit_card" for p in ps) != 1: return None
    if "dispute-which" in acts and "dispute" in [a for a, p in zip(acts, ps) if p["type"] != "credit_card"]: return None
    speakers = [i for i, a in enumerate(acts) if a in SPEAKS]
    if kind == "none":
        if speakers: return None
        kinds = [None] * len(ps)
    else:
        if not speakers: return None
        if kind == "words": kinds = ["words" if a in SPEAKS else None for a in acts]
        elif kind == "digits": kinds = ["digits" if a in SPEAKS else None for a in acts]
        else: kinds = [kind if i == speakers[0] else ("digits" if a in SPEAKS else None) for i, a in enumerate(acts)]
    parts = []
    for i, (p, a) in enumerate(zip(ps, acts)):
        r = b_action(t, i, p, a, kinds[i])
        if r is None: return None
        parts.append(r)
    prs = [r[1] for r in parts if r[1]]
    notes = [N(ids[i], r[2]) for i, r in enumerate(parts) if r[2]]
    if prs: exp = E("promise", None, prs, notes)
    elif all(n["reason"] == "paid" for n in notes): exp = E("paid", None, notes=notes)
    else: exp = E("no_promise", next(n["reason"] for n in notes if n["reason"] != "paid"), notes=notes)
    lead = "" if set(acts) <= {"today", "dispute", "paid", "dispute-the-loan", "dispute-which"} else ("Says {he} cannot pay all of them today. " if any(a in ACT_LADDER for a in acts) else "Cannot pay everything today. ")
    if set(acts) == {"today"}: body = f"Pays {'both' if len(ps) == 2 else 'all three'} in full today. {yes_b(b, 'all')}"
    else: body = lead + " ".join(r[0] for r in parts) + (f" {yes_b(b, *acts)}" if prs else "")
    ck = [r[3] for r in parts] + (["one read-back for all promises"] if len(prs) > 1 else [])
    if notes and not prs: ck.append(f"closed as {exp['outcome']}" + (f" / {exp['reason']}" if exp["reason"] else ""))
    if any(a in ACT_LADDER for a in acts): ck.append("persuasion attempts are counted per product; each product starts at attempt 1")
    tags = set()
    lad = [refusals(p, a) for p, a in zip(ps, acts) if a in ACT_LADDER]
    if "refuse" in acts and len(set(lad)) >= 2: tags.add("ladder")
    if "counter" in acts: tags |= {"counter", "no-but"}
    if "too-much" in acts or "too-much-refuse" in acts: tags.add("too-much")
    if "dispute" in acts: tags.add("dispute-named")
    if "dispute-which" in acts: tags.add("dispute-which")
    if "dispute-the-loan" in acts: tags.add("dispute-the-loan")
    if kind == "correction": tags.add("correction")
    if kind in ("words", "mixed") and any(a in ("partial", "below", "counter") for a in acts): tags.add("amount-words")
    return {"family": combo_family(acts), "sub": "per-product", "phr": kind, "body": body, "expected": exp, "checks": ck, "tags": tags, "actions": tuple(acts)}

def b_rows(b, t, combo_filter):
    c = BASE[b]; ps = c["products"]; x = pron(c); ids = [f"p{i + 1}" for i in range(len(ps))]; T = iso(t); n = len(ps)
    them = "both" if n == 2 else "all three"
    R = Rows(b)
    def add(fam, sub, phr, body, exp, checks, style_ok=True, tags=(), acts=None, verified=True):
        R.add(fam, sub, phr, body, exp, checks, style_ok, tags, acts if acts is not None else (sub,) * n, verified)
    # per-product combinations (rule B8)
    for acts in itertools.product(ACTIONS, repeat=n):
        if not combo_filter(acts): continue
        for kind in ("none",) + KINDS:
            if h(b, T, acts, kind) % 3: continue                 # one call day in three per kind: spreads the combinations
            r = b_combo(b, t, list(acts), kind)
            if r: R.add(r["family"], r["sub"], r["phr"], r["body"], r["expected"], r["checks"], True, r["tags"], r["actions"])
    full_all = E("promise", None, [P(i, p["past_due"], T) for i, p in zip(ids, ps)])
    # one day for everything (rule B6)
    days = sorted({d for p in ps for d in sum(date_buckets(t, p), [])})
    for i, k in enumerate(("digits", "words", "relative", "correction")):
        for d in days[2 + 3 * i:] + days[:2 + 3 * i]:
            s = spoken(t, None, d, k)
            if s is None: continue
            got = [_promise_day(t, d, p["days_late"]) for p in ps]
            cut = [PNAME[p["type"]] for p, e in zip(ps, got) if e != d]
            add("Dates", "one-day-for-all" + ("-cut" if any(e < d for e in got) else ""), k,
                f"Cannot pay today. Wants to pay {them} in full {s}. Accepts any day the agent says it must use instead for a product. {yes_b(b, 'od', k)}",
                E("promise", None, [P(i2, p["past_due"], iso(e)) for i2, p, e in zip(ids, ps, got)]),
                [", ".join(f"{PNAME[p['type']]} on {long_day(e)}" for p, e in zip(ps, got))] + ([f"{' and '.join(cut)}: the read-back says why the day differs from {long_day(d)}"] if cut else []),
                tags=["correction"] if k == "correction" else (), acts=("one-day",) * n)
            break
    d = _weekday_next_week(t, 3)
    got = [_promise_day(t, d, p["days_late"]) for p in ps]
    add("Dates", "vague-end-of-next-week", "relative", f"Cannot pay today. For all of them says 'towards the end of next week'. When asked for the exact day, says 'the Thursday'. Accepts any day the agent must use instead for a product. {yes_b(b, 'vg')}",
        E("promise", None, [P(i2, p["past_due"], iso(e)) for i2, p, e in zip(ids, ps, got)]), ["the agent asks once for the exact day"] + [f"{PNAME[p['type']]} on {long_day(e)}" for p, e in zip(ps, got)],
        tags=["vague"], acts=("one-day",) * n)
    add("Pay full", "asks-total-first", "none", f"Asks what the total is, then says {{he}} will pay {them} in full today. {yes_b(b, 'tot')}", full_all,
        ["all products stated before asking", "one read-back for all promises"], acts=("today",) * n)
    # hardship (rule 32) for the customer as a whole
    sym = ["one sentence of sympathy", "no persuasion attempt"]
    add("Hardship", "lost-job-decline", "none", "Says 'I can't pay, I lost my job.' Turns down the offer of the support team.", E("no_promise", "hardship", notes=[N(i, "hardship") for i in ids]),
        sym + ["a hardship note for each product", "closed as hardship, not as a refusal"], False, tags=["hardship-job"], acts=("hardship",) * n)
    add("Hardship", "lost-job-accept", "none", "Says 'I can't pay, I lost my job.' Accepts the support team.", E("transferred", "support", transfers=TR("support")),
        sym + ["transfer to support", "no notes at all"], False, tags=["hardship-job"], acts=("transfer",) * n)
    add("Hardship", "bereavement-decline", "none", "Says {his} mother died recently and {he} cannot face any of this now. Says no to the support team.",
        E("no_promise", "hardship", notes=[N(i, "hardship") for i in ids]), sym + ["a hardship note for each product"], False, acts=("hardship",) * n)
    p0 = ps[0]; ok0 = date_buckets(t, p0)[0]
    if len(ok0) > 1:
        d0 = ok0[1]
        add("Hardship", "own-offer-first-product", "digits", f"Says {{he}} lost {{his}} job last month. When offered the support team, says no thanks and, in the same answer, says {{he}} can still pay {money(floor_of(p0))} {day_digits(d0)} on {PNAME[p0['type']]}. Nothing on the others. {yes_b(b, 'hs')}",
            E("promise", None, [P("p1", floor_of(p0), iso(d0))], [N(i, "hardship") for i in ids[1:]]),
            sym + [f"{PNAME[p0['type']]}: {money(floor_of(p0))} on {long_day(d0)} is recorded", "a hardship note for each other product"], False,
            tags=["hardship-job"], acts=("own-offer",) + ("hardship",) * (n - 1))
    # transfers (B10)
    add("Transfer", "lower-instalments-all", "none", "As soon as the debts are stated, asks for lower monthly instalments on all of them. Agrees to be put through.",
        E("transferred", "restructuring", transfers=TR("restructuring")), ["no plan is described", "no notes"], False, acts=("transfer",) * n)
    add("Transfer", "person-insists", "none", "After hearing the debts, asks for a real person. When the agent asks whether it can help first, says 'no, a person'.",
        E("transferred", "human", transfers=TR("general")), ["the agent asks once whether it can help first", "no notes at all"], False, acts=("transfer",) * n)
    add("Transfer", "person-before-checks", "none", "Says 'that's me' and asks right away for a person, before any security question. When the agent asks whether it can help first, still wants a person.",
        E("transferred", "human", transfers=TR("general")), ["the agent asks once whether it can help first", "nothing about the account is said"], False, acts=("-",) * n, verified=False)
    add("Mid-call change", "plan-after-first-agreed", "none", f"Says {{he}} can pay {PNAME[ps[0]['type']]} in full today. Then, before any read-back, asks for a new plan with lower instalments for the rest. Agrees to be put through.",
        E("transferred", "restructuring", transfers=TR("restructuring")), ["no plan is described", "the agreed but unrecorded promise is not recorded", "no notes"], False,
        acts=("today",) + ("transfer",) * (n - 1))
    add("Mid-call change", "recorded-then-person", "none", f"Pays {them} in full today and confirms. Once they are recorded, asks for a human. When the agent asks whether it can help first, insists.",
        E("transferred", "human", full_all["promises"], transfers=TR("general")), ["the recorded promises stay", "no notes", "closed as transferred / human"], False, acts=("today",) * n)
    add("Mid-call change", "recorded-move-one", "none", f"Pays {them} in full today and confirms. Once they are recorded, asks to move {PNAME[ps[-1]['type']]} to next month.", full_all,
        ["the agent says a recorded promise can be changed in the mobile app", "no new or changed promise is recorded"], acts=("today",) * n)
    ok_last = date_buckets(t, ps[-1])[0]
    rd, rs = first_with(ok_last, t, None, "relative", start=2) if ok_last else (None, None)
    if rd:
        add("Mid-call change", "readback-day-one", "relative", f"Agrees to pay {them} in full today. At the read-back says '{PNAME[ps[-1]['type']]} {rs} instead, please'. Confirms the new read-back once.",
            E("promise", None, full_all["promises"][:-1] + [P(ids[-1], ps[-1]["past_due"], iso(rd))]),
            [f"the agent reads back again with {PNAME[ps[-1]['type']]} on {long_day(rd)}", "the other products stay today"], tags=["correction"], acts=("today",) * (n - 1) + ("date",))
    # does not want to talk (rule 45)
    nt = [N(i, "refused") for i in ids]
    add("No talk", "says-it-twice", "none", "After the debts are stated, says 'I don't want to talk about any of this.' When the agent goes on, says it again.",
        E("no_promise", "refused", notes=nt), ["the call ends after the second time", "a refused note for each product"], False, tags=["no-talk"], acts=("no-talk",) * n)
    add("No talk", "other-words", "none", "After the debts are stated, says 'not interested, stop calling me.' When the agent goes on, says 'I already told you, I'm done with this call.'",
        E("no_promise", "refused", notes=nt), ["the second refusal to talk closes the call", "a refused note for each product"], False, tags=["no-talk", "no-talk-other"], acts=("no-talk",) * n)
    add("No talk", "before-checks", "none", "Says 'yes' to {his} name. Asked for the date of birth, says 'I'm not doing this.' After the agent explains, says 'no, I'm not doing this.' Never gives a security answer.",
        E("not_verified", "declined"), ["the agent explains once that the questions protect the account", "the debts are not mentioned"], False, tags=["no-talk", "no-talk-other"], acts=("-",) * n, verified=False)
    add("No talk", "once-then-pays", "none", f"After the debts are stated, says once 'I'd rather not get into this.' When the agent goes on, changes {{his}} mind and pays {them} in full today. {yes_b(b, 'nt')}",
        full_all, ["one refusal to talk does not end the call", "one read-back for all promises"], False, tags=["no-talk"], acts=("today",) * n)
    # asks back
    for k, q, ck in (("interest", "asks how much interest these are costing", ["the agent says it does not have that information"]),
                     ("scam", "asks how {he} can be sure the call is genuine", ["the agent says the call can be verified by phoning Freya Bank's customer service number"]),
                     ("fees", "asks whether the late fees can be waived on all of them", ["no discount or waiver is offered"])):
        add("Asks back", f"{k}-all-today", "none", f"After hearing the debts, {q}. After the answer, pays {them} in full today. {yes_b(b, 'q', k)}", full_all, ck + ["one read-back for all promises"], acts=("today",) * n)
    them_pay = f"Pays {them} in full today. {yes_b(b, 'id')}"
    id_rows(b, t, R, them_pay, full_all, ["one read-back for all promises"])
    return R

# ---------------------------------------------------------------- candidates
def combo_ok(acts):
    """Brief B combinations worth a case: not all products the same (those are the whole-customer rows), at most one
    'the loan' dispute, and a ladder or a v4 action in it so the set leans on what the code now decides."""
    if len(set(acts)) == 1 and acts[0] not in ("today", "refuse", "first", "paid", "dispute", "too-much-refuse"): return False
    if len(set(acts)) == 1: return True
    return True

def candidates(seed):
    """Every candidate case as a light dict, in a fixed order."""
    g1.self_check()
    out = []
    for di, day in enumerate(DAYS):
        t = _d(day)
        for b in A_BASES + B_BASES:
            hm = TIMES[(di * 2 + int(b[4:]) + 1) % len(TIMES)]
            c = BASE[b]
            if len(c["products"]) == 1:
                rows = a_rows(b, t)
                id_rows(b, t, rows, f"Pays the whole amount today. {yes(b, 'id')}", prom(c["products"][0]["past_due"], t), ["the full amount is recorded for today"])
            else:
                rows = b_rows(b, t, combo_ok)
            for r in rows: out.append(dict(r, base=b, day=day, now=f"{day}T{hm}"))
            for cb_hm in CB_TIMES:
                rows = Rows(b); third_rows(b, t, cb_hm, rows)
                for r in rows: out.append(dict(r, base=b, day=day, now=f"{day}T{cb_hm}"))
    return out

def group_of(b): return "A" if len(BASE[b]["products"]) == 1 else f"B{len(BASE[b]['products'])}"
def core(r): return ("A" if len(BASE[r["base"]]["products"]) == 1 else "B", r["family"], r["sub"], r["actions"], r["phr"])

def load_bank_scripts():
    """Scripts of the existing banks: rebuilt from tools/edge_cases.py, plus any bank files found on disk."""
    import edge_cases as ec
    scripts = set()
    for rows in (None, ec.FOCUS, ec.EDGE + ec.generated() + ec.FOCUS + ec.extra()):
        scripts |= {c["script"] for c in ec.build(rows)}
    scripts |= {c["script"] for c in ec.build_days()}
    for d in (ROOT / "results", pathlib.Path("/Users/ulas/Desktop/Freya/build-the-builder-v3/build-the-builder/results")):
        for name in ("edge-cases-250.json", "edge-cases.json", "edge-cases-days.json", "edge-cases-focus.json", "cases-1000.json"):
            f = d / name
            if f.exists():
                try: scripts |= {c["script"] for c in json.loads(f.read_text())}
                except (ValueError, KeyError, TypeError): pass
    return scripts

def norm(s): return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

FAMILY_WEIGHT = {"Multi-product mix": 8, "Dates": 1.6, "Counter-offer": 1.5, "Identity": 1.2, "Mid-call change": 1.4, "Callback": 0.8, "Third party": 0.6,
                 "Persuasion": 2, "Asks back": 0.8}

def required(group):
    """(name, predicate on a candidate) that must be met at least once in the group."""
    fams = FAMILIES_B if group != "A" else FAMILIES
    req = [(f"family {f}", (lambda f: lambda r: r["family"] == f)(f)) for f in fams]
    req += [(f"phrasing {k}", (lambda k: lambda r: r["phr"] == k)(k)) for k in KINDS]
    tags = V4_TAGS_A if group == "A" else V4_TAGS_B | (V4_TAGS_B3 if group == "B3" else V4_TAGS_B2)
    req += [(f"tag {k}", (lambda k: lambda r: k in r["tags"])(k)) for k in sorted(tags)]
    if group != "A":
        req.append(("ladder refusal at step 1 and step 2 in one case", lambda r: "ladder" in r["tags"] and r["actions"].count("refuse") >= 2))
    if group == "B3":
        req.append(("ladder with an accepted second proposal", lambda r: "ladder" in r["tags"] and "second" in r["actions"]))
    return req

def pick_cores(cands, n, group, rng):
    by_core = collections.OrderedDict()
    for r in cands: by_core.setdefault(core(r), []).append(r)
    cores = list(by_core); rng.shuffle(cores)
    chosen, seen = [], set()
    def take(k):
        if k not in seen: seen.add(k); chosen.append(k)
    for name, pred in required(group):
        if any(pred(by_core[k][0]) for k in chosen): continue
        hits = [k for k in cores if any(pred(r) for r in by_core[k])]
        if not hits: raise SystemExit(f"{group}: no candidate for {name}")
        take(hits[0])
    by_fam = collections.defaultdict(list)
    for k in cores:
        if k not in seen: by_fam[k[1]].append(k)
    fams = sorted(by_fam); cnt = collections.Counter(k[1] for k in chosen)
    while len(chosen) < n:
        room = [f for f in fams if by_fam[f]]
        if not room: raise SystemExit(f"{group}: only {len(chosen)} distinct scenarios, {n} needed")
        f = min(room, key=lambda f: (cnt[f] / FAMILY_WEIGHT.get(f, 1.0), f))
        take(by_fam[f].pop(0)); cnt[f] += 1
    return [by_core[k] for k in chosen[:n]], required(group)

def place(groups_of_cands, rng, banned, used):
    """One instance per chosen scenario: spread over call days, customers and times; never a script already used."""
    day_n, base_n, time_n, out = collections.Counter(), collections.Counter(), collections.Counter(), []
    order = list(range(len(groups_of_cands))); rng.shuffle(order)
    order.sort(key=lambda i: len(groups_of_cands[i]))                  # the scenarios with few possible instances first
    for i in order:
        opts = [r for r in groups_of_cands[i] if r["script"] not in banned and norm(r["script"]) not in used]
        if not opts: raise SystemExit(f"no fresh instance for {core(groups_of_cands[i][0])}")
        tie = {id(r): rng.random() for r in opts}
        r = min(opts, key=lambda r: (day_n[r["day"]], base_n[r["base"]], time_n[r["now"][11:]], tie[id(r)]))
        day_n[r["day"]] += 1; base_n[r["base"]] += 1; time_n[r["now"][11:]] += 1; used.add(norm(r["script"]))
        out.append(r)
    return out

THIRTY = re.compile(r"\b30\b|30th|thirtieth")

def assign_styles(rows, rng):
    """[None] + run_cases STYLES in turn over the rows whose wording is free; never talkative with a 30th, never the
    'corrects' style on a row whose script already has a correction."""
    k = 0
    opts = [None] + STYLES
    for r in rows:
        r["style"] = None
        if not r["style_ok"]: continue
        for j in range(len(opts)):
            s = opts[(k + j) % len(opts)]
            if s == "talkative" and (THIRTY.search(r["script"]) or any(p["date"].endswith("-30") for p in r["expected"]["promises"])): continue
            if s == "corrects" and r["phr"] == "correction": continue
            r["style"] = s; break
        k += 1
    return rows

def build_case(r):
    c = copy.deepcopy(BASE[r["base"]])
    t = _d(r["day"])
    c["variables"] = dict(c["variables"], now=r["now"])
    fam = r["family"]
    c.update({"id": None, "base_id": r["base"], "brief": "B" if len(c["products"]) > 1 else "A", "family": fam, "level": "gen250",
              "title": f"{fam}: {r['sub']}" + (f" {'+'.join(r['actions'])}" if r["sub"] == "per-product" else "") + f" [{r['phr']}] ({WD[t.weekday()][:3]} {r['day']})",
              "script": r["script"], "checks": [f"the call is on {WD[t.weekday()]} {t.day} {MONTHS[t.month - 1]} {t.year}"] + r["checks"],
              "style": r["style"], "third_party": fam in ("Callback", "Third party"), "expected": copy.deepcopy(r["expected"])})
    if r["base"] in FAILS_FIRST: assert not any("exactly once" in x for x in c["checks"])
    return c

def generate_with_meta(n=250, seed=25, share_a=0.6, share_b2=0.4):
    """(cases, meta): meta[i] is the scenario signature of cases[i] and its v4 tags."""
    rng = random.Random(seed)
    banned = load_bank_scripts()
    cands = [r for r in candidates(seed) if r["script"] not in banned]
    na = round(n * share_a); nb2 = round((n - na) * share_b2); nb3 = n - na - nb2
    rows, used = [], set()                                              # scripts already placed, across all groups
    for group, k in (("A", na), ("B2", nb2), ("B3", nb3)):
        mine = [r for r in cands if group_of(r["base"]) == group]
        picked, _ = pick_cores(mine, k, group, rng)
        rows += place(picked, rng, banned, used)
    rows.sort(key=lambda r: (group_of(r["base"]), FAMILIES_B.index(r["family"]), r["sub"], r["actions"], r["phr"], r["now"]))
    assign_styles(rows, rng)
    cases, meta = [], []
    for i, r in enumerate(rows):
        c = build_case(r)
        slug = re.sub(r"[^a-z0-9]+", "-", r["family"].lower()).strip("-")
        c["id"] = f"N{i + 1:03d}-{c['brief']}-{r['base'][4:]}-{r['day'][5:].replace('-', '')}-{slug}-{r['sub']}"[:90]
        cases.append(c)
        meta.append({"signature": [c["brief"], r["family"], r["sub"], list(r["actions"]), r["day"], r["style"], r["phr"]], "tags": sorted(r["tags"])})
    return cases, meta

def generate(n=250, seed=25): return generate_with_meta(n, seed)[0]

def signature(m): return json.dumps(m["signature"])

def coverage(cases, meta):
    lines = [g1.summary(cases)]
    def table(title, rows):
        cnt = collections.Counter(rows)
        lines.append(f"\n{title}")
        for k, v in sorted(cnt.items(), key=lambda kv: str(kv[0])): lines.append(f"  {str(k):<40} {v:>5}")
    table("family x brief", [f"{c['family']} / {c['brief']}{len(c['products'])}" for c in cases])
    table("phrasing kind (brief)", [f"{m['signature'][6]} ({c['brief']})" for c, m in zip(cases, meta)])
    table("v4 stress tag (brief)", [f"{t} ({c['brief']})" for c, m in zip(cases, meta) for t in m["tags"]])
    lines.append(f"\n{len({signature(m) for m in meta})} distinct signatures for {len(cases)} cases")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=250); ap.add_argument("--seed", type=int, default=25)
    ap.add_argument("--out", default=str(ROOT / "results" / "cases-250-new.json"))
    a = ap.parse_args()
    cases, meta = generate_with_meta(a.n, a.seed)
    out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(cases, open(out, "w"), indent=1)
    print(f"{len(cases)} cases written to {out}")
    print(coverage(cases, meta))

if __name__ == "__main__": main()
