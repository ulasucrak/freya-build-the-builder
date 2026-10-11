"""FX3: verify_identity hooks with crafted bank answers.

G6 parallel verify calls in one message (each on its own copy of the variable table) are all credited.
G5 "locked" / no attempts left is the failed-check close path, not a retry or a service failure.
I10 a customer who asks back ("why do you need that?") is not pushed towards a declined close by the hints.

    cd build-the-builder && python3 -m unittest tests.test_fx3_verify -v
"""
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
import test_generate as TG  # noqa: E402
from test_v4b import route_of  # noqa: E402

DOB = {"field": "date_of_birth", "answer": "1988-03-14"}
PHONE = {"field": "phone_last4", "answer": "4471"}
OK = {"ok": True, "verified": True, "wrong_answers_left": 3}
WRONG = {"ok": True, "verified": False, "wrong_answers_left": 2}
NAMES = ("brief-a", "brief-b", "brief-a-live")


def sim(name="brief-a"):
    return TG.Sim(G.generate_workflow(TG.raw(name)), customer_id="c1")


class ParallelVerify(unittest.TestCase):
    def test_both_correct_without_all_verified(self):
        for name in NAMES:
            s = sim(name)
            s.parallel("greet_verify", [("verify_identity", DOB, OK), ("verify_identity", PHONE, OK)])
            self.assertEqual(s.vs.get("w_id_ok_date_of_birth"), "yes", name)
            self.assertEqual(s.vs.get("w_id_ok_phone_last4"), "yes", name)
            # the model asks the DOB again (its copy told it to): not sent, the customer is verified
            cfg, _r, msg = s.call("greet_verify", "verify_identity", DOB, OK)
            self.assertIn(".invalid", cfg["url"], name)
            self.assertIn("already checked", msg, name)
            self.assertEqual(s.vs["w_id_state"], "verified", name)
            self.assertEqual(route_of(s.wf, "greet_verify", s.vs), "load_account", name)

    def test_both_correct_bank_says_all_verified(self):
        s = sim()
        s.parallel("greet_verify", [("verify_identity", DOB, OK), ("verify_identity", PHONE, dict(OK, all_verified=True))])
        self.assertEqual(route_of(s.wf, "greet_verify", s.vs), "load_account")

    def test_one_correct_then_other_field_is_credited_next(self):
        s = sim()
        s.parallel("greet_verify", [("verify_identity", DOB, OK), ("verify_identity", PHONE, WRONG)])
        self.assertEqual(s.vs.get("w_id_ok_date_of_birth"), "yes")
        self.assertNotEqual(s.vs.get("w_id_state"), "verified")
        cfg, _r, msg = s.call("greet_verify", "verify_identity", PHONE, dict(OK, wrong_answers_left=2))
        self.assertNotIn(".invalid", cfg["url"])
        self.assertEqual(s.vs["w_id_state"], "verified")
        self.assertEqual(sorted(s.vs["w_id_done"].split(",")), ["date_of_birth", "phone_last4"])

    def test_parallel_wrong_answers_all_counted(self):
        s = sim()
        s.parallel("greet_verify", [("verify_identity", DOB, {"ok": True, "verified": False}),
                                    ("verify_identity", PHONE, {"ok": True, "verified": False})])
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, {"ok": True, "verified": False})
        self.assertEqual(s.vs["w_id_wrong"], "3")          # max_wrong 3, the bank gave no count
        self.assertEqual(s.vs["w_id_state"], "failed")
        self.assertIn("FAILED", msg)

    def test_explicit_not_confirmed_reask_is_sent(self):
        s = sim()
        s.call("greet_verify", "verify_identity", DOB, dict(OK, all_verified=False))
        s.call("greet_verify", "verify_identity", PHONE, dict(OK, all_verified=False))
        cfg, _r, _m = s.call("greet_verify", "verify_identity", DOB, dict(OK, all_verified=True))
        self.assertNotIn(".invalid", cfg["url"])
        self.assertEqual(route_of(s.wf, "greet_verify", s.vs), "load_account")


class VerifyErrors(unittest.TestCase):
    def assert_failed(self, s, msg, name=""):
        self.assertEqual(s.vs["w_id_state"], "failed", name)
        self.assertIn("FAILED", msg, name)
        self.assertNotIn("could not be read", msg, name)
        self.assertNotIn("did not answer", msg, name)
        self.assertIn("not_verified", msg, name)
        # no further attempt is sent
        cfg, _r, ev = s.call("greet_verify", "verify_identity", DOB, OK)
        self.assertIn(".invalid", cfg["url"], name)
        self.assertIn("already over", ev, name)

    def test_locked(self):
        for name in NAMES:
            for resp in ({"ok": False, "error": "locked"}, {"ok": False, "error": "Customer is LOCKED"},
                         {"ok": False, "error": "too many wrong answers"}, {"ok": False, "error": "no attempts left"}):
                s = sim(name)
                _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, resp)
                self.assert_failed(s, msg, (name, resp))

    def test_ok_false_with_no_attempts_left(self):
        s = sim()
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, {"ok": False, "error": "rejected", "wrong_answers_left": 0})
        self.assert_failed(s, msg)

    def test_error_status_locked(self):
        s = sim()
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, {"error": "locked"}, status="error")
        self.assert_failed(s, msg)

    def test_plain_service_failure_still_retries(self):
        s = sim()
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, {"error": "internal"}, status="error")
        self.assertIn("did not answer", msg)
        self.assertNotEqual(s.vs.get("w_id_state"), "failed")

    def test_unreadable_still_asks_again(self):
        s = sim()
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, {"ok": False, "error": "unreadable value", "wrong_answers_left": 2})
        self.assertIn("could not be read", msg)
        self.assertNotEqual(s.vs.get("w_id_state"), "failed")
        self.assertEqual(s.vs.get("w_id_wrong"), "0")

    def test_already_verified_is_credited(self):
        s = sim()
        s.call("greet_verify", "verify_identity", DOB, OK)
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, {"ok": False, "error": "already verified"})
        self.assertEqual(s.vs["w_id_state"], "verified")
        self.assertEqual(route_of(s.wf, "greet_verify", s.vs), "load_account")

    def test_wrong_with_left_counts_unchanged(self):
        s = sim()
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, {"ok": True, "verified": False, "wrong_answers_left": 0})
        self.assertEqual(s.vs["w_id_state"], "failed")
        self.assertEqual(s.vs["w_id_wrong"], "1")


class AsksBack(unittest.TestCase):
    """Rule 15: only a refusal after the one explanation, or a second such question, closes the call."""

    def test_hints_answer_why_and_reask(self):
        s = sim()
        _c, _r, msg = s.call("greet_verify", "verify_identity", DOB, OK)
        self.assertIn("asks why", msg)
        self.assertIn("ask again", msg)
        _c, _r, msg = s.call("greet_verify", "verify_identity", PHONE, WRONG)
        self.assertIn("asks why", msg)

    def test_verify_hooks_never_mention_declined(self):
        for name in NAMES:
            wf = G.generate_workflow(TG.raw(name))
            t = TG.tool(wf, "greet_verify", "verify_identity")
            for hk in ("pre_process_code", "post_process_code"):
                own = (pathlib.Path(G.__file__).parent / "codegen" / (("verify_pre" if hk.startswith("pre") else "verify_post") + ".py")).read_text()
                self.assertNotIn("declin", own.lower(), (name, hk))
                self.assertIn(hk, t)


if __name__ == "__main__":
    unittest.main()
