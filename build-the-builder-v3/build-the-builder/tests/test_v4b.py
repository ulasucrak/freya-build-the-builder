"""v4b: the negotiate extraction is off by default (latency), and a verified customer whose bank answer has no
all_verified (retry after a lost call) still reaches load_account.

Bank answers below are copied from the live focus run (FOC-HOL-01 / FOC-HOL-AFTER-01, retry call after a lost
first attempt): {"ok": true, "verified": true, "wrong_answers_left": 3} for both questions, no all_verified.

    cd build-the-builder && python3 -m unittest tests.test_v4b -v
"""
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
import test_generate as TG  # noqa: E402

OFF = {"extract_signals": False, "enforce_callback": False}
LIVE_DOB = {"ok": True, "verified": True, "wrong_answers_left": 3}
LIVE_PHONE = {"ok": True, "verified": True, "wrong_answers_left": 3}


def route_of(wf, node, vs):
    """The node `node` routes to for these variables (first matching transition; None: stays)."""
    for tr in wf["nodes"][node]["transitions"]:
        c = tr["condition"]
        if c.get("operator") == "always" or any(str(vs.get(x["variable"])) == str(x["operand"]) for x in c["conditions"]):
            return tr["target_node"]
    return None


class SignalsOffByDefault(unittest.TestCase):
    def test_default_negotiate_is_the_pre_v4_flow(self):
        for name in ("brief-a", "brief-b", "brief-a-live"):
            wf = G.generate_workflow(TG.raw(name))
            neg = wf["nodes"]["negotiate"]
            self.assertNotIn("extraction_schema", neg, name)
            self.assertNotIn("turn_state", wf["nodes"], name)
            # FX1: the entry is a fixed tts line when the policy fixes it (static_entry), else the model speaks it
            if G.static_entry(TG.S.normalise(TG.raw(name))):
                self.assertEqual(neg["on_enter"], {"type": "tts", "text": "{{w_entry_line}}"}, name)
            else:
                self.assertEqual(neg["on_enter"], {"type": "llm", "prompt": G.NEGOTIATE_ENTER}, name)
            llm = G.generate_workflow(TG.raw(name), options={"static_entry": False})["nodes"]["negotiate"]
            self.assertEqual(llm["on_enter"], {"type": "llm", "prompt": G.NEGOTIATE_ENTER}, name)
            self.assertNotIn("extraction_schema", llm, name)
            self.assertTrue(neg["on_enter_first_entry_only"], name)
            self.assertNotIn("turn_state", [t["target_node"] for t in neg["transitions"]], name)
            # negotiate is the node the options-off (pre-v4) build makes (the hooks' embedded runtime differs only in
            # enforce.callback_close, which negotiate's tools never read)
            off = G.generate_workflow(TG.raw(name), options=OFF)["nodes"]["negotiate"]
            for k in ("task_message", "on_enter", "on_enter_first_entry_only", "transitions", "max_node_turns"):
                self.assertEqual(neg[k], off[k], (name, k))
            self.assertEqual([t["function"]["name"] for t in neg["tools"]], [t["function"]["name"] for t in off["tools"]], name)
            self.assertEqual(G.enforce_rt(TG.S.normalise(TG.raw(name)), G.DEFAULT_OPTIONS)["signals"], [], name)

    def test_callback_close_and_farewell_stay_on(self):
        wf = G.generate_workflow(TG.raw("brief-b"))
        self.assertIn("close_check", [t["target_node"] for t in wf["nodes"]["greet_verify"]["transitions"]])
        self.assertIn("farewell", wf["nodes"])
        self.assertEqual(wf["nodes"]["finish"]["transitions"][0]["target_node"], "farewell")

    def test_option_on_still_builds_the_extraction(self):
        wf = G.generate_workflow(TG.raw("brief-b"), options={"extract_signals": True})
        self.assertIn("extraction_schema", wf["nodes"]["negotiate"])
        self.assertIn("turn_state", wf["nodes"])


class VerifiedWithoutAllVerified(unittest.TestCase):
    def sim(self, name="brief-a"):
        return TG.Sim(G.generate_workflow(TG.raw(name)), customer_id="c1")

    def test_live_retry_answers_reach_load_account(self):
        for name in ("brief-a", "brief-b", "brief-a-live"):
            s = self.sim(name)
            _c, _r, msg = s.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988-03-14"}, LIVE_DOB)
            self.assertIn("Now ask the next security question", msg, name)
            self.assertNotEqual(s.vs.get("w_id_state"), "verified", name)
            self.assertNotEqual(route_of(s.wf, "greet_verify", s.vs), "load_account", name)
            _c, _r, msg = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4471"}, LIVE_PHONE)
            self.assertEqual(s.vs["w_id_state"], "verified", name)
            self.assertIn("The customer is verified", msg, name)
            self.assertNotEqual(msg.strip(), "Correct.", name)
            self.assertEqual(route_of(s.wf, "greet_verify", s.vs), "load_account", name)

    def test_all_verified_true_unchanged(self):
        s = self.sim()
        s.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988-03-14"}, dict(LIVE_DOB, all_verified=False))
        self.assertNotEqual(s.vs.get("w_id_state"), "verified")
        _c, _r, msg = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4471"}, dict(LIVE_PHONE, all_verified=True))
        self.assertEqual(s.vs["w_id_state"], "verified")
        self.assertEqual(route_of(s.wf, "greet_verify", s.vs), "load_account")

    def test_explicit_all_verified_false_is_not_verified(self):
        s = self.sim()
        s.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988-03-14"}, dict(LIVE_DOB, all_verified=False))
        _c, _r, msg = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4471"}, dict(LIVE_PHONE, all_verified=False))
        self.assertNotEqual(s.vs.get("w_id_state"), "verified")
        self.assertNotEqual(route_of(s.wf, "greet_verify", s.vs), "load_account")
        self.assertIn("has not confirmed the identity yet", msg)

    def test_wrong_answer_then_correct_retry(self):
        s = self.sim()
        s.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988-03-14"}, LIVE_DOB)
        _c, _r, msg = s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4470"},
                             {"ok": True, "verified": False, "wrong_answers_left": 2})
        self.assertIn("Wrong answer", msg)
        self.assertNotEqual(s.vs.get("w_id_state"), "verified")
        s.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4471"}, dict(LIVE_PHONE, wrong_answers_left=2))
        self.assertEqual(route_of(s.wf, "greet_verify", s.vs), "load_account")


if __name__ == "__main__":
    unittest.main()
