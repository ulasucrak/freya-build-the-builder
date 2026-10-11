"""Privacy-safe add_note text and the w_next_action state hint, through the generated hooks in simulated calls
(Brief B test policy; today Wed 14 Oct 2026 11:00).

    cd build-the-builder && python3 -m unittest tests.test_codegen_state -v
"""
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
import test_generate as TG  # noqa: E402

PRODS = [
    {"product_id": "p1", "type": "credit_card", "card_last4": "6604", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0},
    {"product_id": "p2", "type": "personal_loan", "past_due": 900.0, "days_late": 20, "instalment_amount": 300.0}]
PARTIAL = {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False}
ALL_OK = {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": True}
WRONG = {"ok": True, "verified": False, "wrong_answers_left": 2, "all_verified": False}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = G.generate_workflow(TG.raw("brief-b"))

    def setUp(self):
        self.sim = TG.Sim(self.wf, customer_id="c1")

    def hint(self):
        return self.sim.vs.get("w_next_action")

    def verified_with_account(self):
        self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988-03-14"}, ALL_OK)
        self.sim.account(PRODS)


class NoteText(Base):
    """KVKK data minimisation: what the customer told never reaches the bank's note."""

    def test_free_text_replaced_by_reason_phrase(self):
        self.verified_with_account()
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {
            "product_id": "p1", "reason": "hardship", "text": "Her husband died last month and she is in hospital with cancer."})
        self.assertEqual(cfg["body"]["text"], "No promise. Reason: hardship.")
        self.assertEqual((cfg["body"]["product_id"], cfg["body"]["reason"]), ("p1", "hardship"))
        self.assertNotIn(".invalid", cfg["url"])

    def test_unknown_reason_gives_no_reason_in_text(self):
        self.verified_with_account()
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p1", "reason": "lost job, divorce", "text": "{{text}}"})
        self.assertEqual(cfg["body"]["text"], "No promise.")

    def test_other_free_text_names_and_no_text_added(self):
        self.verified_with_account()
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p1", "reason": "refused", "comment": "diagnosed with depression"})
        self.assertEqual(cfg["body"]["comment"], "No promise. Reason: refused.")
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "dispute"})
        self.assertEqual(cfg["body"], {"product_id": "p2", "reason": "dispute"})

    def test_hook_has_no_customer_words(self):
        code = TG.tool(self.wf, "negotiate", "add_note")["pre_process_code"]
        self.assertIn("No promise", code)


class NextAction(Base):
    """w_next_action after each state transition."""

    def test_setup_empty(self):
        self.assertEqual(self.hint(), "")

    def test_identity_steps(self):
        self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988-03-14"}, PARTIAL)
        self.assertTrue(self.hint().startswith("Identity not verified yet (1 answer(s) correct, 0 wrong) - next: ask the next security question"))
        self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4401"}, WRONG)
        self.assertIn("(1 answer(s) correct, 1 wrong)", self.hint())
        self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4401"},
                      {"ok": True, "verified": False, "wrong_answers_left": 2, "hint": "too_short"})
        self.assertTrue(self.hint().startswith("Answer not counted (hint 'too_short') - next: ask for the answer again"))
        self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4410"}, ALL_OK)
        self.assertTrue(self.hint().startswith("Identity verified - next: the account is loaded automatically"))

    def test_identity_failed(self):
        self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1990-07-21"},
                      {"ok": True, "verified": False, "wrong_answers_left": 0, "all_verified": False})
        self.assertTrue(self.hint().startswith("Identity check failed - next: say nothing about the account"))

    def test_service_down(self):
        self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988-03-14"}, {"x": 1}, status="error")
        self.assertIn("did not answer", self.hint())
        self.assertIn("call verify_identity again", self.hint())

    def test_account_loaded(self):
        self.verified_with_account()
        self.assertEqual(self.hint(), "Identity verified, account loaded (2 product(s): p1, p2) - next: state the debt as in the "
                                      "account facts, then handle the products one by one.")
        s = TG.Sim(self.wf)
        s.account([])
        self.assertEqual(s.vs["w_next_action"], "")

    def test_promises_notes_and_close(self):
        self.verified_with_account()
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        self.assertTrue(self.hint().startswith("Promise recorded for p1; open: p2 - next: for each open product"))
        # p2 fails twice: the note (service_error) is all that is left
        self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"},
                      {"ok": False, "error": "boom"}, status="error")
        self.assertEqual(self.hint(), "Promise for p2 NOT recorded (service failed) - next: call create_promise again with exactly the same values.")
        self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"},
                      {"ok": False, "error": "boom"}, status="error")
        self.assertEqual(self.hint(), "Promise for p2 could not be recorded (service failed twice) - next: add_note for p2 (one per message), then close_call.")
        # the note fails once, then goes through
        self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "service_error"},
                      {"ok": False, "error": "down"}, status="error")
        self.assertEqual(self.hint(), "Note for p2 NOT sent (service failed) - next: call add_note once more with the same values.")
        self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "service_error"})
        self.assertEqual(self.hint(), "Note sent for p2 - next: ask whether there is anything else, then close_call with the outcome the rules give.")

    def test_all_promised(self):
        self.verified_with_account()
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"})
        self.assertEqual(self.hint(), "Promise recorded for p2; all products handled - next: ask whether there is anything else, "
                                      "then close_call with the outcome the rules give.")

    def test_transfer(self):
        self.verified_with_account()
        self.sim.call("negotiate", "transfer_to_agent", {"queue": "restructuring"})
        self.assertTrue(self.hint().startswith("Transfer done - next: tell the customer a colleague will take over"))
        s = TG.Sim(self.wf, customer_id="c1")
        s.call("negotiate", "transfer_to_agent", {"queue": "restructuring"}, {"ok": False, "error": "busy"})
        self.assertEqual(s.vs["w_next_action"], "Transfer did NOT work - next: call transfer_to_agent once more with the same queue.")

    def test_callback(self):
        self.sim.call("greet_verify", "schedule_callback", {"date": "2026-10-15", "time": "11:00"})
        self.assertTrue(self.hint().startswith("Callback scheduled - next: say when the customer will be called"))
        s = TG.Sim(self.wf, customer_id="c1")
        s.call("greet_verify", "schedule_callback", {"date": "2026-10-15", "time": "11:00"}, {"ok": False, "error": "x"})
        self.assertTrue(s.vs["w_next_action"].startswith("Callback NOT scheduled (service failed)"))

    def test_close_check_wrap(self):
        self.verified_with_account()
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        route, _b, _t = self.sim.close("negotiate", {"outcome": "promise_to_pay", "reason": ""})
        self.assertEqual(route, "wrap")
        self.assertTrue(self.hint().startswith("Close requested but not ready - next: These products have no recorded promise and no note yet: p2"))

    def test_generic_wording(self):
        """No amounts, dates or customer words in the hint: ids, counts and fixed phrases only."""
        self.verified_with_account()
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        for bad in ("300", "2026", "October", "Friday"):
            self.assertNotIn(bad, self.hint())


if __name__ == "__main__":
    unittest.main()
