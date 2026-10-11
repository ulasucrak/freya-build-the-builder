"""FX8: check_offer remembers offers per product and breaks loops; the callback last try asks nothing more; the
static entry line is shorter.

    cd build-the-builder && python3 -m unittest tests.test_fx8_hooks -v
"""
import json
import pathlib
import re
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
from builder import testmode as T  # noqa: E402
import test_generate as TG  # noqa: E402

# today Wed 14 Oct 2026 (brief-b): latest date Mon 2 Nov for a few days late; Mon 26 Oct is a holiday
LOAN = {"product_id": "p1", "type": "personal_loan", "past_due": 2100.0, "days_late": 6, "instalment_amount": 700.0}
CARD = {"product_id": "p2", "type": "credit_card", "card_last4": "4821", "past_due": 420.0, "days_late": 6, "minimum_payment": 126.0}


class OfferMemory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = G.generate_workflow(TG.raw("brief-b"))
        cls.code = TG.tool(cls.wf, "negotiate", "check_offer")["function"]["parameters"]["properties"]["code"]["default"]

    def setUp(self):
        vs = {"now": "2026-10-14T11:00"}
        T.run_code_locally(TG.code_of(self.wf["nodes"]["setup"]), {}, vs)
        vs["w_account_products"] = [dict(LOAN), dict(CARD)]
        T.run_code_locally(TG.code_of(self.wf["nodes"]["prepare_account"]), {}, vs)
        self.vs = vs                         # one variable table for the whole call: the memory lives in it

    def chk(self, **a):
        st, out = T.run_code_locally(self.code, a, self.vs)
        self.assertEqual(st, "success", out)
        return out

    def test_split_number_is_merged(self):
        """N056: 'No, but 14.' then '00 next Tuesday works for me.' is 1400 next Tuesday."""
        self.chk(product_id="p1", date="14")
        out = self.chk(product_id="p1", amount="00", date="Tuesday the 20th")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["amount"], "1400.00")
        self.assertTrue(out["date"].endswith("(2026-10-20)"), out)
        out = self.chk(product_id="p1", date="14")          # the fragment can also arrive in the date field
        out = self.chk(product_id="p1", date="00 Tuesday the 20th")
        self.assertEqual((out["ok"], out["amount"]), (True, "1400.00"), out)

    def test_ambiguous_fragment_is_not_merged(self):
        self.chk(product_id="p1", date="14")
        out = self.chk(product_id="p1", amount="50")         # '50' may be an amount of its own: not 1450
        self.assertEqual((out["code"], out["amount"]), ("below_floor", "50.00"))
        self.chk(product_id="p1", date="23 October")
        out = self.chk(product_id="p1", amount="00")         # nothing bare right before it
        self.assertEqual(out["code"], "bad_amount")
        self.assertIn("Ask for the whole amount", out["what_to_do_next"])

    def test_same_input_rejected_twice_asks_one_clarifying_question_then_stops(self):
        first = self.chk(product_id="p2", date="20 November")
        self.assertEqual(first["code"], "after_deadline")
        self.assertNotIn("ONE", first["what_to_do_next"])
        second = self.chk(product_id="p2", date="20 November")
        self.assertIn("ask ONE question for the whole amount and exact day", second["what_to_do_next"])
        self.assertIn("say the latest is Monday 2 November 2026", second["what_to_do_next"])    # the suggestion stays
        third = self.chk(product_id="p2", date="the 20th of November")
        self.assertIn("stop asking", third["what_to_do_next"])
        self.assertIn("callback", third["what_to_do_next"])
        # a valid check ends the streak
        self.assertTrue(self.chk(product_id="p2", date="23 October")["ok"])
        self.assertNotIn("ONE", self.chk(product_id="p2", date="20 November")["what_to_do_next"])

    def test_haggling_is_not_a_loop(self):
        """Review R08: 50, 80, 100 (all below the floor) are new offers, each answered with the floor."""
        for a in ("50", "80", "100"):
            out = self.chk(product_id="p2", amount=a)
            self.assertEqual(out["code"], "below_floor")
            self.assertNotIn("ONE", out["what_to_do_next"])
            self.assertNotIn("stop asking", out["what_to_do_next"])
            self.assertIn("offer 126.00", out["what_to_do_next"])
        for d in ("20 November", "21 November", "22 November"):
            self.assertNotIn("ONE", self.chk(product_id="p2", date=d)["what_to_do_next"])

    def test_split_number_loop_breaks(self):
        """N056 without the merge: the same two inputs alternating, rejected each time."""
        for a in [dict(date="20 November"), dict(amount="10"), dict(date="20 November"), dict(amount="10"), dict(date="20 November")]:
            out = self.chk(product_id="p1", **a)
        self.assertIn("stop asking", out["what_to_do_next"])

    def test_streak_is_per_product(self):
        self.chk(product_id="p1", date="20 November")
        out = self.chk(product_id="p2", date="20 November")
        self.assertNotIn("ONE", out["what_to_do_next"])
        self.assertIn("w_offers_p1", self.vs)
        self.assertIn("w_offers_p2", self.vs)

    def test_a_correction_is_not_a_refusal(self):
        """Review R08: the latest day was suggested, then a weekend day: no 'refused', a normal correction."""
        out = self.chk(product_id="p2", date="20 November")              # -> offer the latest day, Mon 2 Nov
        self.assertIn("2026-11-02", out["suggest_date"])
        self.assertIn("Offers left for this product: at most", out["what_to_do_next"])
        out = self.chk(product_id="p2", date="Saturday 31 October")
        self.assertEqual(out["code"], "not_business_day")
        self.assertNotIn("refused:", out["what_to_do_next"])
        self.assertIn("Offered via this tool: 2026-11-02", out["what_to_do_next"])

    def test_same_suggestion_twice_is_flagged(self):
        self.chk(product_id="p2", date="20 November")
        out = self.chk(product_id="p2", date="5 November")                # the latest day again
        self.assertIn("Already offered via this tool", out["what_to_do_next"])

    def test_whole_values_not_substrings(self):
        m = {"o": ["1126.00"], "a": [], "f": []}
        self.vs["w_offers_p2"] = json.dumps(m)
        out = self.chk(product_id="p2", amount="100")                     # offer 126.00 is not '1126.00'
        self.assertNotIn("Already offered", out["what_to_do_next"])

    def test_offers_left_counts_only_planned_attempts(self):
        offers = json.loads(self.vs["w_products_json"])[1]["offers"]
        n = len(offers)
        self.assertGreater(n, 0)
        out = self.chk(product_id="p2", date="Saturday 17 October")       # next business day: a correction, no attempt
        self.assertIn("at most " + str(n), out["what_to_do_next"])
        planned = [o for o in offers if 126.0 in (o.get("amount"), o.get("alt"))]
        out = self.chk(product_id="p2", amount="100")                     # the floor: a planned attempt when one has it
        self.assertIn("at most " + str(n - (1 if planned else 0)), out["what_to_do_next"])
        self.assertNotIn("No offers left", out["what_to_do_next"])

    def test_confirmed_parallel_promise_is_not_valid(self):
        """Review R08: a promise confirmed by the engine counters (sent_ok) is already recorded."""
        self.vs.update({"w_cp_sent_p2": "1", "create_promise_success_count": 1, "create_promise_failure_count": 0,
                        "create_promise_response.data.ok": "true"})
        self.assertEqual(self.chk(product_id="p2", date="23 October")["code"], "already_promised")

    def test_generic_no_brief_values(self):
        src = (KIT / "builder" / "codegen" / "check_offer.py").read_text()
        for bad in ("1400", "2100", "p1", "Tuesday", "2026"):
            self.assertNotIn(bad, re.sub(r"#.*", "", src), bad)


class CallbackLastTry(unittest.TestCase):
    def test_last_try_asks_nothing_more(self):
        """N149: the last-try instruction must not be turned into another question."""
        s = TG.Sim(G.generate_workflow(TG.raw("brief-b")))
        _c, _r, msg = s.call("greet_verify", "schedule_callback", {"date": "2026-10-16", "time": "11:00"})
        self.assertIn("Ask once for another", msg)
        _c, _r, msg = s.call("greet_verify", "schedule_callback", {"date": "2026-10-15", "time": "20:00"})
        self.assertIn("last try", msg)
        self.assertIn("do not ask anything else", msg)
        self.assertIn("cannot schedule", msg)
        self.assertIn("say goodbye", msg)


class ShortEntryLine(unittest.TestCase):
    def line(self, name, prods):
        s = TG.Sim(G.generate_workflow(TG.raw(name)), customer_id="c1")
        s.account(prods)
        return s.vs["w_entry_line"]

    def test_product_sentence_is_short_and_keeps_the_facts(self):
        line = self.line("brief-b", [dict(LOAN), dict(CARD)])
        self.assertIn("Your personal loan is 6 days late, with 2100 past due.", line)
        self.assertIn("Your credit card ending 4821 is 6 days late, with 420 past due.", line)
        for sent in re.findall(r"Your [^.]+\.", line):
            self.assertLessEqual(len(sent), 120, sent)
        self.assertNotIn("and the past-due amount is", line)

    def test_missing_days_or_amount(self):
        self.assertIn("Your personal loan has 2100 past due.", self.line("brief-a", [dict(LOAN, days_late=None)]))
        self.assertIn("Your personal loan is 6 days late.", self.line("brief-a", [dict(LOAN, past_due=None)]))

    def test_other_nouns(self):
        pol = json.loads((KIT / "policies" / "S6.policy.json").read_text())
        e = G.static_entry(__import__("builder.policy_schema", fromlist=["x"]).normalise(pol))
        if e:
            s = TG.Sim(G.generate_workflow(pol, None, json.loads((KIT / "tools" / "openapi.json").read_text())), customer_id="c1")
            s.account([dict(CARD)])
            self.assertNotRegex(s.vs["w_entry_line"], r"with \d+ ?(amount)?\.|  ")


if __name__ == "__main__":
    unittest.main()
