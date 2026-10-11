"""FX2: tool-result robustness (S08 audit G1-G13, I04). Based on the probes in scratchpad/s08 (p1-p8)."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from builder import generate as G          # noqa: E402
import test_generate as TG                 # noqa: E402

N = "negotiate"
D = "2026-10-16"
PRODS = [{"product_id": "p1", "type": "credit_card", "card_last4": "4242", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0},
         {"product_id": "p2", "type": "personal_loan", "past_due": 540.0, "days_late": 20, "instalment_amount": 180.0}]
WF = {}


def wf(name="brief-b", **opts):
    key = (name, tuple(sorted(opts.items())))
    if key not in WF:
        WF[key] = G.generate_workflow(TG.raw(name), options=opts or None)
    return WF[key]


def sim(name="brief-b", prods=PRODS, **opts):
    s = TG.Sim(wf(name, **opts), customer_id="c1")
    s.vs["w_id_state"] = "verified"
    if prods is not None:
        s.account(prods)
    return s


def lib(*names):
    """The embedded helpers by name, executed with a runtime policy (for unit checks of common.py)."""
    import datetime
    import json
    g = {"re": re, "json": json, "datetime": datetime, "RT": {"names": {}, "closing": {"service_error": {"outcome": "no_promise", "reason": "service_error"}}}}
    want = set(names)
    for _n, code, _r, imp in G._library():
        if not imp:
            exec(code, g)
    return [g[n] for n in names]


def sent(cfg):
    return ".invalid" not in cfg["url"]


class OkRule(unittest.TestCase):
    """G1/G2/G5: one rule for success."""

    def test_call_ok(self):
        call_ok, = lib("call_ok")
        ok = lambda d, st="success", code=200: call_ok({"status": st, "status_code": code, "data": d}, "ok")
        self.assertTrue(ok({"ok": True}))
        self.assertTrue(ok({"promise_id": "x"}))                 # no ok key, no error
        self.assertFalse(ok({"ok": False}))
        self.assertFalse(ok({"ok": "false"}))
        self.assertFalse(ok({"error": "nope"}))                  # G2: error without an ok key
        self.assertFalse(ok({"ok": True, "error": "x"}))
        self.assertFalse(ok({"ok": True}, "error", 500))
        self.assertFalse(ok({"ok": True}, "success", 503))
        self.assertFalse(call_ok(None, "ok"))
        self.assertFalse(call_ok({"status": "success", "status_code": 200}, "ok"))   # no body

    def test_var_ok_and_err_kind(self):
        var_ok, err_kind = lib("var_ok", "err_kind")
        self.assertTrue(var_ok({}, "t", "ok"))                   # nothing stored: no evidence of a failure
        self.assertFalse(var_ok({"t_response.data.ok": "false"}, "t", "ok"))
        self.assertFalse(var_ok({"t_response.data.error": "x"}, "t", "ok"))
        self.assertTrue(var_ok({"t_response.data.ok": "true", "t_response.data.error": "old"}, "t", "ok"))
        r = lambda d: {"status": "success", "status_code": 200, "data": d}
        self.assertEqual(err_kind(r({"ok": False, "error": "promise already exists for product p1"})), "done")
        self.assertEqual(err_kind(r({"ok": False, "error": "duplicate"})), "done")
        self.assertEqual(err_kind(r({"ok": False, "error": "customer locked: too many wrong answers"})), "failed")
        self.assertEqual(err_kind(r({"ok": False, "error": "x", "wrong_answers_left": 0})), "failed")
        self.assertEqual(err_kind(r({"ok": False, "error": "product does not exists"})), "")
        self.assertEqual(err_kind(r({"ok": False, "error": "service busy"})), "")


class Promise(unittest.TestCase):
    def test_ok_false_is_not_a_promise(self):
        """G1: a 200 with ok:false counted as an engine success must not credit the product."""
        s = sim()
        _c, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D}, {"ok": False, "error": "service busy"})
        self.assertIn("NOT recorded", m)
        self.assertEqual(s.vs.get("create_promise_success_count"), 1)
        self.assertNotIn("w_promised_p1", s.vs)
        cfg, _r, _m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D})   # the retry goes out
        self.assertTrue(sent(cfg))

    def test_error_without_ok_key(self):
        """G2"""
        s = sim()
        _c, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D}, {"error": "nope"})
        self.assertIn("NOT recorded", m)
        self.assertNotIn("w_promised_p1", s.vs)

    def test_already_exists_counts_as_recorded(self):
        """G5: the bank already has it (an earlier request timed out): recorded, no retry, no service-error note."""
        s = sim()
        _c, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D}, {"ok": False, "error": "promise already exists for product p1"})
        self.assertTrue(m.startswith("Recorded"), m)
        self.assertEqual(s.vs.get("w_promised_p1"), "yes")
        self.assertNotIn("w_intent_p1", s.vs)

    def test_locked_stops_retrying(self):
        s = sim()
        _c, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D}, {"ok": False, "error": "account locked"})
        self.assertIn("cannot be recorded", m)
        cfg, _r, _m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D})
        self.assertIn("no-more-retries", cfg["url"])

    def test_case_insensitive_product_id(self):
        """G10"""
        s = sim()
        cfg, _r, _m = s.call(N, "create_promise", {"product_id": "P1 ", "amount": 300, "date": D})
        self.assertTrue(sent(cfg))
        self.assertEqual(cfg["body"]["product_id"], "p1")

    def test_account_failed_blocks_writes(self):
        """G3: get_account failed (prepare_account wrote 'failed'): no promise, no note; close as a service error."""
        s = sim(prods=[])
        self.assertEqual(s.vs["w_account_state"], "failed")
        cfg, _r, m = s.call(N, "create_promise", {"product_id": "p1", "amount": 5, "date": "2026-10-17"})
        self.assertIn("account-failed", cfg["url"])
        self.assertIn("service error", m)
        cfg, _r, m = s.call(N, "add_note", {"product_id": "zzz", "reason": "refused"})
        self.assertIn("account-failed", cfg["url"])

    def test_parallel_shared_state_does_not_credit_the_wrong_product(self):
        """G6 (probe p2): both pre-hooks ran before the post-hooks on one table; w_cp_pending names p2 only."""
        s = sim()
        t = TG.tool(s.wf, N, "create_promise")
        for pid, amt, d in (("p1", 300, D), ("p2", 540, "2026-10-23")):
            cfg = {"url": "u", "method": "POST", "body": {"product_id": pid, "amount": amt, "date": d}}
            TG.run(t["pre_process_code"], s.vs, "config", cfg)
            self.assertTrue(sent(cfg))
        self.assertEqual(s.vs["w_cp_pending"], "p2")
        res = {"status": "success", "status_code": 200, "data": {"ok": True}}
        TG.run(t["post_process_code"], s.vs, "result", res)     # p1's answer: unknown which product
        self.assertNotIn("w_promised_p2", s.vs)
        self.assertNotIn("w_promised_p1", s.vs)
        s.vs.update({"create_promise_success_count": 2, "create_promise_response.data.ok": "true"})
        cfg = {"url": "u", "body": {"product_id": "p1", "reason": "refused"}}
        TG.run(TG.tool(s.wf, N, "add_note")["pre_process_code"], s.vs, "config", cfg)
        self.assertIn("has-promise", cfg["url"])                 # both credited from the per-product marks
        s.vs["create_promise_response.data.ok"] = "false"        # the last answer failed: credit nothing (safe side)
        cfg = {"url": "u", "body": {"product_id": "p1", "reason": "refused"}}
        TG.run(TG.tool(s.wf, N, "add_note")["pre_process_code"], s.vs, "config", cfg)
        self.assertTrue(sent(cfg))

    def test_parallel_copies_still_credit_each(self):
        s = sim()
        s.parallel(N, [("create_promise", {"product_id": "p1", "amount": 300, "date": D}),
                       ("create_promise", {"product_id": "p2", "amount": 540, "date": "2026-10-23"})])
        self.assertEqual((s.vs.get("w_promised_p1"), s.vs.get("w_promised_p2")), ("yes", "yes"))


class Notes(unittest.TestCase):
    def test_ok_false_note_can_be_sent_again(self):
        """G1 (probe p3/p4): a failed note was blocked as already-noted and never sent."""
        s = sim()
        _c, _r, m = s.call(N, "add_note", {"product_id": "p1", "reason": "refused"}, {"ok": False, "error": "service busy"})
        self.assertIn("NOT sent", m)
        cfg, _r, _m = s.call(N, "add_note", {"product_id": "p1", "reason": "refused"})
        self.assertTrue(sent(cfg))
        self.assertEqual(s.vs.get("w_note_p1"), "refused")

    def test_note_already_exists_is_noted(self):
        s = sim()
        _c, _r, m = s.call(N, "add_note", {"product_id": "p1", "reason": "refused"}, {"ok": False, "error": "note already exists"})
        self.assertIn("Note sent for p1", m)
        cfg, _r, _m = s.call(N, "add_note", {"product_id": "p1", "reason": "refused"})
        self.assertIn("already-noted", cfg["url"])

    def test_two_failures_stop(self):
        """G4: after two failures no third try; the model is told to close as a service error."""
        s = sim()
        s.call(N, "add_note", {"product_id": "p1", "reason": "refused"}, {"ok": False, "error": "x"}, "error")
        _c, _r, m = s.call(N, "add_note", {"product_id": "p1", "reason": "refused"}, {"ok": False, "error": "x"})
        self.assertIn("failed twice", m)
        self.assertIn("'service_error'", m)
        cfg, _r, m = s.call(N, "add_note", {"product_id": "p2", "reason": "refused"})
        self.assertIn("service-down", cfg["url"])
        route, out, _t, _h = s.close_until_done(N, {"outcome": "no_promise", "reason": "service_error"})
        self.assertEqual(route, "finish")                        # no wrap-up round for notes that cannot be sent

    def test_reason_validated(self):
        """G12"""
        s = sim()
        cfg, _r, m = s.call(N, "add_note", {"product_id": "p2", "reason": "BOGUS"})
        self.assertIn("unknown-reason", cfg["url"])
        self.assertIn("refused", m)
        cfg, _r, _m = s.call(N, "add_note", {"product_id": "p2", "reason": "Hardship"})
        self.assertTrue(sent(cfg))
        self.assertEqual(cfg["body"]["reason"], "hardship")


class Transfer(unittest.TestCase):
    def test_failed_transfer_is_not_a_transfer(self):
        """G2 (probe p4 TR1/TR2)"""
        for resp in ({"ok": False, "error": "queue closed"}, {"error": "queue closed"}):
            s = sim()
            _c, _r, m = s.call(N, "transfer_to_agent", {"queue": "restructuring"}, resp)
            self.assertIn("did NOT work", m)
            cfg, _r, _m = s.call(N, "create_promise", {"product_id": "p1", "amount": 300, "date": D})
            self.assertTrue(sent(cfg))                           # no transfer recorded: promises still allowed

    def test_queue_and_repeat(self):
        """G13"""
        s = sim()
        cfg, _r, m = s.call(N, "transfer_to_agent", {"queue": "billing"})
        self.assertIn("unknown-queue", cfg["url"])
        self.assertIn("restructuring", m)
        cfg, _r, _m = s.call(N, "transfer_to_agent", {"queue": "Restructuring"})
        self.assertTrue(sent(cfg))
        self.assertEqual(cfg["body"]["queue"], "restructuring")
        cfg, _r, _m = s.call(N, "transfer_to_agent", {"queue": "restructuring"})
        self.assertIn("already-transferred", cfg["url"])

    def test_two_failures_stop(self):
        s = sim()
        s.call(N, "transfer_to_agent", {"queue": "general"}, {"ok": False}, "error")
        _c, _r, m = s.call(N, "transfer_to_agent", {"queue": "general"}, {"ok": False}, "error")
        self.assertIn("failed twice", m)
        cfg, _r, _m = s.call(N, "transfer_to_agent", {"queue": "general"})
        self.assertIn("service-down", cfg["url"])


class Callback(unittest.TestCase):
    def cb(self, s, date, time, resp=None, status="success"):
        return s.call("greet_verify", "schedule_callback", {"date": date, "time": time}, resp, status)

    def test_max_asks_counts_by_default(self):
        """I04 / EDGE-20: enforce_callback is on by default; two times outside the window close in the engine and a
        third schedule_callback never goes out."""
        self.assertTrue(G.DEFAULT_OPTIONS["enforce_callback"])
        w = wf("brief-a")
        self.assertIn("w_cb_over", str(w["nodes"]["greet_verify"]["transitions"]))
        s = TG.Sim(w, now="2026-10-14T20:15", customer_id="c1")
        _c, _r, m = self.cb(s, "2026-10-14", "21:00")
        self.assertIn("Ask once for another", m)
        _c, _r, m = self.cb(s, "2026-10-15", "08:00")
        self.assertEqual(s.vs["w_cb_over"], "yes")
        self.assertIn("last try", m)
        cfg, _r, _m = self.cb(s, "2026-10-15", "11:00")
        self.assertIn("no-callback", cfg["url"])
        route, body, _t = s.engine_close()
        self.assertEqual((body["outcome"], body["reason"]), ("not_reached", "no_callback"))

    def test_max_asks_without_engine_close(self):
        s = TG.Sim(wf("brief-a", enforce_callback=False), now="2026-10-14T20:15", customer_id="c1")
        self.cb(s, "2026-10-14", "21:00")
        self.cb(s, "2026-10-15", "08:00")
        cfg, _r, _m = self.cb(s, "2026-10-15", "11:00")
        self.assertIn("no-callback", cfg["url"])

    def test_already_scheduled_and_failures(self):
        """G5 (probe p5): 'already scheduled' is a booked callback; G4: two failures stop."""
        s = TG.Sim(wf("brief-a"), customer_id="c1")
        _c, _r, m = self.cb(s, "2026-10-14", "15:00", {"ok": False, "error": "already scheduled"})
        self.assertIn("is scheduled", m)
        self.assertEqual(s.vs["w_callback_done"], "yes")
        cfg, _r, _m = self.cb(s, "2026-10-14", "16:00")
        self.assertIn("no-callback", cfg["url"])
        s = TG.Sim(wf("brief-a"), customer_id="c1")
        self.cb(s, "2026-10-14", "15:00", {"ok": False}, "error")
        _c, _r, m = self.cb(s, "2026-10-14", "15:00", {"ok": False}, "error")
        self.assertIn("failed twice", m)
        cfg, _r, _m = self.cb(s, "2026-10-14", "15:00")
        self.assertIn("no-callback", cfg["url"])


class Close(unittest.TestCase):
    """G7: the engine's close counts only when the bank answered ok; the safety net can still close."""

    def fc(self, vs):
        w = G.generate_workflow(TG.raw("brief-b"), options={"final_close_fallback": True})
        cfg = {"url": "u", "body": {"outcome": "", "reason": ""}}
        TG.run(w["tools"][0]["pre_process_code"], vs, "config", cfg)
        return cfg

    def closed(self):
        s = sim(prods=PRODS[:1])
        s.close_until_done(N, {"outcome": "no_promise", "reason": "refused"})
        self.assertEqual(s.vs["w_close_sent"], "yes")
        self.assertEqual(s.vs["w_close_reqs"], "1")
        return s

    def test_close_ok_skips_safety_net(self):
        s = self.closed()
        s.vs.update({"close_call_success_count": 1, "close_call_response.data.ok": "true", "close_call_failure_count": 1})
        self.assertIn(".invalid", self.fc(dict(s.vs))["url"])

    def test_close_500_resent(self):
        s = self.closed()
        s.vs["close_call_failure_count"] = 2                      # the model's stopped request + the engine's close
        cfg = self.fc(dict(s.vs))
        self.assertEqual((cfg["url"], cfg["body"]), ("u", {"outcome": "no_promise", "reason": "refused"}))

    def test_close_ok_false_resent(self):
        s = self.closed()
        s.vs.update({"close_call_success_count": 1, "close_call_response.data.ok": "false", "close_call_failure_count": 1})
        self.assertEqual(self.fc(dict(s.vs))["url"], "u")


if __name__ == "__main__":
    unittest.main()
