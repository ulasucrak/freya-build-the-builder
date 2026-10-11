"""Offline tests of builder/generate.py: structure of the workflow, the documented pitfalls, and the generated
Python (code nodes and hooks) executed locally in simulated calls.

    cd build-the-builder && python3 -m unittest tests.test_generate -v
"""
import json
import pathlib
import re
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import generate as G  # noqa: E402
from builder import policy_schema as S  # noqa: E402

FIX = KIT / "tests" / "fixtures_policy"
ALL = ("fixture-a", "fixture-x", "brief-a", "brief-b", "brief-a-live", "s3-live")


def raw(name):
    return json.loads((FIX / (name + ".policy.json")).read_text())


def code_of(node):
    return node["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]


def tool(wf, node, name):
    for t in wf["nodes"][node].get("tools") or []:
        if t["function"]["name"] == name:
            return t
    raise KeyError(name)


def fake_clock_builtins(wall_day):
    """__builtins__ whose `import datetime` gives a module where today()/now() report `wall_day`, so a snippet that
    used the real clock instead of the call's own `now` variable would produce different facts."""
    import builtins
    import datetime as real
    import types

    class FakeDate(real.date):
        @classmethod
        def today(cls):
            return wall_day

    class FakeDateTime(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(wall_day.year, wall_day.month, wall_day.day, 8, 15)

    mod = types.ModuleType("datetime")
    mod.__dict__.update(real.__dict__)
    mod.date = FakeDate
    mod.datetime = FakeDateTime
    b = dict(builtins.__dict__)
    orig = builtins.__import__

    def imp(name, *a, **k):
        return mod if name == "datetime" else orig(name, *a, **k)
    b["__import__"] = imp
    return b


def run(src, vs, extra_name=None, extra=None, wall_day=None):
    """Execute a snippet the way a strict sandbox might: separate globals and locals."""
    g = {"vars": vs}
    if wall_day is not None:
        g["__builtins__"] = fake_clock_builtins(wall_day)
    if extra_name:
        g[extra_name] = extra
    exec(compile(src, "snippet", "exec"), g, {})


def promised(vs):
    """Product ids with a recorded promise in the variable table (one variable per product)."""
    return sorted(k[len("w_promised_"):] for k, v in vs.items() if k.startswith("w_promised_") and v == "yes")


def underscore_names(src):
    import ast
    out = []
    for n in ast.walk(ast.parse(src)):
        for name in (getattr(n, "id", None), getattr(n, "attr", None), getattr(n, "arg", None),
                     n.name if isinstance(n, ast.FunctionDef) else None):
            if isinstance(name, str) and name.startswith("_"):
                out.append(name)
    return out


class Sim:
    """A tiny stand-in for the engine: runs a tool's hooks around a fake API result.

    Mirrors what a live call showed: a request the pre-hook sends to an unresolvable host fails before any HTTP
    response, and then the post-hook does NOT run. call() returns (config, result, message) where message is the
    post-hook's what_to_do_next, or w_last_event for a stopped request (that is what the prompt shows)."""

    def __init__(self, wf, now="2026-10-14T11:00", **call_vars):
        self.wf = wf
        self.vs = dict(call_vars)
        self.vs.setdefault("now", now)
        run(code_of(wf["nodes"]["setup"]), self.vs)

    def account(self, products, list_field="products"):
        self.vs["w_account_products"] = products
        run(code_of(self.wf["nodes"]["prepare_account"]), self.vs)

    def call(self, node, name, body, response=None, status="success"):
        t = tool(self.wf, node, name)
        cfg = {"url": t["function"]["parameters"]["properties"]["url"]["default"], "method": "POST", "body": dict(body)}
        if t.get("pre_process_code"):
            run(t["pre_process_code"], self.vs, "config", cfg)
        self.vs[name + "_args"] = cfg["body"]
        if ".invalid" in cfg["url"]:
            self.vs[name + "_failure_count"] = self.vs.get(name + "_failure_count", 0) + 1
            return cfg, None, self.vs.get("w_last_event", "")
        result = {"status": status, "data": dict(response or {"ok": True}), "status_code": 200 if status == "success" else 500,
                  "header": {"x": "y"}}
        if t.get("post_process_code"):
            run(t["post_process_code"], self.vs, "result", result)
        if status == "success":
            self.vs[name + "_success_count"] = self.vs.get(name + "_success_count", 0) + 1
            for k, v in (response or {"ok": True}).items():
                self.vs[name + "_response.data." + k] = str(v).lower() if isinstance(v, bool) else v
            for k, v in cfg["body"].items():
                self.vs[name + "_args." + k] = v
        return cfg, result, (result.get("data") or {}).get("what_to_do_next", "")

    def close(self, node, body):
        """The model's close_call (a close request), then the engine's close_check -> (wrap_up | finish -> close_api).
        Returns (route, final close body or None, wrap to-do text)."""
        cfg, _r, _m = self.call(node, "close_call", body)
        assert ".invalid" in cfg["url"], "the model's close_call must never reach the bank"
        return self.engine_close()

    def close_until_done(self, node, body, tries=4):
        """close() that tolerates close requests held back in the wrap-up step (notes missing). Returns
        (route, final close body or None, wrap to-do text, number of holds)."""
        holds = 0
        for _ in range(tries):
            cfg, _r, _m = self.call(node, "close_call", body)
            assert ".invalid" in cfg["url"], "the model's close_call must never reach the bank"
            if self.vs.get("w_close_requested") != "yes":
                holds += 1
                continue
            route, out, todo = self.engine_close()
            if route == "wrap":
                node = "wrap_up"
                continue
            return route, out, todo, holds
        return None, None, "", holds

    def engine_close(self):
        assert self.vs.get("w_close_requested") == "yes" or self.vs.get("w_forced") == "yes"
        run(code_of(self.wf["nodes"]["close_check"]), self.vs)
        if self.vs.get("w_close_route") == "wrap":
            return "wrap", None, self.vs.get("w_wrap_todo", "")
        return "finish", self.finish(), ""

    def finish(self):
        run(code_of(self.wf["nodes"]["finish"]), self.vs)
        t = self.wf["nodes"]["close_api"]["pre_actions"][0]
        body = {}
        for f, leaf in t["function"]["parameters"]["properties"]["body"]["properties"].items():
            v = leaf.get("default", "")
            m = re.fullmatch(r"\{\{(\w+)\}\}", str(v))
            body[f] = self.vs.get(m.group(1), "") if m else v
        body.pop("customer_id", None)
        cfg = {"url": t["function"]["parameters"]["properties"]["url"]["default"], "body": body}
        run(t["pre_process_code"], self.vs, "config", cfg)
        self.closed = cfg
        return cfg["body"]

    def parallel(self, node, calls):
        """Several tool calls of one model message: each runs on its own copy of the variable table, then the
        copies are merged key by key, last writer wins (what the engine showed in live calls)."""
        import copy
        base = copy.deepcopy(self.vs)
        outs, copies = [], []
        for name, body, *resp in calls:
            self.vs = copy.deepcopy(base)
            outs.append(self.call(node, name, body, *(resp or [])))
            copies.append(self.vs)
        merged = copy.deepcopy(base)
        for c in copies:
            for k, v in c.items():
                if k not in base or base[k] != v:
                    merged[k] = v
        self.vs = merged
        return outs


class Structure(unittest.TestCase):
    def test_all_fixtures_generate_and_lint_clean(self):
        for f in ALL:
            wf = G.generate_workflow(raw(f))
            self.assertEqual(G.lint_workflow(wf), [], f)
            text = json.dumps(wf)
            self.assertNotIn("null", re.sub(r'"[^"]*"', '""', text), f)
            self.assertNotIn("agent_config", wf)

    def test_pitfalls(self):
        wf = G.generate_workflow(raw("brief-b"))
        n = wf["nodes"]
        self.assertEqual(wf["initial_node"], "setup")
        for k, node in n.items():
            if node["node_type"] != "conversation":
                self.assertNotIn("tools", node, k)
            if node["node_type"] == "conversation":
                self.assertNotIn("pre_actions", node, k)
                self.assertIn(node["max_turn_transition"], n)
            for t in node.get("tools", []) + node.get("pre_actions", []):
                for hk in ("pre_process_code", "post_process_code"):
                    if hk in t:
                        compile(t[hk], hk, "exec")
                        self.assertNotIn("{{", t[hk])
                        self.assertNotIn("}}", t[hk])
                        self.assertEqual(underscore_names(t[hk]), [])   # no leading-underscore names
                if t["function"]["handler"] == "__function__:code_exec":
                    compile(code_of(node), k, "exec")
                    self.assertEqual(underscore_names(code_of(node)), [])
        # tool results are routed on both boolean spellings
        conds = [c["operand"] for tr in n["greet_verify"]["transitions"] for c in tr["condition"].get("conditions", [])
                 if "all_verified" in c.get("variable", "")]
        self.assertEqual(sorted(conds), ["True", "true"])
        self.assertEqual(n["load_account"]["response_mapping"][0]["path"], "data.products")
        # a close request is the first transition out of every conversation node; only close_api reaches the bank
        for k in ("greet_verify", "negotiate"):
            self.assertEqual(n[k]["transitions"][0]["target_node"], "close_check")
        self.assertEqual(n["wrap_up"]["transitions"][0]["target_node"], "finish")
        for k in ("greet_verify", "negotiate", "wrap_up"):
            close = [t for t in n[k]["tools"] if t["function"]["name"] == "close_call"][0]
            self.assertNotIn("post_process_code", close)
            self.assertIn("w_close_requested", close["pre_process_code"])

    def test_tools_follow_policy(self):
        wf = G.generate_workflow(raw("brief-b"))
        t = tool(wf, "negotiate", "create_promise")
        props = t["function"]["parameters"]["properties"]
        self.assertEqual(props["url"]["default"], "https://freya-bank-mock.freyavoice.workers.dev/v1/create_promise")
        body = props["body"]["properties"]
        self.assertEqual(body["customer_id"]["default"], "{{customer_id}}")
        self.assertEqual(body["amount"]["default"], "{{amount}}")
        self.assertEqual(set(props["body"]["required"]), {"product_id", "amount", "date"})
        q = tool(wf, "greet_verify", "transfer_to_agent")["function"]["parameters"]["properties"]["body"]["properties"]["queue"]
        self.assertEqual(q["enum"], ["restructuring", "general", "support"])
        names = [x["function"]["name"] for x in wf["nodes"]["negotiate"]["tools"]]
        self.assertEqual(names, ["create_promise", "add_note", "transfer_to_agent", "close_call"])
        # Brief A never uses add_note: the tool is not offered
        wa = G.generate_workflow(raw("brief-a"))
        self.assertNotIn("add_note", [x["function"]["name"] for x in wa["nodes"]["negotiate"]["tools"]])

    def test_variables_never_shadow_model_fields(self):
        """A variable with a model-filled field's name would bake the field. Our variables all start with w_."""
        wf = G.generate_workflow(raw("brief-b"))
        sim = Sim(wf, customer_id="c1")
        model_fields = set()
        for node in wf["nodes"].values():
            for t in node.get("tools", []):
                for f, leaf in t["function"]["parameters"]["properties"]["body"]["properties"].items():
                    if leaf["default"] == "{{" + f + "}}" and f != "customer_id":
                        model_fields.add(f)
        self.assertTrue(model_fields)
        self.assertFalse(model_fields & set(sim.vs))
        self.assertTrue(all(k.startswith("w_") for k in sim.vs if k not in ("now", "customer_id")))

    def test_other_bank_names(self):
        wf = G.generate_workflow(raw("fixture-x"))
        body = tool(wf, "negotiate", "create_promise")["function"]["parameters"]["properties"]["body"]["properties"]
        self.assertEqual(body["client_ref"]["default"], "{{client_ref}}")
        self.assertEqual(set(body), {"client_ref", "account_ref", "pay_amount", "pay_date"})
        self.assertEqual(wf["nodes"]["load_account"]["response_mapping"][0]["path"], "data.accounts")
        url = tool(wf, "greet_verify", "verify_identity")["function"]["parameters"]["properties"]["url"]["default"]
        self.assertEqual(url, "https://bank-x.example/api/verify_identity")
        self.assertNotIn("schedule_callback", [t["function"]["name"] for t in wf["nodes"]["negotiate"]["tools"]])

    def test_openapi_supplies_urls(self):
        p = raw("fixture-a")
        del p["api"]
        with self.assertRaises(ValueError):
            G.generate_workflow(p)
        openapi = json.loads((KIT / "tools" / "openapi.json").read_text())
        wf = G.generate_workflow(p, openapi=openapi)
        self.assertEqual(G.lint_workflow(wf), [])
        self.assertIn("workers.dev/v1/close_call", json.dumps(wf))

    def test_prompts_override(self):
        wf = G.generate_workflow(raw("fixture-a"), prompts={
            "system_prompt": "SYS", "opening_line": "Hi, am I speaking with {title} {last_name}?",
            "negotiate.task": "NEG", "nodes": {"greet_verify": {"extra": "EXTRA"}}, "tool.close_call": "CLOSE IT"})
        self.assertEqual(wf["system_prompt"], "SYS")
        self.assertEqual(wf["nodes"]["greet_verify"]["on_enter"], {"type": "tts", "text": "Hi, am I speaking with {{title}} {{last_name}}?"})
        self.assertEqual(wf["nodes"]["negotiate"]["task_message"], "NEG")
        self.assertTrue(wf["nodes"]["greet_verify"]["task_message"].endswith("EXTRA"))
        self.assertTrue(tool(wf, "negotiate", "close_call")["function"]["description"].startswith("CLOSE IT"))

    def test_default_prompts_carry_policy(self):
        wf = G.generate_workflow(raw("brief-b"))
        neg = wf["nodes"]["negotiate"]["task_message"]
        self.assertIn("{{w_account_facts}}", neg)
        self.assertIn("{{w_date_facts}}", neg)
        self.assertIn("add_note", neg)
        sysp = wf["system_prompt"]
        self.assertIn("You will receive a text message confirming this promise.", sysp + neg)
        p = S.normalise(raw("brief-b"))
        for r in p["rules"]:
            self.assertIn(G._brace_vars(r["text"], p), sysp)
        self.assertIn("Am I speaking with {{title}} {{last_name}}?", sysp)

    def test_write_tools_do_not_retry_in_the_engine(self):
        wf = G.generate_workflow(raw("brief-b"))
        for node in ("greet_verify", "negotiate"):
            for t in wf["nodes"][node]["tools"]:
                self.assertEqual(t["config"]["maxRetries"], 0, t["function"]["name"])
        self.assertEqual(wf["nodes"]["close_api"]["pre_actions"][0]["config"]["maxRetries"], 0)
        cond = wf["nodes"]["negotiate"]["transitions"][0]["condition"]["conditions"]
        self.assertIn({"variable": "w_close_requested", "operator": "equals", "operand": "yes"}, cond)

    def test_prompts_are_phase_filtered_and_short(self):
        wf = G.generate_workflow(raw("brief-b"))
        neg = wf["nodes"]["negotiate"]["task_message"]
        greet = wf["nodes"]["greet_verify"]["task_message"]
        for word in ("not_reached", "not_verified", "deceased", "wrong_number"):
            self.assertNotIn(word, neg)                      # third-party / identity situations are over
        self.assertNotIn("already_paid", greet)              # product notes need the account
        self.assertIn("{{w_last_event}}", neg)
        self.assertIn("{{w_last_event}}", greet)
        # per-turn static prompt (system + task) stays well below the review baseline (~28k / ~27k characters). A team
        # latency guard, not a platform limit: 0.72 since the edge-run fixes (first-refusal and phase guard lines)
        self.assertLess(len(wf["system_prompt"]) + len(greet), 0.72 * 28003)
        self.assertLess(len(wf["system_prompt"]) + len(neg), 0.72 * 26888)
        # without a rules list the section texts are carried in the tasks instead
        p = raw("brief-b")
        p["rules"] = []
        wf2 = G.generate_workflow(p)
        self.assertIn("Do you confirm?", wf2["nodes"]["negotiate"]["task_message"])

    def test_no_brief_a_defaults(self):
        p = raw("fixture-a")
        p["identity"]["questions"][0]["normalise"] = "none"
        wf = G.generate_workflow(p)
        task = wf["nodes"]["greet_verify"]["task_message"]
        self.assertNotIn("day, month, year", task)
        self.assertEqual(S.normalise({"identity": {"questions": [{"tool_field": "x", "answer_format": "date"}]}})
                         ["identity"]["questions"][0]["normalise"], "none")
        n = S.normalise(raw("brief-b"))
        self.assertEqual(G._names(n, G.DEFAULT_OPTIONS)["next_weekday"], "")

    def test_cli_exit_codes(self):
        import subprocess
        import tempfile
        out = tempfile.mkdtemp()
        ok = subprocess.run([sys.executable, "-m", "builder.generate", str(FIX / "brief-b.policy.json"), "-o", out + "/b.json"],
                            cwd=str(KIT), capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        bad = raw("fixture-a")
        bad["calendar"]["holidays"].append("26.10.2026")
        pathlib.Path(out, "bad.json").write_text(json.dumps(bad))
        r = subprocess.run([sys.executable, "-m", "builder.generate", out + "/bad.json", "-o", out + "/x.json"],
                           cwd=str(KIT), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error:", r.stderr)

    def test_builder_source_has_no_brief_values(self):
        """Nothing from a brief or the kit's example API values may be written into the builder."""
        banned = ["Freya Bank", "2026-", "2027-", "credit_card", "personal_loan", "car_loan", "minimum_payment",
                  "instalment", "past_due", "not_reached", "no_promise", "not_verified", "\"transferred\"", "'transferred'",
                  "wrong_number", "deceased", "restructuring", "hardship", "day_month_swapped", "phone_last4",
                  "date_of_birth", "10:00", "19:00", "collections", "4821", "text message confirming"]
        files = [KIT / "builder" / "generate.py", KIT / "builder" / "oracle.py", KIT / "builder" / "policy_schema.py"]
        files += sorted((KIT / "builder" / "codegen").glob("*.py"))
        for f in files:
            src = f.read_text()
            for b in banned:
                self.assertNotIn(b, src, "%s contains %r" % (f.name, b))


class Compactness(unittest.TestCase):
    """Snippets run in a sandbox whose payload (code + variables) is limited: a test-mode create_promise of 51 KB
    hit 'Sandbox payload too large' on its retry (T06 self-test SELF-07)."""

    def test_runtime_literal_is_exact_and_brace_safe(self):
        rt = {"a": {"b": {"c": "x}}y{{z", "q": "it's \\ \"ok\" \u00e9"}}}
        lit = G._py_literal(json.dumps(rt, separators=(",", ":")))
        self.assertNotIn("{{", lit)
        self.assertNotIn("}}", lit)
        self.assertEqual(json.loads(eval(lit)), rt)

    def test_hook_sizes(self):
        wf = G.generate_workflow(raw("brief-b"))
        for node in ("greet_verify", "negotiate"):
            for t in wf["nodes"][node]["tools"]:
                for hk in ("pre_process_code", "post_process_code"):
                    self.assertLess(len(t.get(hk, "")), 21000, (t["function"]["name"], hk))   # was up to 24 KB


class EdgeRegressions(unittest.TestCase):
    """Failures of the teammate edge run (EDGE-10, GEN-09-no-talk, GEN-3P-offers-pay), on every fixture policy."""

    def test_verified_customer_never_closes_with_a_pre_verification_outcome(self):
        for name in ALL:
            wf = G.generate_workflow(raw(name))
            p = S.normalise(raw(name))
            cl = p["closing"]
            nm = G._names(p, G.DEFAULT_OPTIONS)
            fo, fr = nm["close_outcome"], nm["close_reason"]
            pre = [cl[k] for k in ("id_fail", "callback", "no_callback") if cl.get(k) and cl[k].get("outcome")]
            for want in pre:
                s = Sim(wf, customer_id="c1")
                s.account([{"product_id": "p1", "type": "credit_card", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0}])
                s.vs["w_id_state"] = "verified"
                req = {fo: want["outcome"], fr: want.get("reason") or ""}
                _route, body, _t, _h = s.close_until_done("negotiate", req)
                self.assertNotEqual(body.get(fo), want["outcome"], "%s: %s kept after verification" % (name, want))
            s = Sim(wf, customer_id="c1")             # before verification the request stands
            _route, body, _t = s.close("greet_verify", {fo: cl["id_fail"]["outcome"], fr: "declined"})
            self.assertEqual(body[fo], cl["id_fail"]["outcome"], name)

    def test_recorded_promise_asks_anything_else_before_goodbye(self):
        wf = G.generate_workflow(raw("brief-a"))
        s = Sim(wf, customer_id="c1")
        s.account([{"product_id": "p1", "type": "credit_card", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0}])
        s.vs["w_id_state"] = "verified"
        _c, _r, msg = s.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-14"})
        self.assertIn("Do not say goodbye yet", msg)
        self.assertLess(msg.index("anything else"), msg.index("say goodbye and call close_call"))
        self.assertIn("change or cancel a recorded promise", msg)

    def test_phase_guards_in_prompts(self):
        for name in ALL:
            p = S.normalise(raw(name))
            wf = G.generate_workflow(raw(name))
            self.assertIn("security questions are over", wf["nodes"]["negotiate"]["task_message"], name)
            self.assertIn("never mention them or why they were asked", wf["nodes"]["negotiate"]["task_message"], name)
            neg = wf["nodes"]["negotiate"]["task_message"]
            for line in neg.splitlines():          # a close after "twice" says what to do the first time
                if re.search(r"\btwice\b", line) and "Close:" in line:
                    self.assertIn("The first time: do not mention security", line, name)
            self.assertIn("Ask each question without giving a reason", wf["nodes"]["greet_verify"]["task_message"], name)
            greet = wf["nodes"]["greet_verify"]["task_message"]
            if p["third_party"]["cases"]:
                self.assertIn("not even that you cannot discuss them", greet, name)
                self.assertIn("speak with the customer personally", greet, name)


class SelfTestAndHolidayRegressions(unittest.TestCase):
    """Self-test 7/10 of the fixed build and the live holiday failures (GEN-01/07-holiday: a holiday inside the
    deadline was answered with the deadline instead of the next business day)."""

    def facts(self, days_late):
        wf = G.generate_workflow(raw("brief-a"))
        s = Sim(wf, customer_id="c1")
        s.account([{"product_id": "p1", "type": "personal_loan", "past_due": 540.0, "days_late": days_late, "instalment_amount": 180.0}])
        return s.vs["w_account_facts"]

    def test_holiday_inside_the_deadline_offers_the_next_business_day(self):
        early = self.facts(5)                     # deadline Mon 2 Nov
        self.assertIn("Mon 26 Oct (holiday) -> Tuesday 27 October 2026 (2026-10-27)", early)
        self.assertIn("Sun 1 Nov -> Monday 2 November 2026", early)
        self.assertIn("never call it a public or bank holiday", early)
        late = self.facts(11)                     # deadline Fri 23 Oct: the 26th is after it
        self.assertNotIn("Mon 26 Oct", late.split("Days up to the deadline")[-1])
        self.assertIn("Sat 17 Oct -> Monday 19 October 2026", late)

    def test_one_promise_per_message(self):
        neg = G.generate_workflow(raw("brief-b"))["nodes"]["negotiate"]["task_message"]
        self.assertIn("one per message, after each result", neg)

    def test_notes_only_for_several_products_when_an_override_adds_them(self):
        p = S.normalise(raw("brief-b"))
        self.assertEqual(p["notes"]["min_products"], 2)
        q = raw("brief-b")
        q["rules"] = [r for r in q["rules"] if "[overrides" not in r.get("text", "")]
        q["notes"] = dict(q.get("notes") or {}, when="For each product without a promise call add_note.")
        self.assertEqual(S.normalise(q)["notes"]["min_products"], 1)
        self.assertEqual(S.normalise(raw("brief-a"))["notes"]["min_products"], 1)

    def test_selftest_plans_follow_the_rules(self):
        from builder import casegen
        pl = casegen.Planner(S.normalise(raw("brief-b")), seed=3) if hasattr(casegen, "Planner") else None
        if pl is None:
            self.skipTest("no Planner class")
        multi = pl.plan_multi()
        self.assertIn("confirms the read-back", multi["plan"])
        self.assertNotIn("each read-back", multi["plan"])
        for trig in pl.post_verification_triggers():
            if trig.get("action") == "transfer" and trig.get("queue"):
                c = pl.plan_trigger(trig, pl.bands()[0])
                self.assertIn("Accepts when the agent offers to connect them", c["plan"])
        self.assertIn("never prescribe how it orders or groups the talk", casegen.llm_request([], S.normalise(raw("brief-b"))))


class S3Flows(unittest.TestCase):
    """fixtures/S3 live failures: notes were switched off, parallel calls lost promises and the transfer."""

    def setUp(self):
        self.wf = G.generate_workflow(raw("s3-live"))
        self.sim = Sim(self.wf, now="2026-10-22T13:30", customer_id="c1")
        self.sim.account([
            {"product_id": "p1", "type": "credit_card", "card_last4": "3092", "past_due": 305.0, "days_late": 20, "minimum_payment": 106.0},
            {"product_id": "p2", "type": "personal_loan", "past_due": 1135.0, "days_late": 24, "instalment_amount": 380.0},
            {"product_id": "p3", "type": "bnpl", "merchant": "Shop", "past_due": 530.0, "days_late": 57, "next_instalment": 180.0}])
        self.sim.vs["w_id_state"] = "verified"

    def test_add_note_offered(self):
        names = [t["function"]["name"] for t in self.wf["nodes"]["negotiate"]["tools"]]
        self.assertIn("add_note", names)

    def test_three_parallel_promises(self):
        self.sim.parallel("negotiate", [("create_promise", {"product_id": p, "amount": a, "date": "2026-10-23"})
                                        for p, a in (("p1", 305), ("p2", 1135), ("p3", 530))])
        self.assertEqual(promised(self.sim.vs), ["p1", "p2", "p3"])
        cfg, _r, _m = self.sim.call("negotiate", "close_call", {"outcome": "promise"})
        self.assertEqual(cfg["body"], {"outcome": "promise"})

    def test_payment_plan_notes_then_transfer(self):
        for p in ("p1", "p2", "p3"):
            cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": p, "reason": "restructuring"})
            self.assertEqual(cfg["url"].split("/")[-1], "add_note")   # notes before the transfer are allowed
        self.sim.call("negotiate", "transfer_to_agent", {"queue": "payment_plans"})
        cfg, _r, _m = self.sim.call("negotiate", "close_call", {"outcome": "transferred", "reason": "payment_plan"})
        self.assertEqual((cfg["body"]["outcome"], cfg["body"]["reason"]), ("transferred", "payment_plan"))

    def test_partial_promise(self):
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 305, "date": "2026-10-23"})
        self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "declined"})
        self.sim.call("negotiate", "add_note", {"product_id": "p3", "reason": "already_paid"})
        route, body, _t = self.sim.close("negotiate", {"outcome": "promise"})
        self.assertEqual((route, body), ("finish", {"outcome": "partial_promise"}))

    def test_prompt_does_not_forbid_notes_before_transfer(self):
        neg = self.wf["nodes"]["negotiate"]["task_message"]
        self.assertNotIn("On a transfer send no notes.", neg)
        self.assertIn("call transfer_to_agent with queue 'payment_plans' and wait for its result", neg)


class S1Flows(unittest.TestCase):
    """fixtures/S1 live failures: a note on a single-product dispute (S1-08), notes missing (S1-10), close_call in
    parallel with add_note (S1-11)."""

    def setUp(self):
        self.wf = G.generate_workflow(raw("s1-live"))

    def sim(self, products, now="2026-12-07T09:05"):
        s = Sim(self.wf, now=now, customer_id="c1")
        s.account(products)
        s.vs["w_id_state"] = "verified"
        return s

    def test_single_product_uses_no_notes(self):
        s = self.sim([{"product_id": "p1", "type": "credit_card", "card_last4": "1", "past_due": 300.0, "days_late": 10, "minimum_payment": 90.0}])
        self.assertIn("Notes are not used", s.vs["w_notes_rule"])
        cfg, _r, _m = s.call("negotiate", "add_note", {"product_id": "p1", "reason": "dispute"})
        self.assertIn("notes-not-used", cfg["url"])
        route, body, _t = s.close("negotiate", {"outcome": "no_promise", "reason": "dispute"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "no_promise", "dispute"))

    def test_multi_product_notes_and_mapped_reason(self):
        prods = [{"product_id": "p1", "type": "credit_card", "card_last4": "1", "past_due": 190.0, "days_late": 10, "minimum_payment": 60.0},
                 {"product_id": "p2", "type": "personal_loan", "past_due": 300.0, "days_late": 10, "instalment_amount": 100.0}]
        s = self.sim(prods, now="2026-10-19T17:45")
        self.assertIn("call add_note", s.vs["w_notes_rule"])
        route, _b, todo = s.close("negotiate", {"outcome": "no_promise", "reason": "declined"})
        self.assertEqual(route, "wrap")                                   # S1-10: notes missing -> wrap_up
        s.call("wrap_up", "add_note", {"product_id": "p1", "reason": "paid"})
        s.call("wrap_up", "add_note", {"product_id": "p2", "reason": "service_error"})
        route, body, _t = s.close("wrap_up", {"outcome": "no_promise", "reason": "declined"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "no_promise", "system_error"))


class LiveDateRegressions(unittest.TestCase):
    """PUB-06 / PUB-07 from the live run, with the extractor's real policy, under two different wall clocks."""

    def facts(self, wall_day, products):
        wf = G.generate_workflow(raw("brief-a-live"))
        vs = {"now": "2026-10-14T11:00", "customer_id": "c1", "w_account_products": products}
        run(code_of(wf["nodes"]["setup"]), vs, wall_day=wall_day)
        run(code_of(wf["nodes"]["prepare_account"]), vs, wall_day=wall_day)
        return wf, vs

    def test_attempt_date_and_holiday_independent_of_wall_clock(self):
        import datetime
        card = [{"product_id": "p1", "type": "credit_card", "card_last4": "1903", "past_due": 2125.1, "days_late": 12, "minimum_payment": 637.5}]
        loan = [{"product_id": "p1", "type": "personal_loan", "past_due": 540.0, "days_late": 5, "instalment_amount": 180.0}]
        seen = []
        for wall in (datetime.date(2026, 10, 10), datetime.date(2027, 2, 3)):
            wf, vs = self.facts(wall, card)
            self.assertEqual(vs["w_today"], "2026-10-14")
            offers = json.loads(vs["w_products_json"])[0]["offers"]
            self.assertEqual((offers[0]["date"], offers[1]["date"]), ("2026-10-16", "2026-10-23"))     # PUB-06
            self.assertIn("Attempt 1: amount 2125.10 (or 637.50", vs["w_account_facts"])
            self.assertIn("on Friday 16 October 2026 (2026-10-16)", vs["w_account_facts"])
            wf, vs2 = self.facts(wall, loan)
            self.assertIn("Mon 26 Oct (holiday) -> Tuesday 27 October 2026 (2026-10-27)", vs2["w_account_facts"])  # PUB-07
            cfg = {"url": "u", "body": {"product_id": "p1", "amount": 540, "date": "2026-10-26"}}
            run(tool(wf, "negotiate", "create_promise")["pre_process_code"], vs2, "config", cfg, wall_day=wall)
            self.assertIn("not-business-day", cfg["url"])
            self.assertIn("Tuesday 27 October 2026", vs2["w_last_event"])
            seen.append((vs["w_date_facts"], vs["w_account_facts"], vs2["w_date_facts"]))
        self.assertEqual(seen[0], seen[1])


class Snippets(unittest.TestCase):
    """The generated code nodes and hooks, executed in simulated calls (Brief B policy from the extractor)."""

    def setUp(self):
        self.wf = G.generate_workflow(raw("brief-b"))
        self.sim = Sim(self.wf, customer_id="c1")
        self.prods = [
            {"product_id": "p1", "type": "credit_card", "card_last4": "6604", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0},
            {"product_id": "p2", "type": "personal_loan", "past_due": 900.0, "days_late": 20, "instalment_amount": 300.0}]

    def test_setup(self):
        vs = self.sim.vs
        self.assertEqual(vs["w_today"], "2026-10-14")
        self.assertEqual(vs["w_now_time"], "11:00")
        self.assertIn("In 2 weeks: business days Tue 27 Oct", vs["w_date_facts"])
        self.assertIn("Mon 26 Oct holiday", vs["w_date_facts"])
        self.assertIn("10:00 to 19:00", vs["w_callback_windows"])
        self.assertEqual(vs["w_last_event"], "none")

    def test_prepare_account_from_list_json_and_flat_keys(self):
        self.sim.account(self.prods)
        self.assertIn("Deadline (latest promise date): Friday 23 October 2026", self.sim.vs["w_account_facts"])
        for value in (json.dumps(self.prods), str(self.prods)):
            s = Sim(self.wf)
            s.account(value)
            self.assertEqual(s.vs["w_product_count"], "2")
        s = Sim(self.wf)
        for i, p in enumerate(self.prods):
            for k, v in p.items():
                s.vs["load_account_response.data.products.%d.%s" % (i, k)] = v
        run(code_of(self.wf["nodes"]["prepare_account"]), s.vs)
        self.assertEqual(s.vs["w_product_ids"], "p1,p2")
        s = Sim(self.wf)
        s.account([])
        self.assertEqual(s.vs["w_account_state"], "failed")

    def test_verify_flow(self):
        cfg, _r, msg = self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "14 March 1988"},
                                     {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "1988-03-14")
        self.assertIn("next security question", msg)
        self.assertNotIn("header", _r)
        cfg, _r, msg = self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "44 01"},
                                     {"ok": True, "verified": False, "wrong_answers_left": 2, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "4401")
        self.assertIn("2 wrong answer(s) left", msg)
        _c, _r, msg = self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "05/14/1988"},
                                    {"ok": True, "verified": False, "wrong_answers_left": 2, "hint": "day_month_swapped"})
        self.assertIn("day_month_swapped", msg)
        self.assertIn("month as a word", msg)
        self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "4410"},
                      {"ok": True, "verified": True, "wrong_answers_left": 2, "all_verified": True})
        self.assertEqual(self.sim.vs["w_id_state"], "verified")

    def test_identity_in_pieces(self):
        """PUB-01/03 'pieces': date of birth and digits given over several turns are joined, never counted wrong."""
        cfg, _r, msg = self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "14 March"})
        self.assertIn("partial-answer", cfg["url"])
        self.assertIn("missing part", msg)
        cfg, _r, _m = self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1988"},
                                    {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": False})
        self.assertEqual(cfg["body"]["answer"], "1988-03-14")
        cfg, _r, _m = self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "44"})
        self.assertIn("partial-answer", cfg["url"])
        cfg, _r, _m = self.sim.call("greet_verify", "verify_identity", {"field": "phone_last4", "answer": "seven one 71"[-2:]},
                                    {"ok": True, "verified": True, "wrong_answers_left": 3, "all_verified": True})
        self.assertEqual(cfg["body"]["answer"], "4471")
        # a whole new answer after a kept piece replaces it
        s = Sim(self.wf)
        s.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "12 July"})
        cfg, _r, _m = s.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "21 August 1992"})
        self.assertEqual(cfg["body"]["answer"], "1992-08-21")
        wf = G.generate_workflow(raw("brief-b"))
        self.assertIn("in pieces", wf["nodes"]["greet_verify"]["task_message"])

    def test_promise_date_year_and_weekday(self):
        self.sim.account(self.prods)
        cfg, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2025-10-30"})
        self.assertEqual(cfg["body"]["date"], "2026-10-30")
        self.assertIn("Friday 30 October 2026", msg)
        self.assertIn("In 5 weeks", self.sim.vs["w_date_facts"])         # about six weeks of calendar

    def test_callback_tomorrow_not_allowed(self):
        s = Sim(self.wf, now="2026-10-23T16:30")                          # a Friday: tomorrow is a weekend day
        self.assertIn("Tomorrow, Saturday 24 October 2026 (2026-10-24), is NOT allowed for a callback (weekend)", s.vs["w_callback_windows"])

    def test_no_verify_after_the_check_is_over(self):
        self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1990-07-21"},
                      {"ok": True, "verified": False, "wrong_answers_left": 0, "all_verified": False})
        cfg, _r, msg = self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1990-07-22"})
        self.assertIn("check-over", cfg["url"])

    def test_verify_fail_closes_not_verified(self):
        _c, _r, msg = self.sim.call("greet_verify", "verify_identity", {"field": "date_of_birth", "answer": "1990-07-21"},
                                    {"ok": True, "verified": False, "wrong_answers_left": 0, "all_verified": False})
        self.assertIn("FAILED", msg)
        self.assertIn("mobile app or a branch", msg)
        route, body, _t = self.sim.close("greet_verify", {"outcome": "not_verified", "reason": "declined"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "not_verified", "failed"))

    def test_promise_guard_and_retry(self):
        self.sim.account(self.prods)
        # after the loan's deadline: stopped before the bank; the prompt status explains and names the deadline
        cfg, res, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": "900", "date": "2026-10-30"})
        self.assertIn("not-sent-after-deadline", cfg["url"])
        self.assertIsNone(res)
        self.assertIn("NOT sent", msg)
        self.assertIn("Friday 23 October 2026", msg)
        # a holiday: next business day suggested
        cfg, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "Monday the 26th"})
        self.assertIn("2026-10-27", msg)
        # valid: date words and amount strings are normalised; the status line is reset
        cfg, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": "300.00", "date": "30 October"})
        self.assertEqual(cfg["body"], {"product_id": "p1", "amount": 300.0, "date": "2026-10-30"})
        self.assertIn("Recorded", msg)
        self.assertEqual(promised(self.sim.vs), ["p1"])
        self.assertEqual(self.sim.vs["w_last_event"], "none")
        # a second promise for the same product is stopped
        cfg, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-30"})
        self.assertIn(".invalid", cfg["url"])
        # service failure (bank answered ok=false): retry once, then give up honestly; the note is kept for the end
        _c, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-23"},
                                    {"ok": False, "error": "boom"})
        self.assertIn("Call create_promise again", msg)
        _c, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-23"},
                                    {"ok": False, "error": "boom"})
        self.assertIn("cannot be recorded", msg)
        self.assertEqual(self.sim.vs.get("w_intent_p2"), "service_error")
        # a third try is stopped in the pre-hook even when no post-hook ran
        cfg, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-23"})
        self.assertIn("no-more-retries", cfg["url"])
        self.assertEqual(promised(self.sim.vs), ["p1"])

    def test_retry_counted_without_post_hook(self):
        """HTTP failures may skip the post-hook: the pre-hook alone allows exactly one retry."""
        self.sim.account(self.prods[:1])
        t = tool(self.wf, "negotiate", "create_promise")
        urls = []
        for _ in range(3):
            cfg = {"url": "u", "body": {"product_id": "p1", "amount": 300, "date": "2026-10-16"}}
            run(t["pre_process_code"], self.sim.vs, "config", cfg)
            urls.append(cfg["url"])
        self.assertEqual(urls[:2], ["u", "u"])
        self.assertIn("no-more-retries", urls[2])

    def test_notes_and_closing(self):
        self.sim.account(self.prods)
        self.sim.vs["w_id_state"] = "verified"
        cfg, _r, msg = self.sim.call("negotiate", "add_note", {"product_id": "p1", "reason": "paid"})
        self.assertEqual(cfg["url"].split("/")[-1], "add_note")
        self.assertIn("product p2", msg)
        cfg, _r, msg = self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "dispute"})
        self.assertIn("'no_promise'", msg)
        self.assertIn("'dispute'", msg)
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "dispute"})
        self.assertIn("already-noted", cfg["url"])
        route, body, _t = self.sim.close("negotiate", {"outcome": "paid", "reason": "x"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "no_promise", "dispute"))
        self.assertEqual(self.sim.vs["w_close_done"], "yes")

    def test_missing_notes_go_to_wrap_up_once(self):
        """Notes missing at the close request: one wrap_up round with the exact products, then the call closes."""
        self.sim.account(self.prods)
        self.sim.vs["w_id_state"] = "verified"
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        route, body, todo = self.sim.close("negotiate", {"outcome": "promise"})
        self.assertEqual((route, body), ("wrap", None))
        self.assertIn("p2", todo)
        self.assertIn("add_note", todo)
        self.sim.call("wrap_up", "add_note", {"product_id": "p2", "reason": "refused"})
        route, body, _t = self.sim.close("wrap_up", {"outcome": "promise"})
        self.assertEqual((route, body), ("finish", {"outcome": "promise"}))
        # without the notes the wrap-up close is held back twice with the exact products (live FOC-NOTALK-11: the
        # model said goodbye again instead of sending the notes), then it closes anyway: never a dead end
        s = Sim(self.wf)
        s.account(self.prods)
        s.vs["w_id_state"] = "verified"
        self.assertEqual(s.close("negotiate", {"outcome": "no_promise", "reason": "refused"})[0], "wrap")
        cfg, _r, _m = s.call("wrap_up", "close_call", {"outcome": "no_promise", "reason": "refused"})
        self.assertNotEqual(s.vs.get("w_close_requested"), "yes")
        self.assertIn("p1", s.vs["w_last_event"])
        self.assertIn("p2", s.vs["w_last_event"])
        self.assertIn("add_note", s.vs["w_last_event"])
        s.call("wrap_up", "add_note", {"product_id": "p1", "reason": "refused"})
        s.call("wrap_up", "close_call", {"outcome": "no_promise", "reason": "refused"})
        self.assertNotIn("p1 (", s.vs["w_last_event"])         # only the product still missing is named
        self.assertIn("p2", s.vs["w_last_event"])
        route, body, _t, holds = s.close_until_done("wrap_up", {"outcome": "no_promise", "reason": "refused"})
        self.assertEqual((route, holds), ("finish", 0))
        # notes before the goodbye when the call ends early; in wrap-up the goodbye is not repeated
        self.assertIn("before you say goodbye", s.vs["w_notes_rule"])
        self.assertIn("ends early", s.vs["w_notes_rule"])
        self.assertIn("do not say it again", self.wf["nodes"]["wrap_up"]["task_message"])
        self.assertIn("do not say it again", self.wf["nodes"]["wrap_up"]["on_enter"]["prompt"])
        # a close request outside the wrap-up step is never held
        s2 = Sim(self.wf)
        s2.account(self.prods)
        s2.vs["w_id_state"] = "verified"
        s2.call("negotiate", "close_call", {"outcome": "no_promise", "reason": "refused"})
        self.assertEqual(s2.vs.get("w_close_requested"), "yes")

    def test_refuse_then_transfer_sends_no_note(self):
        """Refuse p1, then restructuring on p2: the transfer stops any note and decides the close; no wrap_up."""
        self.sim.account(self.prods)
        self.sim.vs["w_id_state"] = "verified"
        self.sim.call("negotiate", "transfer_to_agent", {"queue": "restructuring"})
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p1", "reason": "refused"})
        self.assertIn("not-sent-after-transfer", cfg["url"])
        route, body, _t = self.sim.close("negotiate", {"outcome": "no_promise", "reason": "refused"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "transferred", "restructuring"))
        wf = G.generate_workflow(raw("brief-b"))
        self.assertIn("{{w_notes_rule}}", wf["nodes"]["negotiate"]["task_message"])

    def test_promise_then_notes_requested(self):
        """After the last open product gets its promise, the model is told which notes to send before closing."""
        self.sim.account(self.prods)
        _c, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        self.assertIn("Products still open: p2", msg)
        s = Sim(self.wf)
        s.account(self.prods)
        s.vs["w_note_intents"] = json.dumps({"p2": "service_error"})
        _c, _r, msg = s.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        self.assertIn("product p2 with reason 'service_error'", msg)
        _c, _r, msg = s.call("negotiate", "add_note", {"product_id": "p2", "reason": "service_error"})
        self.assertIn("All notes are sent", msg)
        self.assertIn("'promise'", msg)

    def test_parallel_promises_all_kept(self):
        """S3 live: three create_promise calls in one message; a shared list variable kept only the last."""
        prods3 = self.prods + [{"product_id": "p3", "type": "car_loan", "past_due": 75.0, "days_late": 3, "instalment_amount": 25.0}]
        self.sim.account(prods3)
        self.sim.parallel("negotiate", [("create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"}),
                                        ("create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"}),
                                        ("create_promise", {"product_id": "p3", "amount": 75, "date": "2026-10-16"})])
        self.assertEqual(promised(self.sim.vs), ["p1", "p2", "p3"])
        cfg, _r, _m = self.sim.call("negotiate", "close_call", {"outcome": "promise"})
        self.assertEqual(cfg["body"], {"outcome": "promise"})          # nothing missing, no hold

    def test_parallel_transfer_and_close(self):
        """transfer_to_agent and close_call in one message: the close request is handled after the merge, so the
        engine's close_call comes after the transfer and sees it."""
        self.sim.account(self.prods[:1])
        self.sim.vs["w_id_state"] = "verified"
        outs = self.sim.parallel("negotiate", [("transfer_to_agent", {"queue": "restructuring"}),
                                               ("close_call", {"outcome": "transferred", "reason": "restructuring"})])
        self.assertIn(".invalid", outs[1][0]["url"])                 # the model's close never reaches the bank
        self.assertEqual(self.sim.vs.get("w_transfer_q_restructuring"), "restructuring")
        route, body, _t = self.sim.engine_close()
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "transferred", "restructuring"))

    def test_parallel_notes_and_close(self):
        """S1-11: add_note x2 and close_call in one message; the close is sent after both notes."""
        self.sim.account(self.prods)
        self.sim.vs["w_id_state"] = "verified"
        self.sim.parallel("negotiate", [("add_note", {"product_id": "p1", "reason": "paid"}),
                                        ("add_note", {"product_id": "p2", "reason": "refused"}),
                                        ("close_call", {"outcome": "no_promise", "reason": "refused"})])
        route, body, _t = self.sim.engine_close()
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "no_promise", "refused"))

    def test_close_needing_a_missing_fact_goes_to_wrap_up(self):
        self.sim.account(self.prods[:1])
        self.sim.vs["w_id_state"] = "verified"
        route, _b, todo = self.sim.close("negotiate", {"outcome": "transferred", "reason": "support"})
        self.assertEqual(route, "wrap")
        self.assertIn("transfer_to_agent with queue 'support'", todo)
        self.sim.call("wrap_up", "transfer_to_agent", {"queue": "support"})
        route, body, _t = self.sim.close("wrap_up", {"outcome": "transferred", "reason": "support"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "transferred", "support"))
        s = Sim(self.wf)
        s.account(self.prods[:1])
        s.vs["w_id_state"] = "verified"
        route, _b, todo = s.close("negotiate", {"outcome": "promise"})
        self.assertEqual(route, "wrap")
        self.assertIn("no promise is recorded", todo)

    def test_parallel_notes_kept(self):
        self.sim.account(self.prods)
        self.sim.vs["w_id_state"] = "verified"
        self.sim.parallel("negotiate", [("add_note", {"product_id": "p1", "reason": "paid"}),
                                        ("add_note", {"product_id": "p2", "reason": "dispute"})])
        route, body, _t = self.sim.close("negotiate", {"outcome": "paid"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "no_promise", "dispute"))

    def test_products_json_is_slim(self):
        self.sim.account(self.prods)
        self.assertLess(len(self.sim.vs["w_products_json"]), 500)
        self.assertIn("Attempt 1", self.sim.vs["w_account_facts"])

    def test_transfer_wins_and_blocks_writes(self):
        self.sim.account(self.prods)
        self.sim.vs["w_id_state"] = "verified"
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        _c, _r, msg = self.sim.call("negotiate", "transfer_to_agent", {"queue": "restructuring"})
        self.assertIn("'transferred'", msg)
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "refused"})
        self.assertIn(".invalid", cfg["url"])
        cfg, _r, _m = self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"})
        self.assertIn("not-sent-after-transfer", cfg["url"])
        route, body, _t = self.sim.close("negotiate", {"outcome": "promise"})
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "transferred", "restructuring"))

    def test_promise_closes_without_reason(self):
        self.sim.account(self.prods[:1])
        self.sim.vs["w_id_state"] = "verified"
        _c, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-14"})
        self.assertIn("You will receive a text message", msg)
        self.assertIn("close_call with outcome 'promise'", msg)
        route, body, _t = self.sim.close("negotiate", {"outcome": "promise", "reason": "{{reason}}"})
        self.assertEqual((route, body), ("finish", {"outcome": "promise"}))

    def test_callback_window(self):
        cfg, _r, msg = self.sim.call("greet_verify", "schedule_callback", {"date": "2026-10-16", "time": "11:00"})
        self.assertIn("not-sent-day-not-allowed", cfg["url"])
        self.assertIn("Ask once for another", msg)
        cfg, _r, msg = self.sim.call("greet_verify", "schedule_callback", {"date": "2026-10-15", "time": "20:00"})
        self.assertIn("last try", msg)
        self.assertIn("'no_callback'", msg)
        s = Sim(self.wf)
        self.assertIn("Thursday 15 October 2026 (2026-10-15): 10:00 to 19:00", s.vs["w_callback_windows"])
        cfg, _r, msg = s.call("greet_verify", "schedule_callback", {"date": "tomorrow", "time": "11am"})
        self.assertEqual(cfg["body"], {"date": "2026-10-15", "time": "11:00"})
        self.assertIn("'callback'", msg)
        route, body, _t = s.close("greet_verify", {"outcome": "not_reached", "reason": "unclear"})
        self.assertEqual((route, body["reason"]), ("finish", "callback"))

    def test_force_close_and_final_close(self):
        self.sim.account(self.prods[:1])
        self.sim.vs["w_id_state"] = "verified"
        run(code_of(self.wf["nodes"]["turn_cap"]), self.sim.vs)          # a step ran out of turns
        self.assertEqual(self.sim.vs["w_forced"], "yes")
        body = self.sim.finish()
        self.assertEqual(body, {"outcome": "no_promise", "reason": "refused"})
        self.assertNotIn("tools", self.wf)          # the end-of-call safety net is off by default
        wf = G.generate_workflow(raw("brief-b"), options={"final_close_fallback": True})
        fc = wf["tools"][0]
        self.assertEqual(fc["config"]["alwaysRunsAt"], ["end_of_call"])
        cfg = {"url": "u", "body": {"outcome": "", "reason": ""}}
        run(fc["pre_process_code"], dict(self.sim.vs, w_close_done="yes"), "config", cfg)
        self.assertIn(".invalid", cfg["url"])
        cfg = {"url": "u", "body": {"outcome": "", "reason": ""}}
        run(fc["pre_process_code"], dict(self.sim.vs, w_promised="p1", w_close_done="no"), "config", cfg)
        self.assertEqual((cfg["url"], cfg["body"]), ("u", {"outcome": "promise"}))
        cfg = {"url": "u", "body": {}}
        run(fc["pre_process_code"], {"now": "2026-10-14T11:00"}, "config", cfg)   # nothing known: send nothing
        self.assertIn(".invalid", cfg["url"])

    def test_hooks_survive_garbage(self):
        for node in ("greet_verify", "negotiate"):
            for t in self.wf["nodes"][node]["tools"]:
                for hk, name in (("pre_process_code", "config"), ("post_process_code", "result")):
                    if hk in t:
                        for junk in (None, "x", {}, {"body": "not json"}, {"data": None, "status": "error"}):
                            run(t[hk], {}, name, junk)
                            run(t[hk], {"w_products_json": "{bad"}, name, junk)

    def test_other_bank_flow(self):
        wf = G.generate_workflow(raw("fixture-x"))
        s = Sim(wf, now="", call_time="01.03.2027 10:00", client_ref="r1")
        self.assertEqual(s.vs["w_today"], "2027-03-01")
        s.account([{"account_ref": "A-1", "kind": "overdraft", "iban_tail": "77", "arrears": 1000, "overdue_days": 30, "min_due": 100}])
        self.assertIn("overdraft on account 77", s.vs["w_account_facts"])
        cfg, _r, _m = s.call("negotiate", "create_promise", {"account_ref": "A-1", "pay_amount": 500, "pay_date": "2027-03-05"})
        self.assertIn("not-business-day", cfg["url"])   # Friday is a weekend day here
        cfg, _r, _m = s.call("negotiate", "create_promise", {"account_ref": "A-1", "pay_amount": 500, "pay_date": "2027-03-07"})
        self.assertEqual(cfg["body"]["pay_date"], "2027-03-07")
        route, body, _t = s.close("negotiate", {"result": "no_commitment", "reason": "declined"})
        self.assertEqual((route, body["result"], "reason" in body), ("finish", "commitment", False))
        _c, _r, msg = s.call("greet_verify", "verify_identity", {"question": "birth_year", "value": "nineteen 80"},
                             {"success": True, "correct": False, "attempts_remaining": 0, "all_passed": False})
        self.assertIn("'unverified'", msg)


if __name__ == "__main__":
    unittest.main()
