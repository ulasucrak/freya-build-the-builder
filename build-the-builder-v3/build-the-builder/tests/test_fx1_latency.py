"""FX1 (latency): negotiate's first message as a fixed line built by prepare_account (no model call), the short-reply
rule, the reasoning_override option (off by default), the farewell before close_call on the turn-cap path (rule 47)
and call variables defaulted in setup (G8).

    cd build-the-builder && python3 -m unittest tests.test_fx1_latency -v
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
from builder import policy_schema as S  # noqa: E402
import test_generate as TG  # noqa: E402

CARD = {"product_id": "p1", "type": "credit_card", "card_last4": "6604", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0}
LOAN = {"product_id": "p2", "type": "personal_loan", "past_due": 912.5, "days_late": 1, "instalment_amount": 300.0}
CAR = {"product_id": "p3", "type": "car_loan", "past_due": 600.0, "days_late": 12, "instalment_amount": 200.0}
OA = json.loads((KIT / "tools" / "openapi.json").read_text())
POLICIES = [(n, TG.raw(n)) for n in TG.ALL] + [(p.name, json.loads(p.read_text())) for p in sorted((KIT / "policies").glob("*.policy.json"))]


def entry_after(wf, products):
    s = TG.Sim(wf, customer_id="c1", first_name="Ada", last_name="Lovelace", title="Ms")
    s.account(products)
    return s


def route_of(wf, node, vs):
    for tr in wf["nodes"][node]["transitions"]:
        c = tr["condition"]
        if c.get("operator") == "always":
            return tr["target_node"]
        if any(str(vs.get(x["variable"])) == x["operand"] for x in c.get("conditions") or []):
            return tr["target_node"]
    return None


class StaticEntry(unittest.TestCase):
    def test_single_product_line(self):
        wf = G.generate_workflow(TG.raw("brief-a"))
        neg = wf["nodes"]["negotiate"]
        self.assertEqual(neg["on_enter"], {"type": "tts", "text": "{{w_entry_line}}"})
        self.assertTrue(neg["on_enter_first_entry_only"])
        self.assertIn(G.ENTRY_SAID, neg["task_message"])
        s = entry_after(wf, [CARD])
        self.assertEqual(s.vs["w_entry_line"], "I am calling from the collections team about a late payment. Your credit card ending 6604"
                                               " is 4 days late, and the past-due amount is 300. Can you make this payment today?")
        self.assertIn("already said", s.vs["w_next_action"])

    def test_multi_product_one_sentence_each_then_the_all_question(self):
        wf = G.generate_workflow(TG.raw("brief-b"))
        s = entry_after(wf, [CARD, LOAN, CAR])
        line = s.vs["w_entry_line"]
        self.assertEqual(line, "I am calling from the collections team about a late payment."
                               " Your credit card ending 6604 is 4 days late, and the past-due amount is 300."
                               " Your personal loan is 1 day late, and the past-due amount is 912.50."
                               " Your car loan is 12 days late, and the past-due amount is 600."
                               " Can you pay all of them today?")
        s = entry_after(wf, [LOAN])          # one product on a multi-product brief: the one-product question
        self.assertTrue(s.vs["w_entry_line"].endswith("Can you make this payment today?"))

    def test_every_policy_builds_a_safe_line(self):
        for name, pol in POLICIES:
            wf = G.generate_workflow(pol, None, OA)
            self.assertEqual(G.lint_workflow(wf), [], name)
            neg = wf["nodes"]["negotiate"]
            entry = G.static_entry(S.normalise(pol))
            if not entry:
                self.assertEqual(neg["on_enter"]["type"], "llm", name)
                continue
            self.assertEqual(neg["on_enter"]["type"], "tts", name)
            for prods in ([CARD], [CARD, LOAN], [dict(CARD, days_late=None, past_due=None)], [dict(CARD, card_last4=None)], []):
                s = entry_after(wf, prods)
                line = s.vs["w_entry_line"]
                self.assertTrue(line.strip(), (name, prods))
                self.assertNotRegex(line, r"[{}]|\bNone\b|\bnan\b", (name, prods))
                if prods:
                    self.assertTrue(line.endswith("?"), (name, line))
                    self.assertEqual(line.count("Your "), len(prods), (name, line))

    def test_brief_values_come_from_the_policy(self):
        for name, intro, one, many in (("S3.policy.json", "I am calling about late payments.", "Could you pay this today?", "Could you pay everything today?"),
                                       ("S6.policy.json", "", "Would you be able to clear this today?", "Would you be able to clear all of it today?")):
            e = G.static_entry(S.normalise(json.loads((KIT / "policies" / name).read_text())))
            self.assertEqual((e["intro"], e["one"], e["many"]), (intro, one, many), name)
        self.assertEqual(G.static_entry(S.normalise(json.loads((KIT / "policies" / "S6.policy.json").read_text())))["late"], "in arrears")

    def test_not_fixed_keeps_the_model(self):
        p = TG.raw("brief-b")
        p["phrases"] = {k: v for k, v in p["phrases"].items() if "today" not in str(v).lower()}
        self.assertIsNone(G.static_entry(S.normalise(p)))          # no quoted first question: the model says it
        wf = G.generate_workflow(p)
        self.assertEqual(wf["nodes"]["negotiate"]["on_enter"], {"type": "llm", "prompt": G.NEGOTIATE_ENTER})
        self.assertNotIn("w_entry_line", wf["nodes"]["negotiate"]["task_message"])
        for opts in ({"static_entry": False}, {"extract_signals": True}):
            wf = G.generate_workflow(TG.raw("brief-b"), options=opts)
            self.assertEqual(wf["nodes"]["negotiate"]["on_enter"]["type"], "llm", opts)
        wf = G.generate_workflow(TG.raw("brief-b"), prompts={"negotiate.on_enter": "Say the debt."})
        self.assertEqual(wf["nodes"]["negotiate"]["on_enter"], {"type": "llm", "prompt": "Say the debt."})

    def test_failed_account_never_speaks_a_placeholder(self):
        wf = G.generate_workflow(TG.raw("brief-a"))
        s = entry_after(wf, [])
        self.assertEqual(s.vs["w_account_state"], "failed")
        self.assertNotIn("{", s.vs["w_entry_line"])
        self.assertTrue(s.vs["w_entry_line"])
        # setup alone already gives the variable a value
        s = TG.Sim(wf, customer_id="c1")
        self.assertEqual(s.vs["w_entry_line"], "")

    def test_entry_only_after_verification(self):
        """negotiate (the only node that speaks the line) is reached only through load_account -> prepare_account."""
        wf = G.generate_workflow(TG.raw("brief-b"))
        into = [k for k, n in wf["nodes"].items() for t in n.get("transitions") or [] if t["target_node"] == "negotiate"]
        self.assertEqual(sorted(set(into)), ["prepare_account"])
        into = [k for k, n in wf["nodes"].items() for t in n.get("transitions") or [] if t["target_node"] == "load_account"]
        self.assertEqual(into, ["greet_verify"])


class ShortReplies(unittest.TestCase):
    def test_system_prompt_caps_reply_length(self):
        for name, pol in POLICIES:
            sysp = G.generate_workflow(pol, None, OA)["system_prompt"]
            self.assertIn("one or two short sentences (max 120 chars)", sysp, name)
            self.assertIn("no repeats but the read-back", sysp, name)
            self.assertNotIn("one to three", sysp.splitlines()[0], name)

    def test_next_action_after_the_entry_is_short(self):
        wf = G.generate_workflow(TG.raw("brief-b"))
        s = entry_after(wf, [CARD, LOAN, CAR])
        self.assertLess(len(s.vs["w_next_action"]), 120)


class ReasoningOverride(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(G.DEFAULT_OPTIONS["reasoning_off"])
        wf = G.generate_workflow(TG.raw("brief-b"))
        self.assertNotIn("reasoning_override", json.dumps(wf))

    def test_option_disables_reasoning_on_conversation_nodes_only(self):
        wf = G.generate_workflow(TG.raw("brief-b"), options={"reasoning_off": True})
        self.assertEqual(G.lint_workflow(wf), [])
        for k, n in wf["nodes"].items():
            if n["node_type"] == "conversation":
                self.assertEqual(n["reasoning_override"], "disabled", k)
            else:
                self.assertNotIn("reasoning_override", n, k)


class FarewellBeforeClose(unittest.TestCase):
    """Rule 47 (thank and say goodbye before close_call) on the turn-cap path too."""

    def test_turn_cap_says_goodbye_before_close_api(self):
        for opts in (None, {"enforce_callback": False, "extract_signals": False}):
            wf = G.generate_workflow(TG.raw("brief-a"), options=opts)
            s = entry_after(wf, [CARD])
            s.vs["w_id_state"] = "verified"
            TG.run(TG.code_of(wf["nodes"]["turn_cap"]), s.vs)
            TG.run(TG.code_of(wf["nodes"]["close_check"]), s.vs)
            TG.run(TG.code_of(wf["nodes"]["finish"]), s.vs)
            path, n = [], "finish"
            while wf["nodes"][n]["node_type"] != "end_call":
                n = route_of(wf, n, s.vs)
                path.append(n)
            self.assertEqual(path, ["farewell", "close_api", "end_forced"], opts)
            self.assertEqual(wf["nodes"]["farewell"]["on_enter"]["type"], "tts")
            props = wf["nodes"]["end_forced"]["terminal"]["function"]["parameters"]["properties"]
            self.assertEqual(props["message_mode"]["default"], "none")          # nothing said after close_call
            self.assertNotIn("spoken_message", props)

    def test_model_close_gets_no_second_farewell(self):
        wf = G.generate_workflow(TG.raw("brief-a"))
        s = entry_after(wf, [CARD])
        s.vs["w_id_state"] = "verified"
        s.close("negotiate", {"outcome": "no_promise", "reason": "refused"})
        self.assertEqual(route_of(wf, "finish", s.vs), "close_api")


class CallVariableDefaults(unittest.TestCase):
    """G8: a call variable the call did not pass is "" (never a literal {{first_name}})."""

    def test_missing_call_variables_are_empty(self):
        for name in ("brief-a", "s3-live"):
            wf = G.generate_workflow(TG.raw(name))
            s = TG.Sim(wf, customer_id="c1")
            for v in ("first_name", "last_name", "title"):
                self.assertEqual(s.vs[v], "", (name, v))
            s = TG.Sim(wf, customer_id="c1", first_name="Ada", title="Ms")
            self.assertEqual((s.vs["first_name"], s.vs["title"], s.vs["last_name"]), ("Ada", "Ms", ""))

    def test_tool_fields_and_clock_are_left_alone(self):
        for name, pol in POLICIES:
            p = S.normalise(pol)
            wf = G.generate_workflow(pol, None, OA)
            code = TG.code_of(wf["nodes"]["setup"])
            m = re.search(r'"call_vars":(\[[^\]]*\])', code)
            got = json.loads(m.group(1)) if m else []
            fields = {f for t in p["tools"].values() for f in ((t or {}).get("fields") or {})}
            self.assertFalse(set(got) & fields, name)
            self.assertNotIn(p["clock"]["now_variable"], got, name)
            s = TG.Sim(wf)
            self.assertNotIn("customer_id", s.vs, name)


if __name__ == "__main__":
    unittest.main()
