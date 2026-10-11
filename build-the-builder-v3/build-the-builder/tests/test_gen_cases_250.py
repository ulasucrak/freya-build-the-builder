"""Offline tests for tools/gen_cases_250.py: run_cases format, 250 distinct scenario signatures, no script shared with
the existing banks, coverage, the expected bank records re-checked with a second small calendar written here, and
determinism.

    cd build-the-builder && python -m unittest tests.test_gen_cases_250
"""
import datetime as dt
import json
import pathlib
import re
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT)); sys.path.insert(0, str(KIT / "tools"))

import gen_cases_250 as g  # noqa: E402
import gen_cases_1000 as g1  # noqa: E402
from run_cases import persona_for, STYLES  # noqa: E402

BASE = {c["id"]: c for c in json.load(open(KIT / "public-test-set.json"))}
HOL = {"2026-10-26", "2026-11-11", "2026-11-26", "2026-12-25", "2027-01-01"}    # Brief A section 7
OUTCOMES = {"promise": {None}, "paid": {None}, "transferred": {"restructuring", "human", "support"},
            "no_promise": {"refused", "hardship", "dispute", "service_error"}, "not_verified": {"failed", "declined"},
            "not_reached": {"callback", "no_callback", "wrong_number", "deceased", "unclear"}}
QUEUE = {"restructuring": "restructuring", "human": "general", "support": "support"}
KINDS = {"digits", "words", "mixed", "relative", "correction"}
MAIN_RESULTS = pathlib.Path("/Users/ulas/Desktop/Freya/build-the-builder-v3/build-the-builder/results")


def bday(d): return d.weekday() < 5 and d.isoformat() not in HOL


def nth_bday_after(d, n):
    while n:
        d += dt.timedelta(days=1)
        if bday(d): n -= 1
    return d


def floor_of(p): return p["minimum_payment"] if p["type"] == "credit_card" else p["instalment_amount"]
def D(s): return dt.date.fromisoformat(s)


CASES, META = g.generate_with_meta(250, 25)


class Format(unittest.TestCase):
    def test_fields_and_persona(self):
        need = {"id", "brief", "family", "level", "title", "variables", "identity", "products", "script", "checks", "style", "base_id", "third_party", "expected"}
        for c in CASES:
            self.assertTrue(need <= set(c), c["id"])
            self.assertEqual(set(c) - need, set(BASE[c["base_id"]]) - need, c["id"])          # nothing added beyond the base customer's own keys
            self.assertFalse([k for k in c if k.startswith("_")], c["id"])
            b = BASE[c["base_id"]]
            self.assertEqual(c["products"], b["products"], c["id"])
            self.assertEqual(c["identity"], b["identity"])
            self.assertEqual({k: v for k, v in c["variables"].items() if k != "now"}, {k: v for k, v in b["variables"].items() if k != "now"})
            dt.datetime.strptime(c["variables"]["now"], "%Y-%m-%dT%H:%M")
            self.assertEqual(c["brief"], "B" if len(c["products"]) > 1 else "A")
            self.assertTrue(c["style"] is None or c["style"] in STYLES, c["id"])
            self.assertTrue(c["checks"] and all(isinstance(x, str) and x for x in c["checks"]))
            self.assertEqual(set(c["expected"]), {"outcome", "reason", "promises", "notes", "callbacks", "transfers"})
            for ch in "{}": self.assertNotIn(ch, c["script"], c["id"])
            per = persona_for(c, {"identity": c["identity"]})
            self.assertIn(c["script"], per["prompt"])
            self.assertEqual(c["third_party"], c["family"] in ("Callback", "Third party"))
            if c["third_party"]: self.assertIn("You are NOT", per["prompt"])
            else: self.assertNotIn("You are NOT", per["prompt"])
            json.dumps(c)

    def test_same_shape_as_the_1000_set(self):
        one = g1.generate(30, 7)[0]
        self.assertEqual(set(CASES[0]) - {"faults", "expected_notes"}, set(one) - {"faults", "expected_notes"})
        self.assertEqual(set(CASES[0]["expected"]), set(one["expected"]))

    def test_ids(self):
        self.assertEqual(len({c["id"] for c in CASES}), len(CASES))
        self.assertTrue(all(len(c["id"]) <= 90 for c in CASES))


class Distinct(unittest.TestCase):
    def test_250_unique_signatures(self):
        self.assertEqual(len(CASES), 250)
        sigs = [g.signature(m) for m in META]
        self.assertEqual(len(set(sigs)), 250)
        for c, m in zip(CASES, META):
            brief, fam, sub, acts, day, style, phr = m["signature"]
            self.assertEqual((brief, fam, day, style), (c["brief"], c["family"], c["variables"]["now"][:10], c["style"]), c["id"])
            self.assertIn(phr, KINDS | {"none"})
            self.assertEqual(len(acts), 0 if brief == "A" else len(c["products"]), c["id"])

    def test_even_without_day_and_style_every_scenario_differs(self):
        cores = [json.dumps([m["signature"][i] for i in (0, 1, 2, 3, 6)]) for m in META]
        self.assertEqual(len(set(cores)), 250)

    def test_scripts_unique(self):
        self.assertEqual(len({g.norm(c["script"]) for c in CASES}), 250)

    def test_mix(self):
        a = sum(c["brief"] == "A" for c in CASES)
        two = sum(len(c["products"]) == 2 for c in CASES); three = sum(len(c["products"]) == 3 for c in CASES)
        self.assertEqual((a, two, three), (150, 40, 60))


class NewScripts(unittest.TestCase):
    """No script repeats one of the existing banks (rebuilt from edge_cases.py, plus the files on disk) or the 1000 set."""

    @classmethod
    def setUpClass(cls):
        import edge_cases as ec
        cls.old = set()
        for rows in (None, ec.FOCUS, ec.EDGE + ec.generated() + ec.FOCUS + ec.extra()):
            cls.old |= {c["script"] for c in ec.build(rows)}
        cls.old |= {c["script"] for c in ec.build_days()}
        cls.files = 0
        for d in (KIT / "results", MAIN_RESULTS):
            for name in ("edge-cases-250.json", "edge-cases.json", "edge-cases-days.json", "edge-cases-focus.json"):
                if (d / name).exists():
                    cls.old |= {c["script"] for c in json.loads((d / name).read_text())}; cls.files += 1
        cls.old |= {c["script"] for c in g1.generate(1000, 7)}

    def test_no_overlap(self):
        self.assertGreater(len(self.old), 600)
        old_norm = {g.norm(s) for s in self.old}
        for c in CASES:
            self.assertNotIn(c["script"], self.old, c["id"])
            self.assertNotIn(g.norm(c["script"]), old_norm, c["id"])

    def test_generator_bans_the_same_scripts(self):
        self.assertTrue(set(self.old) - {c["script"] for c in g1.generate(1000, 7)} <= g.load_bank_scripts())


class Coverage(unittest.TestCase):
    def test_family_by_brief(self):
        for grp, fams in (("A", g.FAMILIES), ("B2", g.FAMILIES_B), ("B3", g.FAMILIES_B)):
            got = {c["family"] for c in CASES if g.group_of(c["base_id"]) == grp}
            self.assertEqual(got, set(fams), grp)

    def test_days_styles_kinds(self):
        days = {c["variables"]["now"][:10] for c in CASES}
        self.assertEqual(days, set(g.DAYS))
        self.assertTrue(set(g1.CALL_DAYS) <= days)
        self.assertEqual({c["style"] for c in CASES}, {None} | set(STYLES))
        for brief in "AB":
            self.assertEqual({m["signature"][6] for c, m in zip(CASES, META) if c["brief"] == brief}, KINDS | {"none"}, brief)
        self.assertEqual({c["base_id"] for c in CASES}, set(BASE))

    def test_v4_stress(self):
        for grp, need in (("A", g.V4_TAGS_A), ("B2", g.V4_TAGS_B | g.V4_TAGS_B2), ("B3", g.V4_TAGS_B | g.V4_TAGS_B3)):
            got = {t for c, m in zip(CASES, META) if g.group_of(c["base_id"]) == grp for t in m["tags"]}
            self.assertTrue(need <= got, (grp, need - got))
        scripts = " ".join(c["script"] for c in CASES)
        for words in ("'No, but I can do", "I can't pay, I lost my job", "the loan is not mine", "When the agent asks which loan",
                      "towards the end of next week", "and that there is no other time", "sorry, I mean", "leave me alone"):
            self.assertIn(words, scripts)
        lad = [m for m in META if "ladder" in m["tags"]]
        self.assertTrue(any(m["signature"][3].count("refuse") >= 2 for m in lad))
        self.assertTrue(any("second" in m["signature"][3] and "refuse" in m["signature"][3] for m in lad))

    def test_outcomes(self):
        for o, reasons in OUTCOMES.items():
            got = {c["expected"]["reason"] for c in CASES if c["expected"]["outcome"] == o}
            self.assertEqual(got, reasons - {"service_error"}, o)

    def test_coverage_table_prints(self):
        text = g.coverage(CASES, META)
        self.assertIn("family x brief", text); self.assertIn("250 distinct signatures", text)


class Expected(unittest.TestCase):
    """The bank records, re-checked rule by rule."""

    def test_outcome_table(self):
        for c in CASES:
            e = c["expected"]
            self.assertIn(e["reason"], OUTCOMES[e["outcome"]], c["id"])
            self.assertEqual([t["queue"] for t in e["transfers"]], [QUEUE[e["reason"]]] if e["outcome"] == "transferred" else [], c["id"])
            if e["outcome"] == "promise": self.assertTrue(e["promises"], c["id"])
            elif e["outcome"] != "transferred": self.assertFalse(e["promises"], c["id"])
            self.assertEqual(bool(e["callbacks"]), e["reason"] == "callback", c["id"])
            if e["outcome"] in ("not_reached", "not_verified", "transferred") or c["brief"] == "A": self.assertFalse(e["notes"], c["id"])
            if e["outcome"] == "not_reached" and e["reason"] != "unclear": self.assertTrue(c["third_party"], c["id"])

    def test_promises_inside_floor_and_deadline(self):
        n = 0
        for c in CASES:
            t = D(c["variables"]["now"][:10])
            ids = {f"p{i + 1}": p for i, p in enumerate(c["products"])}
            seen = set()
            for pr in c["expected"]["promises"]:
                self.assertIn(pr["product_id"], ids, c["id"]); self.assertNotIn(pr["product_id"], seen, c["id"]); seen.add(pr["product_id"])
                p = ids[pr["product_id"]]
                self.assertTrue(floor_of(p) <= pr["amount"] <= p["past_due"], (c["id"], pr))
                d = D(pr["date"])
                self.assertTrue(t <= d <= nth_bday_after(t, 12 if p["days_late"] <= 7 else 7) and bday(d), (c["id"], pr))
                n += 1
        self.assertGreater(n, 200)

    def test_brief_b_every_product_settled(self):
        for c in CASES:
            e = c["expected"]
            if c["brief"] != "B" or e["outcome"] in ("transferred", "not_reached", "not_verified"): continue
            got = sorted([p["product_id"] for p in e["promises"]] + [n["product_id"] for n in e["notes"]])
            self.assertEqual(got, [f"p{i + 1}" for i in range(len(c["products"]))], c["id"])
            if not e["promises"]:
                notes = sorted(e["notes"], key=lambda n: n["product_id"])
                if all(n["reason"] == "paid" for n in notes): self.assertEqual((e["outcome"], e["reason"]), ("paid", None), c["id"])
                else: self.assertEqual((e["outcome"], e["reason"]), ("no_promise", next(n["reason"] for n in notes if n["reason"] != "paid")), c["id"])

    def test_callbacks_in_window(self):
        for c in CASES:
            now = c["variables"]["now"]; t = D(now[:10])
            nxt = t + dt.timedelta(days=1)
            while not bday(nxt): nxt += dt.timedelta(days=1)
            for cb in c["expected"]["callbacks"]:
                self.assertTrue("10:00" <= cb["time"] <= "19:00", c["id"])
                self.assertIn(cb["date"], (t.isoformat(), nxt.isoformat()), c["id"])
                if cb["date"] == t.isoformat(): self.assertGreater(cb["time"], now[11:], c["id"])

    def test_ladder_records(self):
        """Brief B ladder: an accepted first offer is the full amount on attempt 1's day, an accepted second offer the
        floor on the product's deadline (late band only), a refusal a refused note."""
        n = 0
        for c, m in zip(CASES, META):
            acts = m["signature"][3]
            if c["brief"] != "B" or m["signature"][2] != "per-product": continue
            t = D(c["variables"]["now"][:10])
            fri = t + dt.timedelta(days=4 - t.weekday())
            while not bday(fri): fri -= dt.timedelta(days=1)
            if fri <= t:
                fri = t + dt.timedelta(days=11 - t.weekday())
                while not bday(fri): fri -= dt.timedelta(days=1)
            pr = {p["product_id"]: p for p in c["expected"]["promises"]}; nt = {x["product_id"]: x["reason"] for x in c["expected"]["notes"]}
            for i, (a, p) in enumerate(zip(acts, c["products"])):
                pid = f"p{i + 1}"
                if a == "first": self.assertEqual((pr[pid]["amount"], pr[pid]["date"]), (p["past_due"], fri.isoformat()), c["id"]); n += 1
                if a == "second":
                    self.assertGreaterEqual(p["days_late"], 8)
                    self.assertEqual((pr[pid]["amount"], pr[pid]["date"]), (floor_of(p), nth_bday_after(t, 7).isoformat()), c["id"]); n += 1
                if a in ("refuse", "too-much-refuse"): self.assertEqual(nt[pid], "refused", c["id"])
                if a in ("dispute", "dispute-which", "dispute-the-loan"): self.assertEqual(nt[pid], "dispute", c["id"])
                if a == "too-much": self.assertEqual((pr[pid]["amount"], pr[pid]["date"]), (floor_of(p), t.isoformat()), c["id"])
        self.assertGreater(n, 5)

    def test_known_values(self):
        """Spot checks worked out by hand from Brief A section 7 and Brief B."""
        def row(rows, fam, sub, phr="none"):
            r, = [r for r in rows if (r["family"], r["sub"], r["phr"]) == (fam, sub, phr)]
            return r["expected"]
        # PUB-04 late band, Tue 27 Oct: attempt 2 = floor 400 on the 7th business day (26 Oct is past), Thu 5 Nov
        self.assertEqual(row(g.a_rows("PUB-04", D("2026-10-27")), "Persuasion", "takes-second")["promises"], [{"product_id": "p1", "amount": 400.0, "date": "2026-11-05"}])
        # Tue 10 Nov, 'after the weekend' then Tuesday: Tue 17 Nov (11 Nov is a holiday, the deadline is Fri 20 Nov)
        self.assertEqual(row(g.a_rows("PUB-04", D("2026-11-10")), "Dates", "after-the-weekend", "relative")["promises"][0]["date"], "2026-11-17")
        # Thu 24 Dec, 'end of next week', the Thursday: Thu 31 Dec
        self.assertEqual(row(g.a_rows("PUB-03", D("2026-12-24")), "Dates", "end-of-next-week", "relative")["promises"][0]["date"], "2026-12-31")
        # Thu 31 Dec, PUB-06 late: attempt 1 is Fri 8 Jan (1 Jan is a holiday, so this week's last business day is today), deadline Tue 12 Jan
        self.assertEqual(row(g.a_rows("PUB-06", D("2026-12-31")), "Persuasion", "takes-first")["promises"][0]["date"], "2027-01-08")
        self.assertEqual(row(g.a_rows("PUB-06", D("2026-12-31")), "Persuasion", "takes-second")["promises"], [{"product_id": "p1", "amount": 637.5, "date": "2027-01-12"}])
        # PUB-12 on Fri 23 Oct: card second offer = 153 on Wed 4 Nov; loan (late) and car loan (early) refused at different steps
        r = g.b_combo("PUB-12", D("2026-10-23"), ["second", "refuse", "refuse"], "none")
        self.assertEqual(r["expected"]["promises"], [{"product_id": "p1", "amount": 153.0, "date": "2026-11-04"}])
        self.assertEqual(r["expected"]["notes"], [{"product_id": "p2", "reason": "refused"}, {"product_id": "p3", "reason": "refused"}])
        self.assertIn("ladder", r["tags"]); self.assertEqual(r["family"], "Persuasion")
        # 'the loan' with two loans: the agent asks which; the note goes on the one the customer names
        r = g.b_combo("PUB-11", D("2026-10-14"), ["today", "first", "dispute-which"], "none")
        self.assertEqual(r["expected"]["notes"], [{"product_id": "p3", "reason": "dispute"}])
        self.assertEqual(r["expected"]["promises"], [{"product_id": "p1", "amount": 220.0, "date": "2026-10-14"}, {"product_id": "p2", "amount": 640.0, "date": "2026-10-16"}])
        self.assertIsNone(g.b_combo("PUB-13", D("2026-10-14"), ["today", "dispute-which"], "none"))      # one loan: never "which loan"
        self.assertIsNone(g.b_combo("PUB-11", D("2026-10-14"), ["today", "today", "dispute-the-loan"], "none"))
        self.assertIsNone(g.b_combo("PUB-13", D("2026-10-14"), ["second", "today"], "none"))           # the card is early band: no second offer
        # callbacks: Fri 23 Oct, Monday 26 is a bank holiday, Tuesday 27 the next business day
        self.assertEqual(g.slot_cat(D("2026-10-23"), "12:00", D("2026-10-26"), "11:00"), "holiday")
        self.assertEqual(g.slot_cat(D("2026-10-23"), "12:00", D("2026-10-27"), "11:00"), "ok")
        self.assertEqual(g.slot_cat(D("2026-10-23"), "12:00", D("2026-10-27"), "09:00"), "early")
        self.assertEqual(g.slot_cat(D("2026-10-23"), "12:00", D("2026-10-23"), "11:00"), "past")

    def test_corrections_keep_the_kind(self):
        self.assertEqual(g.wrong_amount(62.0, 28.5, 94), 82.0)                  # +40 is above the full amount, -40 below the floor
        for x, lo, hi in ((54.0, 1, 89), (195.0, 90, 299), (25.0, 1, 24.99), (2.0, 1, 3)):
            w = g.wrong_amount(x, lo, hi)
            self.assertTrue(lo <= w <= hi and w != x)
        for c in CASES:
            for m in re.finditer(r"'?([0-9.]+), sorry, I mean ([0-9.]+)", c["script"]):
                fl = min(floor_of(p) for p in c["products"]) if len(c["products"]) == 1 else None
                if fl is None: continue
                a, b = float(m.group(1)), float(m.group(2))
                self.assertEqual(a >= fl, b >= fl, c["id"])

    def test_review_lessons(self):
        for c, m in zip(CASES, META):
            if c["style"] == "talkative":
                self.assertFalse(any(p["date"].endswith("-30") for p in c["expected"]["promises"]), c["id"])
                self.assertIsNone(g.THIRTY.search(c["script"]), c["id"])
            if c["style"] == "corrects": self.assertNotEqual(m["signature"][6], "correction", c["id"])
            if c["base_id"] in g.FAILS_FIRST:
                self.assertFalse(any("exactly once" in x or "once" in x and "create_promise" in x for x in c["checks"]), c["id"])
            if c["family"] in ("Partial", "Below floor", "Pay full", "Counter-offer"): self.assertNotIn("and stops", c["script"], c["id"])
            if m["signature"][2] == "own-offer" or m["signature"][2].startswith("own-offer"): self.assertIn("in the same answer", c["script"])
        pool = g.candidates(25)
        for r in pool:
            if "too-much" in r["actions"] or r["sub"] == "minimum-first":
                self.assertIn("before the agent proposes anything", r["script"])
                self.assertNotIn("accepts the smaller amount", r["script"])
            if r["sub"].startswith("own-offer"): self.assertIn("in the same answer", r["script"])
        self.assertTrue(any(c["base_id"] in g.FAILS_FIRST for c in CASES))


class Determinism(unittest.TestCase):
    def test_same_seed_same_file(self):
        again, meta = g.generate_with_meta(250, 25)
        self.assertEqual(json.dumps(again), json.dumps(CASES)); self.assertEqual(meta, META)
        self.assertNotEqual(json.dumps(g.generate(250, 26)), json.dumps(CASES))

    def test_written_file_matches(self):
        for d in (KIT / "results", MAIN_RESULTS):
            out = d / "cases-250-new.json"
            if out.exists() and d == KIT / "results":
                self.assertEqual(json.load(open(out)), json.loads(json.dumps(CASES)))


if __name__ == "__main__":
    unittest.main()
