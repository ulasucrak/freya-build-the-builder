"""FX11: free-judgment fallback. A hook whose input matches no fixed rule (undecided alternatives, garbled or missing
values, an unknown field) answers code 'unclear' with one short judgment text; after two unclear results in a row
for the same field it takes its strict path again. Real rule violations stay strict.

    cd build-the-builder && python3 -m unittest tests.test_fx11_unclear -v
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
from test_fx2_robustness import lib, sim, sent, N, D  # noqa: E402

LOAN = {"product_id": "p1", "type": "personal_loan", "past_due": 2100.0, "days_late": 6, "instalment_amount": 700.0}
CARD = {"product_id": "p2", "type": "credit_card", "card_last4": "4821", "past_due": 420.0, "days_late": 6, "minimum_payment": 126.0}
UNC_START = "Not covered by a fixed rule"


class Helper(unittest.TestCase):
    def test_unclear_counts_per_field_and_resets(self):
        unclear, = lib("unclear")
        vs = {}
        self.assertTrue(unclear(vs, "k", "amount"))
        self.assertTrue(unclear(vs, "k", "amount"))
        self.assertFalse(unclear(vs, "k", "amount"))           # third in a row: strict path
        self.assertFalse(unclear(vs, "k", "amount"))
        self.assertTrue(unclear(vs, "k", "date"))              # another field starts its own count
        self.assertFalse(unclear(vs, "k", ""))                 # a clear result resets
        self.assertTrue(unclear(vs, "k", "date"))
        self.assertTrue(unclear(vs, "other", "date"))          # keys are separate

    def test_two_values(self):
        two_values, parse_amount = lib("two_values", "parse_amount")
        self.assertTrue(two_values("200 or 300", parse_amount))
        self.assertTrue(two_values("four or five hundred", parse_amount))
        self.assertFalse(two_values("200 or so", parse_amount))
        self.assertFalse(two_values("300", parse_amount))
        self.assertFalse(two_values("200, sorry, 300", parse_amount))   # a correction is decided
        self.assertFalse(two_values("", parse_amount))

    def test_text_is_short_and_generic(self):
        unc, = lib("UNC")
        self.assertTrue(unc.startswith(UNC_START))
        self.assertIn("never invent amounts or dates", unc)
        self.assertLess(len(unc), 200)


class CheckOffer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = G.generate_workflow(TG.raw("brief-b"))
        cls.code = TG.tool(cls.wf, "negotiate", "check_offer")["function"]["parameters"]["properties"]["code"]["default"]

    def setUp(self):
        vs = {"now": "2026-10-14T11:00"}
        T.run_code_locally(TG.code_of(self.wf["nodes"]["setup"]), {}, vs)
        vs["w_account_products"] = [dict(LOAN), dict(CARD)]
        T.run_code_locally(TG.code_of(self.wf["nodes"]["prepare_account"]), {}, vs)
        self.vs = vs

    def chk(self, **a):
        st, out = T.run_code_locally(self.code, a, self.vs)
        self.assertEqual(st, "success", out)
        return out

    def test_undecided_amount_is_unclear_twice_then_strict(self):
        for _ in range(2):
            out = self.chk(product_id="p2", amount="200 or 300")
            self.assertEqual((out["ok"], out["code"]), (False, "unclear"), out)
            self.assertTrue(out["what_to_do_next"].startswith(UNC_START))
            self.assertIsNone(out["amount"])
        out = self.chk(product_id="p2", amount="200 or 300")    # third: the old reading (last stated amount)
        self.assertEqual((out["ok"], out["amount"]), (True, "300.00"), out)
        self.assertTrue(self.chk(product_id="p2", amount="200 or 300")["ok"])

    def test_undecided_day_is_unclear(self):
        out = self.chk(product_id="p2", date="Thursday or Friday")
        self.assertEqual(out["code"], "unclear", out)
        self.assertEqual(out["date"], "")
        out = self.chk(product_id="p2", date="23 October")       # a clear answer resets the count
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.chk(product_id="p2", date="Thursday or Friday")["code"], "unclear")

    def test_unclear_leaves_offer_memory_alone(self):
        """R10: an unclear input burns no planned attempt, records no offer and logs no failure."""
        before = self.vs.get("w_offers_p1")
        for _ in range(2):
            out = self.chk(product_id="p1", amount="50 or 60")
            self.assertEqual(out["code"], "unclear")
            self.assertEqual(self.vs.get("w_offers_p1"), before)
        out = self.chk(product_id="p1", amount="70")             # a clear, new input: a normal first rejection
        self.assertEqual(out["code"], "below_floor")
        self.assertNotIn("Already offered", out["what_to_do_next"])
        self.assertNotIn("Third time", out["what_to_do_next"])
        self.assertNotIn("ONE question", out["what_to_do_next"])

    def test_rule_violations_stay_strict(self):
        self.assertEqual(self.chk(product_id="p2", amount="50")["code"], "below_floor")
        self.assertEqual(self.chk(product_id="p2", date="20 November")["code"], "after_deadline")
        self.assertEqual(self.chk(product_id="p2", date="Saturday 17 October")["code"], "not_business_day")
        out = self.chk(product_id="p2", amount="50 or 60")       # both below the floor: still undecided first
        self.assertEqual(out["code"], "unclear")
        self.assertEqual(self.chk(product_id="p2", date="next week")["code"], "bad_date")   # a period keeps its rule
        self.assertEqual(self.chk(product_id="p9", date="tomorrow")["code"], "unknown_product")
        self.assertTrue(self.chk(product_id="p2", amount="whatever they can")["ok"])        # nothing named: not checked


class Promise(unittest.TestCase):
    def test_missing_amount_is_unclear_then_strict(self):
        s = sim()
        for _ in range(2):
            cfg, _r, m = s.call(N, "create_promise", {"product_id": "p1", "date": D})
            self.assertFalse(sent(cfg))
            self.assertIn("unclear", cfg["url"])
            self.assertIn(UNC_START, m)
        cfg, _r, m = s.call(N, "create_promise", {"product_id": "p1", "date": D})
        self.assertFalse(sent(cfg))
        self.assertNotIn("unclear", cfg["url"])                 # strict block, still nothing sent
        self.assertNotIn(UNC_START, m)
        self.assertEqual(s.vs.get("w_cp_tries_p1", "0"), "0")    # stopped tries are not counted as attempts

    def test_unreadable_date_and_unknown_product(self):
        s = sim()
        cfg, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": "soonish"})
        self.assertIn("unclear", cfg["url"])
        cfg, _r, m = s.call(N, "create_promise", {"product_id": "the other one", "amount": 300, "date": D})
        self.assertIn("unclear", cfg["url"])

    def test_violations_stay_strict(self):
        s = sim()
        cfg, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 10, "date": D})
        self.assertFalse(sent(cfg))
        self.assertNotIn("unclear", cfg["url"])
        cfg, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-17"})   # Saturday
        self.assertNotIn("unclear", cfg["url"])
        cfg, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D})
        self.assertTrue(sent(cfg))


class Callback(unittest.TestCase):
    def cb(self, s, date, time):
        return s.call("greet_verify", "schedule_callback", {"date": date, "time": time})

    def test_unreadable_time_uses_no_try(self):
        s = TG.Sim(G.generate_workflow(TG.raw("brief-a")), now="2026-10-14T20:15", customer_id="c1")
        for _ in range(2):
            cfg, _r, m = self.cb(s, "2026-10-15", "after lunch maybe")
            self.assertIn("unclear", cfg["url"])
            self.assertIn(UNC_START, m)
        self.assertEqual(s.vs.get("w_cb_bad", "0"), "0")
        cfg, _r, m = self.cb(s, "2026-10-15", "after lunch maybe")   # third: the strict path counts it
        self.assertNotIn("unclear", cfg["url"])
        self.assertEqual(s.vs.get("w_cb_bad"), "1")

    def test_alternating_unreadable_parts_still_use_tries(self):
        """R10: a garbled day, then a garbled time, then a garbled day...: one count, so the tries are used."""
        s = TG.Sim(G.generate_workflow(TG.raw("brief-a")), now="2026-10-14T20:15", customer_id="c1")
        urls = []
        for i in range(8):
            cfg, _r, _m = self.cb(s, "someday" if i % 2 else "2026-10-15", "11:00" if i % 2 else "after lunch maybe")
            urls.append(cfg["url"])
        self.assertTrue(all("unclear" in u for u in urls[:2]), urls)
        self.assertNotIn("unclear", urls[2])
        self.assertGreaterEqual(int(s.vs.get("w_cb_bad", "0")), 1)
        self.assertIn("no-callback", urls[-1])                  # max_asks used up: nothing more goes out

    def test_outside_window_stays_strict(self):
        s = TG.Sim(G.generate_workflow(TG.raw("brief-a")), now="2026-10-14T20:15", customer_id="c1")
        cfg, _r, m = self.cb(s, "2026-10-14", "21:00")
        self.assertNotIn("unclear", cfg["url"])
        self.assertIn("Ask once for another", m)


class Verify(unittest.TestCase):
    def sim(self):
        return TG.Sim(G.generate_workflow(TG.raw("brief-a")), customer_id="c1")

    def test_garbled_digits_not_sent_twice_then_sent(self):
        s = self.sim()
        for _ in range(2):
            cfg, _r, m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "hmm let me think"})
            self.assertIn("unclear", cfg["url"])
            self.assertIn(UNC_START, m)
            self.assertNotIn("hmm", m)                           # never echo the answer
        self.assertEqual(s.vs.get("w_id_wrong", "0"), "0")
        cfg, _r, m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "hmm let me think"},
                            {"ok": False, "error": "unreadable value", "wrong_answers_left": 3})
        self.assertNotIn(".invalid", cfg["url"])

    def test_unknown_field_and_empty_answer(self):
        s = self.sim()
        cfg, _r, _m = s.call("greet_verify", "verify_identity", {"field": "mother_maiden_name", "answer": "x"})
        self.assertIn("unclear", cfg["url"])
        cfg, _r, _m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": ""})
        self.assertIn("unclear", cfg["url"])
        s.call("greet_verify", "verify_identity", {"field": "other_field_x", "answer": "x"})
        self.assertNotIn("w_uc_vqother_field_x", s.vs)          # R10: one bounded key for unknown fields
        self.assertNotIn("w_uc_vqmother_maiden_name", s.vs)
        self.assertIn("w_uc_vq_", s.vs)

    def test_clear_answers_still_sent(self):
        s = self.sim()
        cfg, _r, m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "1234"},
                            {"ok": True, "verified": False, "wrong_answers_left": 2})
        self.assertNotIn(".invalid", cfg["url"])
        self.assertIn("Wrong answer", m)                       # a clear mismatch stays a wrong answer

    def test_unreadable_by_service_twice_then_strict(self):
        s = self.sim()
        bad = {"ok": False, "error": "unreadable value", "wrong_answers_left": 3}
        for _ in range(2):
            _c, _r, m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4471"}, bad)
            self.assertIn(UNC_START, m)
            self.assertIn("could not be read", m)
        _c, _r, m = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4471"}, bad)
        self.assertNotIn(UNC_START, m)
        self.assertIn("could not be read", m)


class Sizes(unittest.TestCase):
    def test_hooks_carry_the_text_once(self):
        wf = G.generate_workflow(TG.raw("brief-b"))
        code = TG.tool(wf, "negotiate", "check_offer")["function"]["parameters"]["properties"]["code"]["default"]
        self.assertEqual(code.count(UNC_START), 1)
        self.assertLess(len(code), 30000)
        self.assertIn(UNC_START, json.dumps(TG.tool(wf, "negotiate", "create_promise")))


if __name__ == "__main__":
    unittest.main()
