"""Offline tests for tools/gen_cases_1000.py: format, uniqueness, mix, and the expected bank records re-checked
against the brief with a second small calendar written here (not imported from the generator).

    cd build-the-builder && python -m pytest -q tests/test_gen_cases_1000.py
"""
import datetime as dt
import json
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT)); sys.path.insert(0, str(KIT / "tools"))

import gen_cases_1000 as g  # noqa: E402
from run_cases import persona_for, STYLES  # noqa: E402

BASE = {c["id"]: c for c in json.load(open(KIT / "public-test-set.json"))}
HOL = {"2026-10-26", "2026-11-11", "2026-11-26", "2026-12-25", "2027-01-01"}    # Brief A section 7
OUTCOMES = {"promise": {None}, "paid": {None}, "transferred": {"restructuring", "human", "support"},
            "no_promise": {"refused", "hardship", "dispute", "service_error"}, "not_verified": {"failed", "declined"},
            "not_reached": {"callback", "no_callback", "wrong_number", "deceased", "unclear"}}
QUEUE = {"restructuring": "restructuring", "human": "general", "support": "support"}
FAMILIES = {"Pay full", "Partial", "Below floor", "Refusal", "Persuasion", "Dates", "Dispute", "Already paid", "Hardship", "Transfer",
            "No talk", "Identity", "Mid-call change", "Asks back", "Callback", "Third party", "Multi-product mix"}


def bday(d): return d.weekday() < 5 and d.isoformat() not in HOL


def nth_bday_after(d, n):
    while n:
        d += dt.timedelta(days=1)
        if bday(d): n -= 1
    return d


def floor_of(p): return p["minimum_payment"] if p["type"] == "credit_card" else p["instalment_amount"]


CASES = g.generate(1000, 7)


class Format(unittest.TestCase):
    def test_fields_and_persona(self):
        need = {"id", "brief", "family", "level", "title", "variables", "identity", "products", "script", "checks", "style", "base_id", "third_party", "expected"}
        for c in CASES:
            self.assertTrue(need <= set(c), c["id"])
            self.assertFalse([k for k in c if k.startswith("_")], c["id"])
            self.assertIn(c["base_id"], BASE)
            b = BASE[c["base_id"]]
            self.assertEqual(c["products"], b["products"], c["id"])          # the mock serves the base customer's products
            self.assertEqual(c["identity"], b["identity"])
            self.assertEqual({k: v for k, v in c["variables"].items() if k != "now"}, {k: v for k, v in b["variables"].items() if k != "now"})
            dt.datetime.strptime(c["variables"]["now"], "%Y-%m-%dT%H:%M")
            self.assertEqual(c["brief"], "B" if len(c["products"]) > 1 else "A")
            self.assertTrue(c["style"] is None or c["style"] in STYLES, c["id"])
            self.assertTrue(c["checks"] and all(isinstance(x, str) and x for x in c["checks"]))
            self.assertEqual(set(c["expected"]), {"outcome", "reason", "promises", "notes", "callbacks", "transfers"})
            self.assertNotIn("{", c["script"]); self.assertNotIn("}", c["script"])
            per = persona_for(c, {"identity": c["identity"]})
            self.assertIn(c["script"], per["prompt"])
            if c["third_party"]: self.assertIn("You are NOT", per["prompt"])
            else: self.assertNotIn("You are NOT", per["prompt"])
            json.dumps(c)

    def test_unique(self):
        self.assertEqual(len({c["id"] for c in CASES}), len(CASES))
        self.assertEqual(len({(c["base_id"], c["variables"]["now"], c["script"]) for c in CASES}), len(CASES))
        self.assertEqual(len({(c["base_id"], c["variables"]["now"][:10], c["script"]) for c in CASES}), len(CASES))

    def test_mix(self):
        self.assertEqual(len(CASES), 1000)
        a = sum(c["brief"] == "A" for c in CASES)
        self.assertTrue(570 <= a <= 630, a)
        two = sum(len(c["products"]) == 2 for c in CASES); three = sum(len(c["products"]) == 3 for c in CASES)
        self.assertTrue(two >= 100 and three >= 100, (two, three))
        for brief in "AB":
            fams = {c["family"] for c in CASES if c["brief"] == brief}
            self.assertEqual(fams, FAMILIES - ({"Multi-product mix"} if brief == "A" else set()), brief)
        days = {c["variables"]["now"][:10] for c in CASES}
        self.assertTrue(set(g.CALL_DAYS) <= days)
        self.assertEqual({c["style"] for c in CASES}, {None} | set(STYLES))
        for o, reasons in OUTCOMES.items():
            got = {c["expected"]["reason"] for c in CASES if c["expected"]["outcome"] == o}
            self.assertEqual(got, reasons - {"service_error"}, o)                       # every outcome except a tool failure appears

    def test_deterministic(self):
        self.assertEqual(json.dumps(g.generate(1000, 7)), json.dumps(CASES))
        self.assertNotEqual(json.dumps(g.generate(1000, 8)), json.dumps(CASES))
        small = g.generate(100, 7)
        self.assertEqual(len(small), 100)

    def test_split(self):
        parts = g.split(CASES, 4)
        self.assertEqual(sorted(c["id"] for p in parts for c in p), sorted(c["id"] for c in CASES))
        fams = {(c["brief"], c["family"]) for c in CASES}
        for p in parts:
            self.assertEqual(len(p), 250)
            self.assertTrue(130 <= sum(c["brief"] == "A" for c in p) <= 170)
            self.assertEqual({(c["brief"], c["family"]) for c in p}, fams)

    def test_committed_files_match(self):
        out = KIT / "results" / "cases-1000.json"
        if not out.exists(): self.skipTest("results/cases-1000.json not generated")
        self.assertEqual(json.load(open(out)), json.loads(json.dumps(CASES)))
        parts = sorted(out.parent.glob("cases-1000-part*.json"))
        self.assertEqual(len(parts), 4)
        self.assertEqual(sorted(c["id"] for p in parts for c in json.load(open(p))), sorted(c["id"] for c in CASES))


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
            t = dt.date.fromisoformat(c["variables"]["now"][:10])
            ids = {f"p{i + 1}": p for i, p in enumerate(c["products"])}
            seen = set()
            for pr in c["expected"]["promises"]:
                self.assertIn(pr["product_id"], ids, c["id"]); self.assertNotIn(pr["product_id"], seen, c["id"]); seen.add(pr["product_id"])
                p = ids[pr["product_id"]]
                self.assertTrue(floor_of(p) <= pr["amount"] <= p["past_due"], (c["id"], pr))
                d = dt.date.fromisoformat(pr["date"])
                dl = nth_bday_after(t, 12 if p["days_late"] <= 7 else 7)
                self.assertTrue(t <= d <= dl and bday(d), (c["id"], pr, dl))
                n += 1
        self.assertGreater(n, 600)

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
            now = c["variables"]["now"]; t = dt.date.fromisoformat(now[:10])
            nxt = t + dt.timedelta(days=1)
            while not bday(nxt): nxt += dt.timedelta(days=1)
            for cb in c["expected"]["callbacks"]:
                self.assertTrue("10:00" <= cb["time"] <= "19:00", c["id"])
                self.assertIn(cb["date"], (t.isoformat(), nxt.isoformat()), c["id"])
                if cb["date"] == t.isoformat(): self.assertGreater(cb["time"], now[11:], c["id"])

    def test_known_dates(self):
        """Spot checks worked out by hand from Brief A section 7."""
        def find(base, day, kind):
            return [c for c in g.pool(7) if c["base_id"] == base and c["variables"]["now"].startswith(day) and c["_key"] == kind]
        c, = find("PUB-04", "2026-10-27", "second-offer")            # late band, Tue 27 Oct: attempt 2 = floor on the 7th business day, Thu 5 Nov
        self.assertEqual(c["expected"]["promises"], [{"product_id": "p1", "amount": 400.0, "date": "2026-11-05"}])
        c, = find("PUB-01", "2026-10-16", "first-offer")             # Friday: attempt 1 is the last business day of next week
        self.assertEqual(c["expected"]["promises"][0]["date"], "2026-10-23")
        c, = find("PUB-01", "2026-12-24", "moved-digits-2027-01-01")  # New Year's Day is a bank holiday: Monday 4 January
        self.assertEqual(c["expected"]["promises"][0]["date"], "2027-01-04")
        c, = find("PUB-13", "2026-10-14", "one-date-words-2026-10-31")  # rule B6: card to Mon 2 Nov, loan cut to its deadline 23 Oct
        self.assertEqual([p["date"] for p in c["expected"]["promises"]], ["2026-11-02", "2026-10-23"])

    def test_talkative_never_with_a_30th(self):
        for c in CASES:
            if c["style"] == "talkative":
                self.assertFalse(any(p["date"].endswith("-30") for p in c["expected"]["promises"]), c["id"])
                self.assertNotIn("30th", c["script"]); self.assertNotIn("thirtieth", c["script"])

    def test_scripts_leave_one_reading(self):
        """Review fixes: no amount stopped halfway, the floor asked for before any offer, the own offer in the decline turn."""
        pool = g.pool(7)
        for c in pool:
            if c["family"] in ("Partial", "Below floor", "Pay full"): self.assertNotIn("and stops", c["script"], c["_key"])
            if c["family"] == "Multi-product mix" and "too-much" in c["_key"].split("+"):
                self.assertIn("before the agent offers anything, says 'I can only manage the minimum, today'", c["script"])
                self.assertNotIn("accepts the smaller amount", c["script"])
            if c["_key"].startswith("own-offer"):
                self.assertIn("declines it and, in the same answer, says", c["script"])
        self.assertTrue(any(c["_key"].startswith("own-offer") for c in pool))
        self.assertTrue(any("too-much" in c["_key"].split("+") for c in pool if c["family"] == "Multi-product mix"))

    def test_no_create_promise_once_check_on_faulty_base(self):
        for c in CASES:
            if c["base_id"] in g.FAILS_FIRST: self.assertFalse(any("exactly once" in x for x in c["checks"]), c["id"])


if __name__ == "__main__":
    unittest.main()
