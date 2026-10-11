"""v4: critical rules enforced in code instead of prompt text, with no new model-callable tool.

- negotiate's per-turn extraction labels the customer's last message (x_reply: refusal / too_much / no_talk / dispute /
  other; x_product on multi-product banks); a deterministic transition sends a labelled message to the turn_state code
  node, which counts per product, writes the next offer into w_next_action and the re-entry instruction, or closes;
- create_promise is stopped for a disputed product, add_note gets the dispute reason for it;
- schedule_callback: after max_asks stopped tries the engine closes (no_callback) with a spoken farewell.

Today in the simulated calls: Wed 14 Oct 2026 11:00 (test policies of tests/fixtures_policy).

    cd build-the-builder && python3 -m unittest tests.test_enforce_v4 -v
"""
import json
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
from builder import policy_schema as S  # noqa: E402
import test_generate as TG  # noqa: E402

OFF = {"extract_signals": False, "enforce_callback": False}
CARD_EARLY = {"product_id": "p1", "type": "credit_card", "card_last4": "6604", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0}
LOAN_LATE = {"product_id": "p2", "type": "personal_loan", "past_due": 900.0, "days_late": 20, "instalment_amount": 300.0}
CAR_LATE = {"product_id": "p3", "type": "car_loan", "past_due": 600.0, "days_late": 12, "instalment_amount": 200.0}


def state(sim, wf, reply, product=""):
    """One labelled customer message: the extraction's values, then the turn_state code node. Returns the route."""
    sim.vs["x_reply"] = reply
    sim.vs["x_product"] = product
    TG.run(TG.code_of(wf["nodes"]["turn_state"]), sim.vs)
    return sim.vs["w_turn_route"]


def route_of(wf, node, vs):
    """The node `node` routes to for these variables (first matching transition)."""
    for tr in wf["nodes"][node]["transitions"]:
        c = tr["condition"]
        if c.get("operator") == "always" or any(str(vs.get(x["variable"])) == str(x["operand"]) for x in c["conditions"]):
            return tr["target_node"]


def close_path(wf, vs):
    """Nodes from finish to the end node: a code-decided close says goodbye (tts) BEFORE close_api sends close_call."""
    path, n = [], "finish"
    while wf["nodes"][n]["node_type"] != "end_call":
        n = route_of(wf, n, vs)
        path.append(n)
    return path


class Structure(unittest.TestCase):
    def test_every_policy_generates_lint_clean_with_and_without(self):
        pols = [TG.raw(n) for n in TG.ALL] + [json.loads(p.read_text()) for p in sorted((KIT / "policies").glob("*.policy.json"))]
        oa = json.loads((KIT / "tools" / "openapi.json").read_text())
        for pol in pols:
            for opts in (None, OFF):
                wf = G.generate_workflow(pol, None, oa, opts)
                self.assertEqual(G.lint_workflow(wf), [], opts)
                for n in wf["nodes"].values():
                    for t in (n.get("tools") or []):
                        for hk in ("pre_process_code", "post_process_code"):
                            self.assertLess(len(t.get(hk, "")), 23000, (t["function"]["name"], hk))

    def test_negotiate_extraction_and_routing(self):
        wf = G.generate_workflow(TG.raw("brief-b"))
        neg = wf["nodes"]["negotiate"]
        ex = {e["name"]: e for e in neg["extraction_schema"]}
        self.assertEqual(ex["x_reply"]["enum_values"], ["refusal", "too_much", "no_talk", "dispute", "other"])
        self.assertEqual(ex["x_reply"]["type"], "string")            # not a boolean: equals compares strings
        self.assertIn("x_product", ex)                                # several products possible
        for e in ex.values():
            self.assertIn("min_confidence", e)
        tr = neg["transitions"]
        self.assertEqual(tr[0]["target_node"], "close_check")         # the model's own close request first
        self.assertEqual(tr[1]["target_node"], "turn_state")
        self.assertEqual(sorted(c["operand"] for c in tr[1]["condition"]["conditions"]), ["dispute", "no_talk", "refusal", "too_much"])
        self.assertNotIn("matches_intent", json.dumps(wf))           # no routing-model call added
        # re-entry from turn_state runs on_enter again; its text is a variable
        self.assertEqual(neg["on_enter"], {"type": "llm", "prompt": "{{w_enter_prompt}}"})
        self.assertFalse(neg["on_enter_first_entry_only"])
        ts = wf["nodes"]["turn_state"]
        self.assertEqual([t["target_node"] for t in ts["transitions"]], ["close_check", "turn_cap", "negotiate"])
        self.assertTrue(ts["passthrough"])
        # no model-callable tool was added
        self.assertEqual(sorted(t["function"]["name"] for t in neg["tools"]),
                         sorted(t["function"]["name"] for t in G.generate_workflow(TG.raw("brief-b"), options=OFF)["nodes"]["negotiate"]["tools"]))
        single = G.generate_workflow(TG.raw("brief-a"))["nodes"]["negotiate"]["extraction_schema"]
        self.assertEqual([e["name"] for e in single], ["x_reply"])   # one product: no product label

    def test_options_off_restore_v3(self):
        wf = G.generate_workflow(TG.raw("brief-b"), options=OFF)
        neg = wf["nodes"]["negotiate"]
        self.assertNotIn("extraction_schema", neg)
        self.assertNotIn("turn_state", wf["nodes"])
        self.assertNotIn("end_said", wf["nodes"])
        self.assertTrue(neg["on_enter_first_entry_only"])
        self.assertIn("State the debt", neg["on_enter"]["prompt"])
        self.assertNotIn("w_cb_over", json.dumps(wf["nodes"]["greet_verify"]["transitions"]))

    def test_policy_values_not_brief_values(self):
        """Counts, outcomes and note reasons come from the policy: S3 names its codes differently."""
        p = S.normalise(json.loads((KIT / "policies" / "S3.policy.json").read_text()))
        ef = G.enforce_rt(p, G.DEFAULT_OPTIONS)
        self.assertEqual(ef["refused"], {"outcome": p["closing"]["refused"]["outcome"], "reason": p["closing"]["refused"]["reason"] or ""})
        self.assertEqual(ef["refused_note"], "declined")
        if ef["dispute_on"]:
            self.assertEqual(ef["dispute_note"], "disputed")
        b = S.normalise(TG.raw("brief-b"))
        ef = G.enforce_rt(b, G.DEFAULT_OPTIONS)
        self.assertEqual((ef["talk_count"], ef["talk"], ef["talk_note"]), (2, {"outcome": "no_promise", "reason": "refused"}, "refused"))
        self.assertEqual((ef["dispute"], ef["dispute_note"]), ({"outcome": "no_promise", "reason": "dispute"}, "dispute"))
        self.assertEqual(ef["no_callback"], {"outcome": "not_reached", "reason": "no_callback"})
        # a 'three times' rule counts to three
        q = TG.raw("brief-b")
        for t in q["triggers"]:
            if t["name"] == "refuses_to_talk":
                t["detect"] = t["detect"].replace("twice", "three times")
        self.assertEqual(G.enforce_rt(S.normalise(q), G.DEFAULT_OPTIONS)["talk_count"], 3)
        self.assertEqual(G.enforce_rt(b, OFF)["signals"], [])

    def test_prompts_shorter_where_code_enforces(self):
        for name in ("brief-a", "brief-b", "brief-a-live"):
            on = G.generate_workflow(TG.raw(name))["nodes"]
            off = G.generate_workflow(TG.raw(name), options=OFF)["nodes"]
            self.assertLess(len(on["negotiate"]["task_message"]), len(off["negotiate"]["task_message"]), name)
            self.assertLess(len(on["greet_verify"]["task_message"]), len(off["greet_verify"]["task_message"]), name)
        s = TG.Sim(G.generate_workflow(TG.raw("brief-a")), customer_id="c1")
        s.account([LOAN_LATE])
        self.assertIn("Persuasion attempts allowed: 2; never repeat an offer that was refused.", s.vs["w_account_facts"])
        self.assertIn("Attempt 2: amount 300.00 on Friday 23 October 2026", s.vs["w_account_facts"])
        self.assertIn("Floor (lowest amount that can be recorded): 300.00. Offer it only when a tool result or the state line says so",
                      s.vs["w_account_facts"])
        self.assertNotIn("After attempt 2 is refused", s.vs["w_account_facts"])
        s = TG.Sim(G.generate_workflow(TG.raw("brief-a"), options=OFF), customer_id="c1")
        s.account([LOAN_LATE])
        self.assertIn("After attempt 2 is refused", s.vs["w_account_facts"])


class ReviewFixes(unittest.TestCase):
    def test_labels_tie_break_and_confidence(self):
        """Review 2: a message that also fits 'other' (hardship, a question, a counter-offer, 'no, but ...') or a dispute
        is labelled that, never refusal / no_talk; a doubtful label (below 0.8) is dropped."""
        ex = G.generate_workflow(TG.raw("brief-b"))["nodes"]["negotiate"]["extraction_schema"][0]
        self.assertGreaterEqual(ex["min_confidence"], 0.8)
        d = ex["description"]
        self.assertIn("Tie-break: a message that fits dispute or other in any part is labelled that, never refusal or too_much or no_talk", d)
        for x in ("'no, I lost my job'", "'I can do 200 on Friday'", "'no, but next week'", "When unsure, other."):
            self.assertIn(x, d)

    def test_turn_cap_survives_re_entry(self):
        """Review 4: each re-entry resets negotiate's turn counter; turn_state sums the turns and routes to turn_cap."""
        wf = G.generate_workflow(TG.raw("brief-a"), options={"negotiate_max_turns": 6})
        ts = wf["nodes"]["turn_state"]
        self.assertIn({"target_node": "turn_cap", "description": "negotiate turns (summed over re-entries) over the cap",
                       "condition": {"logic": "or", "conditions": [{"variable": "w_turn_route", "operator": "equals", "operand": "cap"}]}},
                      ts["transitions"])
        self.assertEqual(wf["nodes"]["turn_cap"]["transitions"][-1]["target_node"], "close_check")
        sim = TG.Sim(wf, customer_id="c1")
        sim.vs["w_id_state"] = "verified"
        sim.account([LOAN_LATE])
        sim.vs["negotiate.current_turns"] = 3                     # turns in the node since the last entry
        self.assertEqual(state(sim, wf, "too_much"), "stay")       # 3
        sim.vs["negotiate.current_turns"] = 1
        self.assertEqual(state(sim, wf, "other"), "stay")          # 4
        sim.vs.pop("negotiate.current_turns")
        self.assertEqual(state(sim, wf, "other"), "stay")          # 5 (a hop counts at least one turn)
        self.assertEqual(state(sim, wf, "other"), "cap")           # 6: the cap is reached
        self.assertEqual(sim.vs["w_neg_turns"], "6")


class Ladder(unittest.TestCase):
    def setUp(self):
        self.wf = G.generate_workflow(TG.raw("brief-a"))
        self.sim = TG.Sim(self.wf, customer_id="c1")
        self.sim.vs["w_id_state"] = "verified"

    def test_first_entry_states_the_debt(self):
        self.sim.account([LOAN_LATE])
        self.assertIn("State the debt", self.sim.vs["w_enter_prompt"])

    def test_late_band_two_attempts_then_close(self):
        self.sim.account([LOAN_LATE])
        self.assertEqual(state(self.sim, self.wf, "refusal"), "stay")          # no to "can you pay today?"
        hint = self.sim.vs["w_next_action"]
        self.assertIn("Refusal 1 for p2", hint)
        self.assertIn("make attempt 1 of 2 only: 900.00 on Friday 16 October 2026", hint)
        self.assertEqual(self.sim.vs["x_reply"], "none")                       # cleared: a tool result does not count it again
        self.assertIn(hint, self.sim.vs["w_enter_prompt"])
        self.assertTrue(self.sim.vs["w_enter_prompt"].startswith("Reply to the customer's last message now"))
        self.assertEqual(state(self.sim, self.wf, "refusal"), "stay")
        self.assertIn("make attempt 2 of 2 only: 300.00 on Friday 23 October 2026", self.sim.vs["w_next_action"])
        self.assertEqual(state(self.sim, self.wf, "refusal"), "close")         # attempt 2 refused: ladder used up
        self.assertEqual((self.sim.vs["w_close_req_outcome"], self.sim.vs["w_close_req_reason"]), ("no_promise", "refused"))
        self.assertEqual(self.sim.vs["w_end_say"], "yes")
        route, body, _t = self.sim.engine_close()
        self.assertEqual((route, body), ("finish", {"outcome": "no_promise", "reason": "refused"}))
        # rule 47: goodbye before close_call -> the farewell node speaks before close_api, the end node is silent
        self.assertEqual(close_path(self.wf, self.sim.vs), ["farewell", "close_api", "end_closed"])
        fw = self.wf["nodes"]["farewell"]
        self.assertEqual(fw["on_enter"]["type"], "tts")
        self.assertTrue(fw["on_enter"]["text"])
        self.assertTrue(fw["passthrough"])
        self.assertEqual(self.wf["nodes"]["end_closed"]["terminal"]["function"]["parameters"]["properties"]["message_mode"]["default"], "none")
        self.assertNotIn("end_said", self.wf["nodes"])
        # a close the model asked for itself (it said goodbye) does not get a second farewell
        self.assertEqual(close_path(self.wf, dict(self.sim.vs, w_end_say="no")), ["close_api", "end_closed"])

    def test_early_band_one_attempt(self):
        self.sim.account([CARD_EARLY])
        state(self.sim, self.wf, "refusal")
        self.assertIn("attempt 1 of 1 only: 300.00 on Friday 16 October 2026", self.sim.vs["w_next_action"])
        self.assertEqual(state(self.sim, self.wf, "refusal"), "close")          # MIX-09: no floor after a plain no

    def test_too_much_makes_the_floor_the_offer(self):
        self.sim.account([CARD_EARLY])
        self.assertEqual(state(self.sim, self.wf, "too_much"), "stay")
        self.assertIn("offer the floor, 90.00, once", self.sim.vs["w_next_action"])
        self.assertEqual(self.sim.vs.get("w_refusals_p1"), None)               # 'too much' is not a refusal
        state(self.sim, self.wf, "refusal")                                     # the floor refused: a flat refusal
        self.assertIn("attempt 1 of 1 only: 90.00 on Friday 16 October 2026", self.sim.vs["w_next_action"])
        state(self.sim, self.wf, "too_much")                                    # second 'too much': counted as a refusal
        self.assertEqual(self.sim.vs["w_turn_route"], "close")

    def test_events_after_every_product_is_handled_are_ignored(self):
        self.sim.account([CARD_EARLY])
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        self.assertEqual(state(self.sim, self.wf, "refusal"), "stay")           # 'no' to 'anything else?'
        self.assertIsNone(self.sim.vs.get("w_refusals_p1"))
        self.assertIn("All products handled", self.sim.vs["w_next_action"])


class NoTalkAndDispute(unittest.TestCase):
    def setUp(self):
        self.wf = G.generate_workflow(TG.raw("brief-a"))
        self.sim = TG.Sim(self.wf, customer_id="c1")
        self.sim.vs["w_id_state"] = "verified"
        self.sim.account([LOAN_LATE])

    def test_second_no_talk_closes(self):
        self.assertEqual(state(self.sim, self.wf, "no_talk"), "stay")
        hint = self.sim.vs["w_next_action"]
        self.assertIn("said 1 time(s) that they do not want to talk (the call ends at 2)", hint)
        self.assertIn("acknowledge it in one sentence; then make attempt 1 of 2", hint)
        state(self.sim, self.wf, "other")                                       # not routed in the engine; harmless here
        self.assertEqual(state(self.sim, self.wf, "no_talk"), "close")         # GEN-09-no-talk
        route, body, _t = self.sim.engine_close()
        self.assertEqual(body, {"outcome": "no_promise", "reason": "refused"})

    def test_dispute_blocks_the_promise(self):
        self.assertEqual(state(self.sim, self.wf, "dispute"), "stay")
        self.assertIn("disputes p2", self.sim.vs["w_next_action"])
        self.assertIn("close_call with outcome 'no_promise' and reason 'dispute'", self.sim.vs["w_next_action"])
        cfg, _r, msg = self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"})
        self.assertIn("not-sent-disputed", cfg["url"])
        self.assertIn("Do not argue and take no promise", msg)
        route, body, _t = self.sim.close("negotiate", {"outcome": "no_promise", "reason": "dispute"})
        self.assertEqual(body, {"outcome": "no_promise", "reason": "dispute"})


class MultiProduct(unittest.TestCase):
    def setUp(self):
        self.wf = G.generate_workflow(TG.raw("brief-b"))
        self.sim = TG.Sim(self.wf, customer_id="c1")
        self.sim.vs["w_id_state"] = "verified"
        self.sim.account([CARD_EARLY, LOAN_LATE, CAR_LATE])

    def test_ladder_per_product_then_next_product(self):
        state(self.sim, self.wf, "refusal", "the card")
        state(self.sim, self.wf, "refusal", "card")
        self.assertEqual(self.sim.vs["w_turn_route"], "stay")                  # other products are still open
        self.assertIn("All 1 offer(s) for p1 were refused", self.sim.vs["w_next_action"])
        # review 1: moving on IS the next product's attempt 1 (the first payment question was already asked)
        self.assertIn("continue with p2 (personal loan): make its attempt 1 of 2 only: 900.00 on Friday 16 October 2026",
                      self.sim.vs["w_next_action"])
        self.assertEqual(self.sim.vs["w_intent_p1"], "refused")
        state(self.sim, self.wf, "refusal", "loan")                            # 'loan' fits two: the current product
        self.assertEqual(self.sim.vs["w_refusals_p2"], "1")
        self.assertIn("Refusal 1 for p2", self.sim.vs["w_next_action"])
        self.assertIn("make attempt 2 of 2 only: 300.00 on Friday 23 October 2026", self.sim.vs["w_next_action"])
        state(self.sim, self.wf, "refusal", "car loan")                        # the product the customer named
        self.assertEqual(self.sim.vs["w_refusals_p3"], "1")
        self.assertIn("make attempt 1 of 2 only: 600.00", self.sim.vs["w_next_action"])

    def test_dispute_on_one_product_forces_the_note_reason(self):
        state(self.sim, self.wf, "dispute", "personal loan")
        self.assertEqual(self.sim.vs["w_dispute_p2"], "yes")
        self.assertIn("continue with p1 (credit card ending 6604): make its attempt 1 of 1 only: 300.00", self.sim.vs["w_next_action"])
        state(self.sim, self.wf, "refusal", "card")                            # its attempt 1 refused: p1 is used up
        self.assertEqual(self.sim.vs["w_intent_p1"], "refused")
        cfg, _r, _m = self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"})
        self.assertIn("not-sent-disputed", cfg["url"])
        cfg, _r, _m = self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        self.assertNotIn("disputed", cfg["url"])                               # other products are not affected
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p2", "reason": "refused"})
        self.assertEqual(cfg["body"]["reason"], "dispute")
        cfg, _r, _m = self.sim.call("negotiate", "add_note", {"product_id": "p3", "reason": "refused"})
        self.assertEqual(cfg["body"]["reason"], "refused")

    def test_unclear_dispute_with_several_open_products_asks(self):
        state(self.sim, self.wf, "dispute", "")
        self.assertFalse([k for k in self.sim.vs if k.startswith("w_dispute_") or k.startswith("w_intent_")])
        self.assertIn("ask which product they mean", self.sim.vs["w_next_action"])
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        self.sim.call("negotiate", "create_promise", {"product_id": "p2", "amount": 900, "date": "2026-10-16"})
        state(self.sim, self.wf, "dispute", "")                                # one product left open: it is that one
        self.assertEqual(self.sim.vs["w_dispute_p3"], "yes")

    def test_dispute_of_a_promised_product_changes_nothing(self):
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        state(self.sim, self.wf, "dispute", "the card")
        self.assertFalse([k for k in self.sim.vs if k.startswith("w_dispute_")])

    def test_all_refused_closes_with_notes_first(self):
        # p1: the first payment question + attempt 1; p2 and p3 start at attempt 1 (review 1): two refusals each
        for pname, n in (("card", 2), ("personal loan", 2), ("car loan", 2)):
            for _ in range(n):
                state(self.sim, self.wf, "refusal", pname)
        self.assertEqual(self.sim.vs["w_turn_route"], "close")
        route, _b, todo = self.sim.engine_close()
        self.assertEqual(route, "wrap")                                        # notes are sent before the close
        for pid in ("p1", "p2", "p3"):
            self.assertIn(pid + " (", todo)
        self.assertIn("reason 'refused'", todo)

    def test_second_no_talk_marks_open_products(self):
        self.sim.call("negotiate", "create_promise", {"product_id": "p1", "amount": 300, "date": "2026-10-16"})
        state(self.sim, self.wf, "no_talk")
        self.assertEqual(state(self.sim, self.wf, "no_talk"), "close")
        self.assertEqual((self.sim.vs["w_intent_p2"], self.sim.vs["w_intent_p3"]), ("refused", "refused"))
        self.assertNotIn("w_intent_p1", self.sim.vs)


class Callback(unittest.TestCase):
    def test_last_try_outside_the_window_closes_in_the_engine(self):
        wf = G.generate_workflow(TG.raw("brief-b"))
        tr = [t for t in wf["nodes"]["greet_verify"]["transitions"] if t["target_node"] == "close_check"]
        self.assertEqual(len(tr), 2)
        self.assertEqual(tr[1]["condition"]["conditions"][0], {"variable": "w_cb_over", "operator": "equals", "operand": "yes"})
        s = TG.Sim(wf)
        cfg, _r, msg = s.call("greet_verify", "schedule_callback", {"date": "2026-10-16", "time": "11:00"})
        self.assertIn("Ask once for another", msg)
        self.assertEqual(s.vs["w_cb_over"], "no")
        cfg, _r, msg = s.call("greet_verify", "schedule_callback", {"date": "2026-10-15", "time": "20:00"})
        self.assertEqual(s.vs["w_cb_over"], "yes")                              # EDGE-20: no third ask
        route, body, _t = s.engine_close()
        self.assertEqual((route, body["outcome"], body["reason"]), ("finish", "not_reached", "no_callback"))
        self.assertEqual(close_path(wf, s.vs), ["farewell", "close_api", "end_closed"])

    def test_off(self):
        wf = G.generate_workflow(TG.raw("brief-b"), options=OFF)
        s = TG.Sim(wf)
        s.call("greet_verify", "schedule_callback", {"date": "2026-10-16", "time": "11:00"})
        s.call("greet_verify", "schedule_callback", {"date": "2026-10-15", "time": "20:00"})
        self.assertNotEqual(s.vs.get("w_cb_over"), "yes")


class Helpers(unittest.TestCase):
    def test_match_product(self):
        src = "\n".join(c for _n, c, _r, _i in G._library() if "def match_product" in c)
        g = {"re": __import__("re")}
        exec(src, g)
        mp = g["match_product"]
        prods = [{"id": "p1", "name": "credit card ending 6604", "type": "credit_card"},
                 {"id": "p2", "name": "personal loan", "type": "personal_loan"},
                 {"id": "p3", "name": "car loan", "type": "car_loan"}]
        self.assertEqual(mp(prods, "the card")["id"], "p1")
        self.assertEqual(mp(prods, "6604")["id"], "p1")
        self.assertEqual(mp(prods, "my car loan")["id"], "p3")
        self.assertIsNone(mp(prods, "the loan"))
        self.assertIsNone(mp(prods, ""))
        self.assertIsNone(mp(prods, "the"))


if __name__ == "__main__":
    unittest.main()
