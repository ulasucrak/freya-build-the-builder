"""Offline tests of the fake bank (builder/testmode.py). Run: python -m unittest tests/test_testmode.py"""
import copy
import json
import pathlib
import sys
import unittest

K = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(K))
from builder import testmode as tm  # noqa: E402

OPENAPI = json.load(open(K / "tools" / "openapi.json"))
BASE = json.load(open(K / "examples" / "base-agent.json"))
CASES = {c["id"]: c for c in json.load(open(K / "public-test-set.json"))}
REASONS = {"promise": [], "paid": [], "no_promise": ["refused", "hardship", "dispute", "service_error"],
           "not_verified": ["failed", "declined"], "transferred": ["restructuring", "human", "support"],
           "not_reached": ["callback", "no_callback", "wrong_number", "deceased", "unclear"]}


class Bank:
    """Drives the generated code_exec snippets locally, as the sandbox would."""

    def __init__(self, case, openapi=OPENAPI, extra=None, workflow=None):
        self.spec = tm.spec_from_openapi(openapi)
        sc = tm.scenario_for_case(case, dict({"max_wrong": 3, "close_reasons": REASONS}, **(extra or {})))
        self.vars = {"customer_id": "c_x", tm.SCENARIO_VAR: json.dumps(sc)}
        self.code = {t: tm.tool_code(t, self.spec) for t in self.spec["tools"]}

    def __call__(self, tool, **args):
        status, data = tm.run_code_locally(self.code[tool], dict(args, customer_id="c_x"), self.vars)
        return data if status == "success" else {"_error": data}

    def ledger(self):
        return tm.ledger_from_state(self.vars[tm.STATE_VAR])


class SpecTest(unittest.TestCase):
    def test_roles_from_kit_openapi(self):
        s = tm.spec_from_openapi(OPENAPI)["tools"]
        self.assertEqual(set(s), set(tm.KNOWN_TOOLS))
        self.assertEqual(s["verify_identity"]["roles"], {"field": "field", "answer": "answer"})
        self.assertEqual(s["create_promise"]["roles"], {"product": "product_id", "amount": "amount", "date": "date"})
        self.assertEqual(s["close_call"]["roles"], {"outcome": "outcome", "reason": "reason"})
        self.assertEqual(s["transfer_to_agent"]["fields"]["queue"]["enum"], ["restructuring", "general", "support"])

    def test_code_has_no_double_braces(self):
        spec = tm.spec_from_openapi(OPENAPI)
        hook = "d = {'a': {'b': 1}}\nresult['data']['x'] = d"
        for t in spec["tools"]:
            code = tm.tool_code(t, spec, hook, hook)
            self.assertNotIn("{{", code)
            self.assertNotIn("}}", code)
            compile("def f():\n" + "\n".join("    " + ln for ln in code.splitlines()), "x", "exec")


class SemanticsTest(unittest.TestCase):
    def test_happy_path(self):
        b = Bank(CASES["PUB-01"])
        self.assertEqual(b("get_account"), {"ok": False, "error": "identity not verified"})
        r = b("verify_identity", field="date_of_birth", answer="1988-03-14")
        self.assertEqual(r, {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        r = b("verify_identity", field="phone_last4", answer=" 4471")
        self.assertTrue(r["all_verified"])
        acc = b("get_account")
        self.assertEqual(acc["products"][0]["product_id"], "p1")
        self.assertEqual(acc["products"][0]["past_due"], 420)
        self.assertEqual(b("create_promise", product_id="p1", amount=420, date="2026-10-14"), {"ok": True, "promise_id": "pr1"})
        self.assertEqual(b("create_promise", product_id="p1", amount=420, date="2026-10-14")["error"], "this product already has a promise")
        self.assertEqual(b("close_call", outcome="promise"), {"ok": True})
        self.assertEqual(b("add_note", product_id="p1", reason="paid")["error"], "the call is already closed")
        led = b.ledger()
        self.assertEqual((led["outcome"], led["reason"]), ("promise", None))
        self.assertEqual([(p["product_id"], p["amount"], p["date"]) for p in led["promises"]], [("p1", 420, "2026-10-14")])
        self.assertEqual(led["calls"][-1]["tool"], "add_note")

    def test_swapped_hint_wrong_answers_lockout(self):
        b = Bank(CASES["PUB-03"])  # dob 1992-07-21
        r = b("verify_identity", field="date_of_birth", answer="1992-21-07")
        self.assertEqual(r, {"ok": True, "verified": False, "wrong_answers_left": 3, "hint": "day_month_swapped"})
        r = b("verify_identity", field="date_of_birth", answer="21 July 1992")
        self.assertFalse(r["ok"])  # unreadable: not counted
        r = b("verify_identity", field="date_of_birth", answer="1992-21-07")  # second swap counts
        self.assertEqual(r, {"ok": True, "verified": False, "wrong_answers_left": 2, "all_verified": False})
        b("verify_identity", field="phone_last4", answer="1111")
        r = b("verify_identity", field="date_of_birth", answer="1990-07-21")
        self.assertEqual(r["wrong_answers_left"], 0)
        r = b("verify_identity", field="date_of_birth", answer="1992-07-21")
        self.assertEqual(r["error"], "identity check is over")
        self.assertEqual(b("verify_identity", field="postcode", answer="x")["ok"], False)
        self.assertEqual(b("close_call", outcome="not_verified")["ok"], False)  # reason required
        self.assertEqual(b("close_call", outcome="not_verified", reason="failed"), {"ok": True})
        self.assertEqual(b.ledger()["wrong"], 3)

    def test_fault_and_validation(self):
        b = Bank(CASES["PUB-10"])
        r = b("create_promise", product_id="p1", amount=1, date="2026-10-16")
        self.assertIn("_error", r)  # fail_first_n=1 -> sandbox exception (status 500)
        self.assertIn("service unavailable", r["_error"])
        b("verify_identity", field="date_of_birth", answer="1989-02-23")
        b("verify_identity", field="phone_last4", answer="9904")
        self.assertEqual(b("create_promise", product_id="p9", amount=1, date="2026-10-16")["error"], "unknown product_id")
        self.assertEqual(b("create_promise", product_id="p1", amount="abc", date="2026-10-16")["error"], "amount must be a positive number")
        self.assertEqual(b("create_promise", product_id="p1", amount=365, date="16/10")["error"], "date must be YYYY-MM-DD")
        self.assertTrue(b("create_promise", product_id="p1", amount="365.00", date="2026-10-16")["ok"])
        self.assertEqual(b("add_note", product_id="p1", reason="bogus")["error"], "unknown reason")
        self.assertEqual(b("transfer_to_agent", queue="bogus")["error"], "unknown queue")
        self.assertTrue(b("transfer_to_agent", queue="general")["ok"])
        self.assertEqual(b("schedule_callback", date="tomorrow", time="11")["ok"], False)
        self.assertTrue(b("schedule_callback", date="2026-10-15", time="11:00")["ok"])
        self.assertEqual(b("close_call", outcome="weird")["error"], "unknown outcome")
        self.assertEqual(b("close_call", outcome="promise", reason="x")["ok"], False)
        led = b.ledger()
        self.assertEqual(led["failures"], {"create_promise": 1})
        self.assertEqual(led["calls"][0]["status"], 500)
        self.assertEqual(led["callbacks"], [{"date": "2026-10-15", "time": "11:00"}])

    def test_renamed_fields_and_enums(self):
        api = copy.deepcopy(OPENAPI)
        vi = api["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        vi["properties"] = {"customer_id": {"type": "string"},
                            "question": {"type": "string", "enum": ["postcode", "birth_year"]},
                            "reply": {"type": "string"}}
        cc = api["paths"]["/close_call"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        cc["properties"]["outcome"]["enum"] = ["agreed", "declined"]
        case = dict(CASES["PUB-01"], identity={"postcode": "AB1 2CD", "birth_year": "1988"})
        b = Bank(case, openapi=api, extra={"close_reasons": None})
        self.assertEqual(b.spec["tools"]["verify_identity"]["roles"], {"field": "question", "answer": "reply"})
        self.assertTrue(b("verify_identity", question="postcode", reply="ab1  2cd")["verified"])
        self.assertTrue(b("verify_identity", question="birth_year", reply="1988")["all_verified"])
        self.assertEqual(b("close_call", outcome="promise")["ok"], False)
        self.assertTrue(b("close_call", outcome="agreed")["ok"])


class TransformTest(unittest.TestCase):
    def test_base_agent(self):
        wf = tm.to_test_mode(BASE, OPENAPI)
        self.assertEqual(tm.LAST_STATS, {"converted": 4, "sink": 0})
        s = json.dumps(wf)
        self.assertNotIn("freya-bank-mock", s)
        vt = [t for t in wf["nodes"]["identity"]["tools"] if t["function"]["name"] == "verify_identity"][0]
        self.assertEqual(vt["function"]["handler"], "__function__:code_exec")
        props = vt["function"]["parameters"]["properties"]
        self.assertEqual(props["field"]["default"], "{{field}}")
        self.assertEqual(props["customer_id"]["default"], "{{customer_id}}")
        self.assertEqual(vt["function"]["parameters"]["required"], ["field", "answer"])
        self.assertEqual(vt["config"], BASE["nodes"]["identity"]["tools"][0]["config"])
        ends = [t for t in wf["nodes"]["identity"]["tools"] if t["function"]["name"] == "end_call"]
        self.assertEqual(ends[0]["function"]["handler"], "__function__:end_call")
        close = wf["tools"][0]
        self.assertNotIn("pre_process_code", close)
        self.assertIn("_fb_tool='close_call'", close["function"]["parameters"]["properties"]["code"]["default"])
        self.assertIn("b.pop('reason'", tm.tool_module("close_call", tm.spec_from_openapi(OPENAPI), BASE["tools"][0]["pre_process_code"]))
        self.assertEqual(BASE["tools"][0]["function"]["handler"], "__function__:api_call")  # input untouched

    def test_sink_for_end_of_call(self):
        wf = tm.to_test_mode(BASE, OPENAPI, sink={"base_url": "https://bank.example/v1", "var": "sink_id"})
        self.assertEqual(tm.LAST_STATS, {"converted": 3, "sink": 1})
        close = wf["tools"][0]
        p = close["function"]["parameters"]["properties"]
        self.assertEqual(close["function"]["handler"], "__function__:api_call")
        self.assertEqual(p["url"]["default"], "https://bank.example/v1/close_call")
        self.assertEqual(p["body"]["properties"]["customer_id"]["default"], "{{sink_id}}")
        self.assertIn("pre_process_code", close)

    def test_hooks_run_inside_snippet(self):
        t = copy.deepcopy(BASE["nodes"]["identity"]["tools"][0])
        t["pre_process_code"] = "config['body']['answer'] = str(config['body'].get('answer')).strip()\nvars['pre_ran'] = 'yes'"
        t["post_process_code"] = ("try:\n    result.pop('header', None)\n    d = result.get('data')\n"
                                  "    d['what_to_do_next'] = 'next' if d.get('verified') else 'again'\nexcept Exception:\n    pass\n")
        wf = copy.deepcopy(BASE)
        wf["nodes"]["identity"]["tools"][0] = t
        out = tm.to_test_mode(wf, OPENAPI)
        code = out["nodes"]["identity"]["tools"][0]["function"]["parameters"]["properties"]["code"]["default"]
        sc = tm.scenario_for_case(CASES["PUB-01"], {"max_wrong": 3})
        v = {tm.SCENARIO_VAR: json.dumps(sc)}
        st, data = tm.run_code_locally(code, {"field": "date_of_birth", "answer": " 1988-03-14 ", "customer_id": "c"}, v)
        self.assertEqual(st, "success")
        self.assertEqual(data["what_to_do_next"], "next")
        self.assertEqual(v["pre_ran"], "yes")
        self.assertTrue(data["verified"])  # the pre-hook stripped the answer before the bank saw it
        led = tm.ledger_from_state(v[tm.STATE_VAR])
        self.assertEqual(led["calls"][0]["tool"], "verify_identity")
        self.assertEqual(led["calls"][0]["args"], {})  # args are kept for write tools only

    def test_prehook_url_block_raises_without_recording(self):
        t = copy.deepcopy(BASE["nodes"]["identity"]["tools"][0])
        t["pre_process_code"] = "config['url'] = 'https://blocked.invalid/verify_identity'"
        t["post_process_code"] = "vars['post_ran'] = 'yes'"
        wf = copy.deepcopy(BASE)
        wf["nodes"]["identity"]["tools"][0] = t
        out = tm.to_test_mode(wf, OPENAPI)
        code = out["nodes"]["identity"]["tools"][0]["function"]["parameters"]["properties"]["code"]["default"]
        sc = tm.scenario_for_case(CASES["PUB-01"], {"max_wrong": 3})
        v = {tm.SCENARIO_VAR: json.dumps(sc)}
        st, msg = tm.run_code_locally(code, {"field": "date_of_birth", "answer": "1988-03-14", "customer_id": "c"}, v)
        self.assertEqual(st, "error")
        self.assertIn("DNS resolution failed for 'blocked.invalid'", msg)
        self.assertNotIn("post_ran", v)
        self.assertEqual(tm.ledger_from_state(v.get(tm.STATE_VAR))["calls"], [])

    def test_local_call_matches_snippet(self):
        spec = tm.spec_from_openapi(OPENAPI)
        sc = json.dumps(tm.scenario_for_case(CASES["PUB-01"], {"max_wrong": 3}))
        v1, v2 = {tm.SCENARIO_VAR: sc}, {tm.SCENARIO_VAR: sc}
        for tool, a in [("verify_identity", {"field": "date_of_birth", "answer": "1988-14-03"}), ("get_account", {})]:
            _, d1 = tm.run_code_locally(tm.tool_code(tool, spec), a, v1)
            _, d2 = tm.fakebank_call(tool, a, v2, spec)
            self.assertEqual(d1, d2)
        self.assertEqual(v1[tm.STATE_VAR], v2[tm.STATE_VAR])


class RunnerLedgerTest(unittest.TestCase):
    def test_ledger_from_call_state_replay_and_sink(self):
        sys.path.insert(0, str(K / "tools"))
        try:
            import run_synthetic as rs
        except Exception as e:
            self.skipTest("run_synthetic not importable: %s" % e)
        spec = tm.spec_from_openapi(OPENAPI)
        sc = tm.scenario_for_case(CASES["PUB-10"], {"max_wrong": 3})
        v = {tm.SCENARIO_VAR: json.dumps(sc)}
        calls = [("verify_identity", {"field": "date_of_birth", "answer": "1989-02-23"}),
                 ("verify_identity", {"field": "phone_last4", "answer": "9904"}), ("get_account", {}),
                 ("create_promise", {"product_id": "p1", "amount": 365, "date": "2026-10-16"}),
                 ("create_promise", {"product_id": "p1", "amount": 365, "date": "2026-10-16"})]
        msgs = []
        for tool, a in calls:
            code = tm.tool_code(tool, spec)
            msgs.append({"role": "system", "message": json.dumps({"event": "function_call_start", "function": tool, "args": {"params": a, "code": code}})})
            tm.run_code_locally(code, a, v)
            msgs.append({"role": "system", "message": json.dumps({"event": "route_evaluated", "variable_table": dict(v)})})
        sink = {"calls": [{"tool": "close_call", "args": {"outcome": "promise"}}]}
        for call in ({"messages": msgs}, {"messages": [m for m in msgs if "function_call_start" in m["message"]]}):
            led, source, _ = rs.ledger_for(call, sc, spec, sink)
            self.assertEqual(score_ok(CASES["PUB-10"], led), [], source)
            self.assertEqual(led["failures"], {"create_promise": 1})

    def test_parallel_tool_calls_restored(self):
        sys.path.insert(0, str(K / "tools"))
        import run_synthetic as rs
        spec = tm.spec_from_openapi(OPENAPI)
        sc = tm.scenario_for_case(CASES["PUB-09"], {"max_wrong": 3})
        v = {tm.SCENARIO_VAR: json.dumps(sc)}
        for tool, a in [("verify_identity", {"field": "date_of_birth", "answer": CASES["PUB-09"]["identity"]["date_of_birth"]}),
                        ("verify_identity", {"field": "phone_last4", "answer": CASES["PUB-09"]["identity"]["phone_last4"]}),
                        ("get_account", {})]:
            tm.run_code_locally(tm.tool_code(tool, spec), a, v)
        # one turn, two tool calls: both sandboxes start from the same variable table, the last write wins
        va, vb = dict(v), dict(v)
        tm.run_code_locally(tm.tool_code("close_call", spec), {"outcome": "transferred", "reason": "restructuring"}, va)
        tm.run_code_locally(tm.tool_code("transfer_to_agent", spec), {"queue": "restructuring"}, vb)
        final = dict(va)
        final.update(vb)
        self.assertNotIn("close_call", json.dumps(tm.ledger_from_state(final[tm.STATE_VAR])["calls"]))
        ev = lambda e: {"role": "system", "message": json.dumps(e)}
        msgs = [ev({"event": "function_call_complete", "function": f}) for f in
                ("verify_identity", "verify_identity", "get_account", "transfer_to_agent", "close_call")]
        msgs.append(ev({"event": "route_evaluated", "variable_table": final}))
        led, source, restored = rs.ledger_for({"messages": msgs}, sc, spec)
        self.assertEqual(source, "state+parallel")
        self.assertEqual([e[0] for e in restored], ["close_call"])
        self.assertEqual((led["outcome"], led["reason"]), ("transferred", "restructuring"))
        self.assertEqual(led["transfers"], [{"queue": "restructuring"}])
        self.assertEqual([c["tool"] for c in led["calls"]][-2:], ["transfer_to_agent", "close_call"])
        self.assertEqual(score_ok(CASES["PUB-09"], led), [])


def score_ok(case, led):
    sys.path.insert(0, str(K / "tools"))
    from score import score_ledger
    return score_ledger(case, led)["failed"]


class PayloadSizeTest(unittest.TestCase):
    """The platform answered HTTP 413 "Sandbox payload too large" for ~45-51 KB snippets plus a growing variable
    table. Keep each snippet and the fake bank's state small, also on a long call."""
    LIMIT_SNIPPET = 25000
    LIMIT_STATE = 8000

    def _snippets(self, wf):
        out = {}
        lists = [n.get(k) for n in wf["nodes"].values() for k in ("tools", "pre_actions")] + [wf.get("tools")]
        for lst in lists:
            for t in lst or []:
                code = t["function"]["parameters"]["properties"].get("code", {}).get("default", "")
                if "_fb_tool=" in code:
                    out[t["function"]["name"]] = code
        return out

    def test_generated_workflow_snippets_and_long_call(self):
        try:
            from builder import generate as G
        except Exception as e:  # generator not present in this checkout
            self.skipTest("builder.generate not importable: %s" % e)
        fix = K / "tests" / "fixtures_policy"
        names = [n for n in ("brief-b", "brief-a") if (fix / (n + ".policy.json")).exists()]
        if not names:
            self.skipTest("no policy fixtures")
        for name in names:
            wf = G.generate_workflow(json.loads((fix / (name + ".policy.json")).read_text()), openapi=OPENAPI)
            snippets = self._snippets(tm.to_test_mode(wf, OPENAPI))
            self.assertIn("create_promise", snippets)
            for tool, code in snippets.items():
                self.assertLess(len(code), self.LIMIT_SNIPPET, "%s %s snippet %d bytes" % (name, tool, len(code)))
            # 30 tool calls on one call: state in vars must stay small
            sc = tm.scenario_for_case(CASES["PUB-12"], {"max_wrong": 3, "close_reasons": REASONS})
            sc["faults"] = [{"tool": "create_promise", "fail_first_n": 2}]
            v = {tm.SCENARIO_VAR: json.dumps(sc), "customer_id": "c_x"}
            seq = [("verify_identity", {"field": "date_of_birth", "answer": "1983-10-19"}),
                   ("verify_identity", {"field": "phone_last4", "answer": "4410"}), ("get_account", {})]
            seq += [("create_promise", {"product_id": "p1", "amount": 510, "date": "2026-10-15"})] * 4
            seq += [("add_note", {"product_id": "p%d" % (i % 3 + 1), "reason": "refused", "text": "x" * 300}) for i in range(10)]
            seq += [("schedule_callback", {"date": "2026-10-15", "time": "11:00"})] * 5
            seq += [("get_account", {})] * 7 + [("close_call", {"outcome": "promise"})]
            self.assertEqual(len(seq), 30)
            worst = 0
            for tool, args in seq:
                code = snippets.get(tool) or tm.tool_code(tool, tm.spec_from_openapi(OPENAPI))
                tm.run_code_locally(code, dict(args, customer_id="c_x"), v)
                worst = max(worst, len(code) + len(json.dumps(v)))
            st = v[tm.STATE_VAR]
            self.assertLess(len(st), self.LIMIT_STATE, "state %d bytes" % len(st))
            led = tm.ledger_from_state(st)
            # calls the generated pre-hooks block (url rewritten to a dead host) are not recorded
            self.assertTrue(0 < len(led["calls"]) <= 30)
            self.assertEqual(led["outcome"], "promise")
            self.assertEqual([(p["product_id"], p["amount"]) for p in led["promises"]], [("p1", 510)])
            self.assertEqual(led["failures"], {"create_promise": 2})
            self.assertLess(worst, self.LIMIT_SNIPPET + self.LIMIT_STATE)
            print("\n  %s: largest fake snippet %d B, state after 30 calls %d B, worst snippet+fake vars %d B"
                  % (name, max(map(len, snippets.values())), len(st), worst))


if __name__ == "__main__":
    unittest.main()
