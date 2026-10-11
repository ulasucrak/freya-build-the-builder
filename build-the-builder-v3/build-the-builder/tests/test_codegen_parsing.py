"""The new parsers inside the generated hooks and the check_offer tool, run in simulated calls (Brief B test policy):
amounts in words / with corrections, relative and corrected days, corrected identity digits and callback times.

    cd build-the-builder && python3 -m unittest tests.test_codegen_parsing -v
"""
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

    def test_unreadable_amount_is_not_waved_through(self):
        out = self.run_check(amount="the 25th")
        self.assertEqual(out["code"], "bad_amount")
        self.assertFalse(out["ok"])
        self.assertIn("exact amount", out["what_to_do_next"])

    def test_no_amount_is_not_checked(self):
        self.assertIsNone(self.run_check(date="tomorrow", amount="")["amount"])
        self.assertTrue(self.run_check(date="tomorrow")["ok"])

    def test_relative_and_corrected_days(self):
        for said, label in [("end of the week", "(2026-10-16)"), ("a week from today", "(2026-10-21)"),
                            ("end of next week", "(2026-10-23)"), ("Friday next week", "(2026-10-23)"),
                            ("Friday, no, Thursday", "(2026-10-15)"), ("the 20th, not the 21st", "(2026-10-20)"),
                            ("the twenty first, sorry, I mean the twentieth", "(2026-10-20)")]:
            out = self.run_check(date=said)
            self.assertTrue(out["ok"], (said, out))
            self.assertTrue(out["date"].endswith(label), (said, out))
        # the last business day of the month (Sat 31 Oct is not one), inside the deadline
        self.assertTrue(self.run_check(date="end of the month")["date"].endswith("(2026-10-30)"))
        self.assertTrue(self.run_check(date="in a fortnight")["date"].endswith("(2026-10-28)"))
        out = self.run_check(date="end of next month")
        self.assertEqual(out["code"], "after_deadline")
        self.assertTrue(out["suggest_date"].endswith("(2026-11-02)"), out)
        self.assertEqual(self.run_check(date="next week")["code"], "bad_date")        # vague: ask for the exact day
        self.assertEqual(self.run_check(date="the 31st of next month")["code"], "bad_date")


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
