"""Offline tests of tools/run_scenarios.py: variable mapping, persona text, verdict/row parsing, summary. No network."""
import io, json, pathlib, sys, tempfile, unittest
from contextlib import redirect_stdout
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import run_scenarios as rs

TR = {"id": "T-01", "family": "Ret", "persona": "Sen Ayşe Yılmaz'sın. Borcunu ödemek istemiyorsun.",
      "criteria": ["Ajan kibar kalır", "Ajan  ödeme\ntarihi sorar"], "customerNumber": "+905551112233", "now": "2026-10-14T11:00",
      "variables": {"customer_id": "c_1", "amount": 1250.5, "vip": True, "now": "2026-10-15T09:00", "none": None, "items": [1, 2]},
      "nested": {"x": 1}, "nothing": None, "count": 3}
EN = {"id": "E-01", "persona": "You are Dana. You pay today.", "criteria": "The agent confirms the payment."}


def _call(user_t=2.0, end_epoch_ms=1005500, vt=None, route_vt=None, turns=True):
    """A small call: setup event at t=0 (epoch 1000 s), user at 2.0 s, assistant finishing at epoch 1005.5 s -> 3.5 s turn."""
    ms = [{"role": "system", "message": json.dumps({"event": "node_enter", "node": "greet"}), "messageId": "msg_0_1000000", "secondsFromStart": 0}]
    if route_vt is not None:
        ms.append({"role": "system", "message": json.dumps({"event": "route_evaluated", "variable_table": route_vt}), "messageId": "msg_1_1000500", "secondsFromStart": 0.5})
    if turns:
        ms.append({"role": "user", "message": "Merhaba?", "messageId": "msg_2_1002000", "secondsFromStart": user_t})
        ms.append({"role": "system", "message": json.dumps({"event": "function_call_start", "function": "get_account", "args": {"params": {"customer_id": "c_1"}, "code": "x"}}), "messageId": "msg_3_1002500", "secondsFromStart": 2.5})
        ms.append({"role": "system", "message": json.dumps({"event": "function_call_complete", "function": "get_account", "result": {"data": {"balance": 10}}}), "messageId": "msg_4_1003000", "secondsFromStart": 3.0})
        a = {"role": "assistant", "message": "İyi günler.", "messageId": f"msg_5_{end_epoch_ms}", "secondsFromStart": 3.2}
        if vt is not None: a["workflowSnapshot"] = {"variableTable": vt}
        ms.append(a)
    return {"id": "call-1", "endedReason": "customer-ended-call", "messages": ms}


class Variables(unittest.TestCase):
    def test_top_level_scalars_and_variables(self):
        v = rs.call_variables(TR)
        self.assertEqual(v["customerNumber"], "+905551112233")
        self.assertEqual(v["now"], "2026-10-15T09:00")                     # "variables" wins on a clash
        self.assertEqual(v["customer_id"], "c_1"); self.assertEqual(v["amount"], "1250.5"); self.assertEqual(v["vip"], "true")
        self.assertEqual(v["count"], "3"); self.assertEqual(v["items"], "[1,2]")
        for k in ("id", "family", "persona", "criteria", "variables", "nested", "nothing", "none"): self.assertNotIn(k, v)
        self.assertTrue(all(isinstance(x, str) for x in v.values()))

    def test_no_variables(self):
        self.assertEqual(rs.call_variables(EN), {})
        self.assertEqual(rs.call_variables(dict(EN, now="2026-10-14T11:00")), {"now": "2026-10-14T11:00"})


class Persona(unittest.TestCase):
    def test_turkish_auto_and_forced(self):
        p = rs.persona_spec(TR)
        self.assertIn("Speak only Turkish.", p["prompt"]); self.assertIn(TR["persona"], p["prompt"]); self.assertIn("OUTPUT RULE", p["prompt"])
        self.assertIn("lang-tr", p["tags"])
        self.assertNotIn("Speak only", rs.persona_spec(EN)["prompt"])                       # auto: English text, no line
        self.assertIn("Speak only Turkish.", rs.persona_spec(EN, language="tr")["prompt"])  # --language tr
        self.assertNotIn("Speak only", rs.persona_spec(TR, language="none")["prompt"])
        self.assertIn("Speak only German.", rs.persona_spec(EN, language="de")["prompt"])

    def test_raw_and_criteria(self):
        p = rs.persona_spec(TR, raw=True)
        self.assertTrue(p["prompt"].startswith(TR["persona"])); self.assertNotIn("OUTPUT RULE", p["prompt"]); self.assertIn("Speak only Turkish.", p["prompt"])
        self.assertEqual(p["successCriteria"], "All of these are true:\n- Ajan kibar kalır\n- Ajan ödeme tarihi sorar")
        self.assertEqual(rs.persona_spec(EN)["successCriteria"], "All of these are true:\n- The agent confirms the payment.")
        self.assertEqual(p["name"], "T-01 Ret"); self.assertEqual(rs.persona_spec(EN)["name"], "E-01")

    def test_same_text_distinct_personas(self):
        a, b = dict(EN, id="A"), dict(EN, id="B")
        self.assertNotEqual(rs.spec_key("A", rs.persona_spec(a))[2:], rs.spec_key("B", rs.persona_spec(b))[2:])
        self.assertEqual(rs.spec_key("A", rs.persona_spec(a)), rs.spec_key("A", rs.persona_spec(dict(a))))

    def test_persona_ids_batches_and_store(self):
        made = []
        def create(batch): made.append(len(batch)); return {"personas": [{"id": "p-" + x["name"]} for x in batch]}
        sc = [dict(EN, id=f"S{i}") for i in range(130)]
        specs = {s["id"]: rs.persona_spec(s) for s in sc}
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            store = pathlib.Path(d) / "sub" / "store.json"
            ids = rs.persona_ids(sc, specs, store=store, create=create)
            self.assertEqual(made, [100, 30]); self.assertEqual(ids["S7"], "p-S7")
            ids2 = rs.persona_ids(sc, specs, store=store, create=create)      # unchanged text: nothing created again
            self.assertEqual(made, [100, 30]); self.assertEqual(ids, ids2)


class Selection(unittest.TestCase):
    def test_only_and_ids(self):
        data = [TR, EN, dict(EN, id=7)]
        with redirect_stdout(io.StringIO()) as out:
            s = rs.select(data, "E-01,7,NOPE")
        self.assertEqual([x["id"] for x in s], ["E-01", "7"]); self.assertIn("NOPE", out.getvalue())
        self.assertEqual(len(rs.select({"scenarios": data})), 3)

    def test_validation(self):
        bad = [dict(EN), dict(EN), {"id": "X", "criteria": ["c"]}, {"id": "Y", "persona": "p", "criteria": []}, {"persona": "p"},
               {"id": "Z", "persona": "p", "criteria": "c", "variables": [1]}]
        errs = rs.validate(bad)
        self.assertTrue(any("E-01: duplicate id" in e for e in errs)); self.assertTrue(any("X: no persona" in e for e in errs))
        self.assertTrue(any("Y: no criteria" in e for e in errs)); self.assertTrue(any("item 4: no id" in e for e in errs))
        self.assertTrue(any("Z: variables" in e for e in errs))
        with self.assertRaises(ValueError): rs.select(bad)
        self.assertEqual(rs.validate([TR, EN]), [])


class Verdicts(unittest.TestCase):
    def test_run_outcome(self):
        self.assertEqual(rs.verdict({"outcome": "pass", "reason": "ok \n fine"}), (True, "pass", "ok fine"))
        self.assertEqual(rs.verdict({"outcome": "fail", "reason": "no date"})[:2], (False, "fail"))
        self.assertEqual(rs.verdict({"outcome": "inconclusive"}), (False, "inconclusive", "inconclusive"))
        self.assertEqual(rs.verdict(None), (False, "none", "no verdict"))

    def test_call_analysis_fallback(self):
        call = {"analysis": {"successEvaluation": {"success": True, "reason": "met"}}}
        self.assertEqual(rs.verdict({"callId": "c"}, call), (True, "pass", "met"))
        call = {"successCriteriaResult": {"success": False, "reason": "missed"}}
        self.assertEqual(rs.verdict({}, call), (False, "fail", "missed"))
        self.assertEqual(rs.verdict({"outcome": "fail", "reason": "r"}, {"analysis": {"successEvaluation": {"success": True}}})[0], False)

    def test_row(self):
        v = rs.call_variables(TR)
        call = _call(vt={k: {"value": x} for k, x in v.items() if k != "vip"}, route_vt={"now": "x"})
        r = rs.make_row(dict(TR), {"outcome": "pass", "reason": "good", "callId": "call-1"}, call, v)
        self.assertTrue(r["pass"] and r["passed"]); self.assertFalse(r["empty"]); self.assertEqual(r["slowest_turn_s"], 3.5)
        self.assertEqual(r["vars_missing"], ["vip"]); self.assertEqual(r["turns"], 2); self.assertEqual(r["ended"], "customer-ended-call")
        self.assertEqual(r["failed"], [])

    def test_empty_rows(self):
        r = rs.make_row(dict(EN), {"outcome": "fail", "reason": "silence", "callId": "c"}, _call(turns=False), {})
        self.assertTrue(r["empty"]); self.assertFalse(r["pass"]); self.assertEqual(r["failed"], ["judge: silence"]); self.assertIsNone(r["vars_missing"])
        r = rs.make_row(dict(EN), None, None, {})                               # the persona never got a call
        self.assertTrue(r["empty"]); self.assertEqual(r["reason"], "no verdict")
        r = rs.make_row(dict(EN), {"outcome": "pass", "callId": "c"}, None, {}, fetch_error="boom")
        self.assertFalse(r["empty"]); self.assertEqual(r["call_error"], "boom")

    def test_seen_variables(self):
        self.assertEqual(rs.seen_variables(_call(vt={"a": 1}, route_vt={"b": 2})), {"a", "b"})
        self.assertIsNone(rs.missing_variables({"messages": []}, {"a": "1"}))

    def test_transcript(self):
        v = rs.call_variables(TR); call = _call(vt={})
        r = rs.make_row(dict(TR), {"outcome": "fail", "reason": "bad", "callId": "call-1"}, call, v)
        t = rs.transcript(dict(TR), r, call)
        self.assertIn("#### T-01", t); self.assertIn("FAIL (fail)", t); self.assertIn("judge: bad", t); self.assertIn("  - Ajan kibar kalır", t)
        self.assertIn("U: Merhaba?", t); self.assertIn("A: İyi günler.", t); self.assertIn("   [node greet]", t)
        self.assertIn('>> get_account {"customer_id": "c_1"}', t); self.assertIn('<< get_account {"balance": 10}', t); self.assertNotIn('"code"', t)
        self.assertIn("(no call)", rs.transcript(dict(EN), rs.make_row(dict(EN), None, None, {}), None))


class Summary(unittest.TestCase):
    def test_final_and_summary(self):
        sc = [dict(TR), dict(EN), dict(EN, id="E-02", family="Ret")]
        rep1 = [{"id": "T-01", "pass": True, "reason": "ok"}, {"id": "E-01", "pass": False, "reason": "first miss", "empty": True}, {"id": "E-02", "pass": True}]
        rep2 = [{"id": "T-01", "pass": False, "reason": "date \n wrong"}, {"id": "E-01", "pass": True, "reason": "ok"}, {"id": "E-02", "pass": True}]
        final = rs.final_rows(sc, [rep1, rep2])
        self.assertEqual([(f["id"], f["passes"], f["pass"]) for f in final], [("T-01", 1, False), ("E-01", 1, False), ("E-02", 2, True)])
        self.assertEqual(dict(rs.by_family(final)), {"Ret": {"passed": 1, "total": 2}, "-": {"passed": 0, "total": 1}})
        lines = rs.summary(final, [rep1, rep2])
        self.assertTrue(lines[0].startswith("1 / 3 passed")); self.assertEqual(lines[1], "per family: Ret 1/2, - 0/1")
        self.assertIn("  T-01 1/2 [Ret] date \n wrong"[:16], lines[3]); self.assertIn("EMPTY first miss", lines[4])
        one = rs.summary(rs.final_rows(sc, [rep1]), [rep1])
        self.assertEqual(one[0], "2 / 3 passed"); self.assertEqual(one[3], "  E-01 [-] EMPTY first miss")

    def test_cli(self):
        a = rs.parse_args(["AG", "--cases", "x.json", "--only", "a,b", "--reps", "2", "--language", "tr", "--out", "o.json", "--tx-dir", "tx",
                           "--force-cancel-others", "--no-empty-retry"])
        self.assertEqual((a.agent_id, a.only, a.reps, a.language, a.out, a.tx_dir, a.force_cancel_others, a.no_empty_retry),
                         ("AG", "a,b", 2, "tr", "o.json", "tx", True, True))
        self.assertEqual(rs.parse_args(["AG", "--cases", "x"]).language, "auto")
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit): rs.parse_args(["AG", "--cases", "x", "--reps", "0"])


class FakeRun(unittest.TestCase):
    """main() end to end with the Freya calls replaced: checks what reaches the simulation (customerVariablesMap)."""
    def test_main_offline(self):
        sims, cancels = [], []
        calls = {"call-T": _call(vt={"customerNumber": 1, "now": 1, "customer_id": 1, "amount": 1, "vip": 1, "count": 1, "items": 1}),
                 "call-E": _call(turns=False), "call-E2": _call(vt={})}
        def run_simulation(agent_id, name, vmap, max_seconds=420):
            sims.append((agent_id, vmap, max_seconds)); return f"sim{len(sims)}", "it", "completed"
        def get_iteration(sim_id, it_id):
            if sim_id == "sim1": return {"runs": [{"personaId": "p-T-01 Ret", "outcome": "pass", "reason": "ok", "callId": "call-T"},
                                                  {"personaId": "p-E-01", "outcome": "fail", "reason": "nothing said", "callId": "call-E"}]}
            return {"runs": [{"personaId": "p-E-01", "outcome": "fail", "reason": "wrong date", "callId": "call-E2"}]}
        def create(batch): return {"personas": [{"id": "p-" + x["name"]} for x in batch]}
        with tempfile.TemporaryDirectory() as d:
            cases = pathlib.Path(d) / "cases.json"; cases.write_text(json.dumps([TR, EN]))
            out, tx = pathlib.Path(d) / "out.json", pathlib.Path(d) / "tx"
            real_ids = rs.persona_ids
            with mock.patch.object(rs.freya, "run_simulation", run_simulation), mock.patch.object(rs.freya, "get_iteration", get_iteration), \
                 mock.patch.object(rs.freya, "get_call", lambda cid: calls[cid]), mock.patch.object(rs.rc, "cancel_others", lambda: cancels.append(1)), \
                 mock.patch.object(rs, "persona_ids", lambda sc, specs: real_ids(sc, specs, store=pathlib.Path(d) / "store.json", create=create)), \
                 redirect_stdout(io.StringIO()) as printed:
                rs.main(["AG", "--cases", str(cases), "--out", str(out), "--tx-dir", str(tx), "--force-cancel-others", "--max-call-seconds", "300"])
            self.assertEqual(sims[0][0], "AG"); self.assertEqual(sims[0][2], 300); self.assertEqual(len(cancels), 2)
            self.assertEqual(sims[0][1], {"p-T-01 Ret": rs.call_variables(TR), "p-E-01": {}})          # per-persona call variables
            self.assertEqual(sims[1][1], {"p-E-01": {}})                                              # the empty call re-run alone
            res = json.loads(out.read_text())
            self.assertEqual((res["passed"], res["total"], res["failed"]), (1, 2, ["E-01"]))
            rows = {r["id"]: r for r in res["rows"]}
            self.assertTrue(rows["E-01"]["retried_empty"]); self.assertEqual(rows["E-01"]["reason"], "wrong date"); self.assertFalse(rows["T-01"]["retried_empty"])
            self.assertEqual(rows["T-01"]["vars_missing"], []); self.assertEqual(rows["T-01"]["slowest_turn_s"], 3.5)
            self.assertEqual(sorted(p.name for p in tx.iterdir()), ["E-01.txt", "T-01.txt"])
            self.assertIn("1 / 2 passed", printed.getvalue()); self.assertIn("E-01 [-] wrong date", printed.getvalue())

    def test_abort_cancels_own_simulation(self):
        cancelled = []
        def run_simulation(agent_id, name, vmap, max_seconds=420):
            rs.freya.LAST_SIM = "sim-own"; raise KeyboardInterrupt
        with mock.patch.object(rs.freya, "run_simulation", run_simulation), mock.patch.object(rs.freya, "LAST_SIM", None), \
             mock.patch.object(rs.freya, "cancel_simulation", cancelled.append), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt): rs.run_once("AG", [dict(EN)], {"E-01": "p1"}, {"E-01": {}})
        self.assertEqual(cancelled, ["sim-own"])


if __name__ == "__main__": unittest.main()
