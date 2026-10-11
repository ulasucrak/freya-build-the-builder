"""The new parsers inside the generated hooks and the check_offer tool, run in simulated calls (Brief B test policy):
amounts in words / with corrections, relative and corrected days, corrected identity digits and callback times.

    cd build-the-builder && python3 -m unittest tests.test_codegen_parsing -v
"""
import json
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
from builder import testmode as T  # noqa: E402
import test_generate as TG  # noqa: E402

PRODUCT = {"product_id": "p1", "type": "credit_card", "card_last4": "4821", "past_due": 420.0, "days_late": 6,
           "minimum_payment": 126.0}


class CheckOfferParsing(unittest.TestCase):
    """Today Wed 14 Oct 2026; p1: floor 126, full 420, latest date Mon 2 Nov; Mon 26 Oct is a holiday."""

    @classmethod
    def setUpClass(cls):
        cls.wf = G.generate_workflow(TG.raw("brief-b"))
        tool = TG.tool(cls.wf, "negotiate", "check_offer")
        cls.code = tool["function"]["parameters"]["properties"]["code"]["default"]
        vs = {"now": "2026-10-14T11:00"}
        T.run_code_locally(TG.code_of(cls.wf["nodes"]["setup"]), {}, vs)
        vs["w_account_products"] = [dict(PRODUCT)]
        T.run_code_locally(TG.code_of(cls.wf["nodes"]["prepare_account"]), {}, vs)
        cls.vs = vs

    def run_check(self, **args):
        st, out = T.run_code_locally(self.code, dict(args, product_id="p1"), dict(self.vs))
        self.assertEqual(st, "success", out)
        return out

    def test_amounts_in_words_and_conventions(self):
        out = self.run_check(amount="four hundred and twenty")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["amount"], "420.00")
        self.assertEqual(self.run_check(amount="two hundred and fifty euros")["amount"], "250.00")
        self.assertEqual(self.run_check(amount="1.250,00 EUR")["code"], "above_full")
        self.assertEqual(self.run_check(amount="two thousand ... five hundred")["code"], "above_full")
        self.assertEqual(self.run_check(amount="one twenty-six")["amount"], "126.00")
        self.assertTrue(self.run_check(amount="one twenty-six")["ok"])

    def test_amount_corrections(self):
        out = self.run_check(amount="three hundred, sorry, I mean one hundred")
        self.assertEqual(out["code"], "below_floor")
        self.assertEqual(out["amount"], "100.00")
        self.assertTrue(self.run_check(amount="100, no, 200")["ok"])
        self.assertTrue(self.run_check(amount="200, not 100")["ok"])

    def test_full_and_minimum_words_and_old_fallback(self):
        """Review item 8: words that passed before must never be blocked."""
        for said, amount in [("full", "420.00"), ("the full amount", "420.00"), ("all of it", "420.00"),
                             ("minimum", "126.00"), ("the minimum", "126.00"), ("the minimum payment", "126.00")]:
            out = self.run_check(amount=said)
            self.assertTrue(out["ok"], (said, out))
            self.assertEqual(out["amount"], amount, said)
        self.assertTrue(self.run_check(amount="whatever they can")["ok"])            # nothing readable: not checked
        self.assertEqual(self.run_check(amount="the 25th")["code"], "below_floor")   # the old digit reading
        for out in (self.run_check(amount="bla"), self.run_check(amount="the full amount")):
            self.assertNotEqual(out["code"], "bad_amount")

    def test_no_amount_is_not_checked(self):
        self.assertIsNone(self.run_check(date="tomorrow", amount="")["amount"])
        self.assertTrue(self.run_check(date="tomorrow")["ok"])

    def test_relative_and_corrected_days(self):
        for said, label in [("a week from today", "(2026-10-21)"), ("Friday next week", "(2026-10-23)"),
                            ("Friday, no, Thursday", "(2026-10-15)"), ("the 20th, not the 21st", "(2026-10-20)"),
                            ("the twenty first, sorry, I mean the twentieth", "(2026-10-20)"), ("this time next week", "(2026-10-21)")]:
            out = self.run_check(date=said)
            self.assertTrue(out["ok"], (said, out))
            self.assertTrue(out["date"].endswith(label), (said, out))
            self.assertEqual(out["period"], "", said)
        self.assertTrue(self.run_check(date="in a fortnight")["date"].endswith("(2026-10-28)"))
        self.assertEqual(self.run_check(date="the 31st of next month")["code"], "bad_date")

    def test_periods_are_vague_and_propose_the_deadline(self):
        """EDGE-05 / AD-16 (chain250): a period is no day (the rules: ask once, then propose the deadline)."""
        for said in ("end of the month", "at the end of the month, when my salary comes", "end of next week", "next week",
                     "end of the week", "month end", "end of next month", "early next month"):
            out = self.run_check(date=said)
            self.assertFalse(out["ok"], said)
            self.assertEqual(out["code"], "bad_date", said)   # the code the generator tests know; `period` marks it
            self.assertEqual(out["period"], said)
            self.assertTrue(out["suggest_date"].endswith("(2026-11-02)"), out)
            self.assertIn("propose the deadline, Monday 2 November 2026", out["what_to_do_next"])
        out = self.run_check(date="when my salary comes")          # unreadable, no period: also the deadline after one ask
        self.assertEqual((out["code"], out["period"]), ("bad_date", ""))
        self.assertIn("propose the deadline, Monday 2 November 2026", out["what_to_do_next"])

    def run_after(self, first, **args):
        """check_offer after an earlier check_offer call: the engine keeps its result in check_offer_response.*"""
        vs = dict(self.vs)
        st, out = T.run_code_locally(self.code, dict(date=first, product_id="p1"), vs)
        for k, v in out.items():
            vs["check_offer_response.data." + k] = v
        st, out = T.run_code_locally(self.code, dict(args, product_id="p1"), vs)
        self.assertEqual(st, "success", out)
        return out

    def test_weekday_inside_the_period_said_before(self):
        # AD-16: 'at the end of next week' -> 'Thursday' is Thursday 22 October, not tomorrow (15 October)
        out = self.run_after("end of next week", date="Thursday")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["date"].endswith("(2026-10-22)"), out)
        self.assertEqual(out["period"], "")
        self.assertTrue(self.run_after("next week", date="Monday")["date"].endswith("(2026-10-19)"))
        self.assertTrue(self.run_after("end of the month", date="Friday")["date"].endswith("(2026-10-30)"))
        self.assertTrue(self.run_after("end of the month", date="the 27th")["date"].endswith("(2026-10-27)"))
        self.assertTrue(self.run_check(date="Thursday at the end of next week")["date"].endswith("(2026-10-22)"))
        # the period is not carried further, and 'this/next <weekday>' keep their own meaning
        self.assertTrue(self.run_after("Friday", date="Thursday")["date"].endswith("(2026-10-15)"))
        self.assertTrue(self.run_after("end of next week", date="this Thursday")["date"].endswith("(2026-10-15)"))
        self.assertTrue(self.run_check(date="Thursday")["date"].endswith("(2026-10-15)"))

    def test_suggested_day_of_a_planned_attempt_is_not_repeated(self):
        """GEN-05-refuses: 'I am not paying on the 24' -> the deadline, which is also the last planned attempt."""
        vs = dict(self.vs)
        prods = json.loads(vs["w_products_json"])
        prods[0]["offers"] = [dict(n=1, amount=420.0, date="2026-10-16"), dict(n=2, amount=126.0, date="2026-11-02")]
        vs["w_products_json"] = json.dumps(prods)
        st, out = T.run_code_locally(self.code, dict(date="the 3rd of November", product_id="p1"), vs)
        self.assertEqual(out["code"], "after_deadline")
        self.assertIn("Attempt 2 in the account facts is on this day too", out["what_to_do_next"])
        self.assertIn("do not offer it again", out["what_to_do_next"])
        st, out = T.run_code_locally(self.code, dict(date="Saturday the 17th", product_id="p1"), vs)
        self.assertEqual(out["code"], "not_business_day")
        self.assertNotIn("Attempt", out["what_to_do_next"])


class HookParsing(unittest.TestCase):
    def setUp(self):
        self.wf = G.generate_workflow(TG.raw("brief-b"))
        self.sim = TG.Sim(self.wf, customer_id="c1")

    def test_promise_date_relative_forms(self):
        self.sim.account([dict(PRODUCT)])
        cfg, _r, _m = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": "300", "date": "a week from today"})
        self.assertEqual(cfg["body"]["date"], "2026-10-21")
        self.assertEqual(cfg["body"]["amount"], 300.0)
        self.assertNotIn(".invalid", cfg["url"])

    def test_promise_date_next_week_weekday(self):
        self.sim.account([dict(PRODUCT)])
        cfg, _r, _m = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "Friday next week"})
        self.assertEqual(cfg["body"]["date"], "2026-10-23")

    def test_identity_digits_with_a_correction(self):
        cfg, _r, _m = self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4471, sorry, 4417"},
                                    {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "4417")
        # a kept piece, then the whole answer again
        s = TG.Sim(self.wf, customer_id="c1")
        cfg, _r, _m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "44"})
        self.assertIn("partial-answer", cfg["url"])
        cfg, _r, _m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "sorry, it is 4 4 7 1"},
                             {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "4471")
        cfg, _r, _m = self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "four o seven one"},
                                    {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "4071")

    def test_identity_digit_corrections_by_length(self):
        """Review item 9: a correction replaces the answer only when it is a whole answer."""
        for said, want in [("1520, sorry, 1502", "1502"), ("1502, not 1520", "1502"), ("four four, no, seven one", "4471"),
                           ("one hundred two three four", "1234")]:
            s = TG.Sim(self.wf, customer_id="c1")
            cfg, _r, _m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": said},
                                 {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
            self.assertEqual(cfg["body"]["answer"], want, said)

    def test_identity_partial_date_correction(self):
        cfg, _r, _m = self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "14 March 1988, no, 1989"},
                                    {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "1989-03-14")

    def test_identity_date_with_a_correction(self):
        cfg, _r, _m = self.sim.call("greet_verify", "verify_identity",
                                    {"field": "date_of_birth", "answer": "the 14th, sorry, the 15th of March 1988"},
                                    {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "1988-03-15")

    def test_callback_corrections(self):
        cfg, _r, _m = self.sim.call("greet_verify", "schedule_callback", {"date": "Friday, no, Thursday", "time": "three, sorry, four pm"})
        self.assertEqual(cfg["body"]["date"], "2026-10-15")
        self.assertEqual(cfg["body"]["time"], "16:00")


if __name__ == "__main__":
    unittest.main()
