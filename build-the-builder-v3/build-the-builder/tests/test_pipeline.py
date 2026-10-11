"""Offline tests for builder.pipeline / build.py: every external call (LLM, extractor, generator, Freya,
simulation) is faked, time runs on a fake clock.

    cd build-the-builder && python -m unittest tests.test_pipeline -v
"""
import copy
import datetime
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest

KIT = pathlib.Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(KIT))

from builder import pipeline, prompts as prompts_mod  # noqa: E402
from builder.pipeline import Builder, Deps  # noqa: E402

pipeline.QUIET = True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def base_workflow(policy):
    return {"initial_node": "greet", "system_prompt": "You are the agent of {{bank}}.",
            "nodes": {"greet": {"name": "greet", "node_type": "conversation",
                                "task_message": "Greet {{first_name}}. Call verify_identity.",
                                "tools": [{"type": "function", "function": {"name": "verify_identity"}}]},
                      "talk": {"name": "talk", "node_type": "conversation", "task_message": "Negotiate. {{w_facts}}"},
                      "end": {"name": "end", "node_type": "end_call", "terminal": {}}},
            "test_variables": {"first_name": "x"}, "_policy_marker": len((policy.get("calendar") or {}).get("holidays") or [])}


class Fakes:
    """All external services, recording what happened."""

    def __init__(self, clock, extract_s=120, prompt_answer=None, dry_run_rejects=(), test_results=None,
                 extract_fails=0, repair_answer=None, generate_fails=0, hang=()):
        self.clock = clock
        self.hooks = []
        self.saved, self.exported, self.validated, self.llm_purposes = [], [], [], []
        self.extract_s = extract_s
        self.extract_fails = extract_fails
        self.prompt_answer = prompt_answer if prompt_answer is not None else {"edits": [
            {"step": "system_prompt", "op": "append", "new": "Be brief."},
            {"step": "greet", "op": "replace", "old": "Greet {{first_name}}.", "new": "Say hello to {{first_name}}."},
            {"step": "talk", "op": "rewrite", "new": "Lost the placeholder."},
            {"step": "ghost", "op": "append", "new": "x"}]}
        self.repair_answer = repair_answer if repair_answer is not None else {
            "edits": [{"step": "talk", "op": "replace", "old": "Negotiate.", "new": "Negotiate better."}],
            "policy_patch": {"calendar": {"weekend": [6, 7], "holidays": ["2026-12-25"]}},
            "diagnosis": "x"}
        self.dry_run_rejects = list(dry_run_rejects)
        self.test_results = list(test_results or [])
        self.tests_run = []
        self.extract_calls = []
        self.generate_fails = generate_fails
        self.generate_calls = []
        self.hang = set(hang)               # Freya calls that never return
        self.release = threading.Event()

    # --- llm
    def llm(self):
        f = self

        def on_call(fn):
            f.hooks.append(fn)
            return fn

        def emit(purpose, secs):
            f.clock.advance(secs)
            for h in f.hooks:
                h({"model": "fake-model", "purpose": purpose, "backend": "fake", "duration_s": secs, "ok": True})

        def ask_json(prompt, **kw):
            p = kw.get("purpose", "")
            f.llm_purposes.append(p)
            emit(p, 30)
            if p.startswith("write node prompts"):
                return copy.deepcopy(f.prompt_answer)
            if p.startswith("repair"):
                return copy.deepcopy(f.repair_answer)
            if p.startswith("fix policy"):
                return {"bands": [{"name": "fixed"}]}
            return {}
        return types.SimpleNamespace(on_call=on_call, remove_hook=lambda fn: None, ask_json=ask_json,
                                     fast_model=lambda: "fake-fast", emit=emit)

    def extract(self):
        f = self

        def extract_policy(paths, openapi, budget_s=None, warnings=None, replicates=2):
            f.clock.advance(f.extract_s)
            f.extract_calls.append(replicates)
            for h in f.hooks:
                h({"model": "fake-model", "purpose": "extract policy part 1", "duration_s": f.extract_s, "ok": True})
            if f.extract_fails and len(f.extract_calls) <= f.extract_fails:
                raise RuntimeError("extraction failed")
            f.extract_budget = budget_s
            return {"identity": {"max_wrong": 2}, "outcomes": {"table": []}, "variables": [],
                    "bands": []}
        return types.SimpleNamespace(extract_policy=extract_policy, read_documents=lambda paths: [("b.md", "brief text")],
                                     documents_block=lambda docs: "=== b.md ===\nRule one.\n\nRule two.",
                                     parse_openapi=lambda path: {"tools": {"verify_identity": {"fields": {}}}},
                                     numbered_rules=lambda docs: [])

    def schema(self):
        return types.SimpleNamespace(validate=lambda p: [] if p.get("bands") is not None else ["bands missing"])

    def generate(self):
        f = self

        def generate_workflow(policy, prompts=None, openapi=None):
            f.generate_calls.append(policy)
            if len(f.generate_calls) <= f.generate_fails:
                raise ValueError("policy has no verify_identity tool")
            return base_workflow(policy)
        return types.SimpleNamespace(generate_workflow=generate_workflow)

    def freya(self):
        f = self

        def maybe_hang(name):
            if name in f.hang:
                f.release.wait(60)

        def validate_workflow(name, wf):
            maybe_hang("validate")
            f.validated.append(wf)
            if f.dry_run_rejects and f.dry_run_rejects.pop(0):
                raise RuntimeError("POST /api/v2/workflow -> 400: bad field")
            return {"ok": True}

        def save_workflow(slot, wf, state_file=None):
            maybe_hang("save")
            f.clock.advance(5)
            f.saved.append((slot, copy.deepcopy(wf), state_file))
            return {"workflow_id": "wf_1", "agent_id": "ag_1", "version": len(f.saved)}

        def export_workflow(wid, path):
            maybe_hang("export")
            f.exported.append((wid, path, copy.deepcopy(f.saved[-1][1])))
            pathlib.Path(path).write_text(json.dumps({"workflow_id": wid}))
            return path
        return types.SimpleNamespace(validate_workflow=validate_workflow, save_workflow=save_workflow,
                                     export_workflow=export_workflow)

    def casegen(self):
        def generate_cases(policy, ask_json=None, n_max=12, deadline=None, seed=7, model=None, timeout_s=None):
            if ask_json:
                ask_json("x", purpose="write self-test customer scripts")
            return [{"id": "SELF-%02d" % i, "family": "F", "title": "t", "script": "s", "variables": {},
                     "products": [], "expected": {}, "checks": []} for i in range(1, n_max + 1)], []
        return types.SimpleNamespace(generate_cases=generate_cases)

    def selftester(self):
        f = self

        class Tester:
            def __init__(self, openapi_path, slot, state_file, trace=None, policy=None):
                f.test_slot = slot

            def run(self, wf, cases, label="", timeout_s=None):
                f.clock.advance(300)
                f.tests_run.append((label, [c["id"] for c in cases], wf, timeout_s))
                failing = f.test_results.pop(0) if f.test_results else set()
                # a call that ran made tool calls; 'NOTOOLS:<id>' marks a platform failure before the first tool call
                rows = [{"id": c["id"], "pass": c["id"] not in failing, "failed": ["x"] if c["id"] in failing else [],
                         "tools": [] if "NOTOOLS:" + c["id"] in failing else ["verify_identity"]} for c in cases]
                return {"ok": True, "passed": sum(r["pass"] for r in rows), "total": len(rows), "rows": rows,
                        "duration_s": 300, "simulation_id": "sim"}
        return Tester

    def deps(self):
        return Deps(llm=self.llm(), extract=self.extract(), schema=self.schema(), generate=self.generate(),
                    freya=self.freya(), casegen=self.casegen(), selftester=self.selftester())


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.brief = self.dir / "my-brief.md"
        self.brief.write_text("brief")
        self.openapi = self.dir / "openapi.json"
        self.openapi.write_text("{}")
        self.clock = Clock()

    def tearDown(self):
        os.environ.pop("SCORE_LOG", None)
        self.tmp.cleanup()

    def build(self, fakes, **kw):
        os.environ["SCORE_LOG"] = str(self.dir / "results" / "log.jsonl")
        now0 = datetime.datetime(2026, 10, 18, 9, 0, tzinfo=datetime.timezone.utc)
        b = Builder([str(self.brief)], str(self.openapi), out_dir=str(self.dir / "out"), trace_dir=str(self.dir / "traces"),
                    state_file=str(self.dir / "state.json"), deps=fakes.deps(), clock=self.clock,
                    now=lambda: now0 + datetime.timedelta(seconds=self.clock.t), seed=1, **kw)
        return b, b.run()

    def test_no_test_run_saves_exports_and_traces(self):
        f = Fakes(self.clock)
        b, res = self.build(f, no_test=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(b.slot, "my-brief")                         # slot from the first brief's file name
        self.assertEqual(len(f.saved), 1)
        slot, wf, state = f.saved[0]
        self.assertEqual((slot, state), ("my-brief", str(self.dir / "state.json")))
        # LLM prompts applied safely: kept placeholders, dropped the text that lost one, ignored unknown step
        self.assertEqual(wf["system_prompt"], "You are the agent of {{bank}}.\n\nBe brief.")
        self.assertEqual(wf["nodes"]["greet"]["task_message"], "Say hello to {{first_name}}. Call verify_identity.")
        self.assertEqual(wf["nodes"]["talk"]["task_message"], "Negotiate. {{w_facts}}")
        self.assertEqual(f.exported[0][1], str(self.dir / "out" / "my-brief.workflow.json"))
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("model call: fake-model | extract policy part 1", txt)
        self.assertIn("write node prompts", txt)
        self.assertIn("saved phone agent", txt)
        self.assertIn("workflow_id=wf_1", txt)
        self.assertIn("exported", txt)
        self.assertIn("self-test skipped (--no-test)", txt)
        self.assertIn("FINAL RESULT: status=ok", txt)
        self.assertTrue(pathlib.Path(res["trace"]).name.startswith("my-brief-"))
        self.assertTrue((self.dir / "out" / "my-brief.policy.json").exists())

    def test_score_log_line(self):
        os.environ["RUN_LABEL"] = "unit"
        try:
            f = Fakes(self.clock, test_results=[{"SELF-02", "SELF-05"}, {"SELF-05"}, {"SELF-05"}])
            b, res = self.build(f)
        finally:
            os.environ.pop("RUN_LABEL", None)
        lines = (self.dir / "results" / "log.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["source"], "build")
        self.assertEqual(rec["label"], "unit")
        self.assertEqual((rec["slot"], rec["bank"]), ("my-brief", "my-brief"))
        self.assertEqual((rec["agent_id"], rec["workflow_id"]), ("ag_1", "wf_1"))
        self.assertEqual((rec["passed"], rec["total"]), (9, 10))
        self.assertEqual(rec["failed"], ["SELF-05"])
        self.assertEqual(rec["score_kind"], "verified")              # gain of 1 was confirmed by a full retest
        self.assertFalse(rec["estimated"])
        self.assertTrue(rec["exported"].endswith("my-brief.workflow.json"))
        for k in ("at", "git_sha", "build_seconds", "stage_seconds"):
            self.assertIn(k, rec)

    def test_score_log_null_when_untested(self):
        b, res = self.build(Fakes(self.clock), no_test=True)
        rec = json.loads((self.dir / "results" / "log.jsonl").read_text().splitlines()[-1])
        self.assertIsNone(rec["passed"])
        self.assertIsNone(rec["total"])
        self.assertEqual(rec["failed"], [])

    def test_dry_run_rejection_falls_back_to_default_prompts(self):
        f = Fakes(self.clock, dry_run_rejects=[True, False])
        b, res = self.build(f, no_test=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(f.saved[0][1]["system_prompt"], "You are the agent of {{bank}}.")
        self.assertIn("rejected: POST /api/v2/workflow -> 400", pathlib.Path(res["trace"]).read_text())

    def test_selftest_repair_kept_when_better(self):
        f = Fakes(self.clock, test_results=[{"SELF-02", "SELF-05"}, set()])
        b, res = self.build(f)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(f.test_slot, "my-brief-selftest")
        self.assertEqual(len(f.tests_run), 2)
        retest_ids = f.tests_run[1][1]
        self.assertIn("SELF-02", retest_ids)
        self.assertIn("SELF-05", retest_ids)
        self.assertLessEqual(len(retest_ids), 4)                     # failing + a small sample of passing
        self.assertEqual(len(f.saved), 2)                            # v1, then the repaired version in place
        self.assertEqual(f.saved[1][1]["nodes"]["talk"]["task_message"], "Negotiate better. {{w_facts}}")
        self.assertEqual(f.saved[1][1]["_policy_marker"], 1)         # policy patch applied
        self.assertEqual(len(f.exported), 2)
        self.assertEqual(res["best"], {"passed": 10, "total": 10, "label": "v2"})
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("test run: self-test v1", txt)
        self.assertIn("score 8/10", txt)
        self.assertIn("repair 1 kept: retested subset 2 -> 4 passing; estimated total 10/10", txt)
        self.assertIn("selftest_score=10/10 (estimated)", txt)
        self.assertIn("repair round 1 from 2 failing", txt)
        self.assertLessEqual(self.clock.t, 28 * 60)

    def test_platform_failures_are_retested_without_repair(self):
        # v1: two calls stopped before any tool call (Freya code-step hiccup) -> same version retested, no LLM repair
        f = Fakes(self.clock, test_results=[{"SELF-02", "SELF-05", "NOTOOLS:SELF-02", "NOTOOLS:SELF-05"}, set()])
        b, res = self.build(f)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(f.tests_run), 2)
        self.assertIn("rerun, no change", f.tests_run[1][0])
        self.assertIs(f.tests_run[1][2], f.tests_run[0][2])               # the very same workflow
        self.assertEqual(len(f.saved), 1)                                 # nothing repaired, nothing re-saved
        self.assertIn("retesting the same version (no repair)", pathlib.Path(res["trace"]).read_text())

    def test_repair_discarded_when_not_better(self):
        f = Fakes(self.clock, test_results=[{"SELF-03"}, {"SELF-03"}, {"SELF-03"}])
        b, res = self.build(f)
        self.assertEqual(len(f.saved), 1)                            # the repaired versions never replaced v1
        self.assertEqual(res["best"]["label"], "v1")
        self.assertIn("repair 1 discarded", pathlib.Path(res["trace"]).read_text())

    def test_budget_skips_selftest_when_time_is_short(self):
        f = Fakes(self.clock, extract_s=24 * 60)
        b, res = self.build(f)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(f.tests_run, [])
        self.assertEqual(len(f.saved), 1)
        self.assertEqual(len(f.exported), 1)
        self.assertLessEqual(self.clock.t, 28 * 60)
        self.assertIn("not enough time", pathlib.Path(res["trace"]).read_text())

    def test_budget_never_starts_a_simulation_that_cannot_finish(self):
        # time for v1 test, not for a repair retest
        f = Fakes(self.clock, extract_s=8 * 60, test_results=[{"SELF-01"}, set()])
        b, res = self.build(f, budget_min=20)
        est_total = 20 * 60
        for label, ids, wf, timeout in f.tests_run:
            self.assertGreater(timeout, 0)
        self.assertLessEqual(self.clock.t, est_total)
        self.assertEqual(res["status"], "ok")

    def test_case_count_shrinks_to_fit(self):
        # after extract + prompts there is room for one wave only
        f = Fakes(self.clock, extract_s=17 * 60)
        b, res = self.build(f)
        self.assertEqual(len(f.tests_run), 1)
        self.assertLessEqual(len(f.tests_run[0][1]), 5)

    def test_extract_failure_retries_with_one_replicate(self):
        f = Fakes(self.clock, extract_fails=1)
        b, res = self.build(f, no_test=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(f.extract_calls, [2, 1])
        self.assertEqual(len(f.saved), 1)
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("extraction failed: RuntimeError: extraction failed", txt)
        self.assertIn("extract: ok (single-replicate retry)", txt)

    def test_extract_failure_still_ships_fallback_agent(self):
        f = Fakes(self.clock, extract_fails=2)
        b, res = self.build(f, no_test=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(f.saved), 1)                            # something is always saved and exported
        self.assertEqual(len(f.exported), 1)
        self.assertEqual(b.policy["tools"], {"verify_identity": {"fields": {}}})
        self.assertEqual([r["text"] for r in b.policy["rules"]], ["=== b.md ===\nRule one.", "Rule two."])
        self.assertEqual(set(b.policy), set(pipeline.POLICY_KEYS))
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("using fallback policy", txt)
        self.assertIn("extract: fallback", txt)

    def test_generate_failure_falls_back(self):
        f = Fakes(self.clock, generate_fails=1)
        b, res = self.build(f, no_test=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(f.generate_calls[1]["source"]["notes"], ["fallback policy: extraction failed"])
        self.assertIn("generate: fallback policy", pathlib.Path(res["trace"]).read_text())
        self.assertEqual(len(f.saved), 1)

    def test_generate_failure_everywhere_ships_minimal_workflow(self):
        f = Fakes(self.clock, generate_fails=99)
        b, res = self.build(f, no_test=True)
        self.assertEqual(res["status"], "ok")
        wf = f.saved[0][1]
        self.assertEqual(list(wf["nodes"]), ["conversation"])
        self.assertIn("Rule one.", wf["system_prompt"])
        self.assertEqual(len(f.exported), 1)

    def test_hung_freya_calls_end_before_the_budget(self):
        # 17 s left after extraction; validate, save and export never return
        f = Fakes(self.clock, extract_s=28 * 60 - pipeline.FINAL_MARGIN_S - 2, hang={"export"})
        t0 = time.time()
        try:
            b, res = self.build(f, no_test=True, no_prompts=True)
        finally:
            f.release.set()
        self.assertLess(time.time() - t0, 10)                        # export 2 s + retry 2 s, not 2 x 120 s
        self.assertEqual(len(f.saved), 1)
        self.assertIsNone(res["export"])
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("export failed", txt)
        self.assertIn("export missing at the end: retrying", txt)
        self.assertLessEqual(self.clock.t, 28 * 60)

    def test_hung_save_fails_fast(self):
        f = Fakes(self.clock, extract_s=28 * 60 - pipeline.FINAL_MARGIN_S - 2, hang={"save", "validate"})
        t0 = time.time()
        try:
            b, res = self.build(f, no_test=True, no_prompts=True)
        finally:
            f.release.set()
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(res["status"], "failed")
        self.assertIn("did not return within", pathlib.Path(res["trace"]).read_text())

    def test_export_retried_in_finally(self):
        f = Fakes(self.clock)
        orig = f.freya
        state = {"n": 0}

        def freya():
            ns = orig()
            inner = ns.export_workflow

            def flaky(wid, path):
                state["n"] += 1
                if state["n"] == 1:
                    raise RuntimeError("GET -> 502")
                return inner(wid, path)
            ns.export_workflow = flaky
            return ns
        f.freya = freya
        b, res = self.build(f, no_test=True)
        self.assertEqual(state["n"], 2)
        self.assertTrue(res["export"] and os.path.exists(res["export"]))

    def test_gain_of_one_needs_full_retest(self):
        f = Fakes(self.clock, test_results=[{"SELF-02", "SELF-05"}, {"SELF-05"}, {"SELF-05"}, {"SELF-05"}])
        b, res = self.build(f)
        labels = [t[0] for t in f.tests_run]
        self.assertEqual(labels[:3], ["self-test v1", "self-test v2 (retest after repair 1)",
                                      "self-test v2 (full retest after repair 1)"])
        self.assertEqual(len(f.tests_run[2][1]), 10)
        self.assertEqual(res["best"]["label"], "v2")
        self.assertEqual(res["best"]["passed"], 9)
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("repair 1 kept: full retest 8 -> 9 passing; verified total 9/10", txt)
        rec = json.loads((self.dir / "results" / "log.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["score_kind"], "verified")

    def test_gain_of_one_not_kept_without_full_retest_gain(self):
        f = Fakes(self.clock, test_results=[{"SELF-02", "SELF-05"}, {"SELF-05"}, {"SELF-05", "SELF-07", "SELF-08"}])
        b, res = self.build(f, repair_rounds=1)
        self.assertEqual(len(f.saved), 1)
        self.assertEqual(res["best"]["label"], "v1")
        self.assertIn("discarded after full retest", pathlib.Path(res["trace"]).read_text())

    def test_policy_fix_when_validation_fails(self):
        f = Fakes(self.clock)
        orig = f.extract

        def extract():
            ns = orig()
            inner = ns.extract_policy

            def ep(*a, **k):
                p = inner(*a, **k)
                p.pop("bands")
                return p
            ns.extract_policy = ep
            return ns
        f.extract = extract
        b, res = self.build(f, no_test=True)
        self.assertIn("fix policy validation errors", f.llm_purposes)
        self.assertEqual(b.policy["bands"], [{"name": "fixed"}])


class SelfTestRunnerTests(unittest.TestCase):
    """builder.selftest: health check, per-simulation lock, hard timeout (no Freya, no simulation)."""

    def setUp(self):
        from builder import selftest
        self.st = selftest
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / "openapi.json").write_text("{}")
        self.saved = []
        self.env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def runner(self, sim_s=0.0, fail=(), save_hang=False):
        t = self
        import time as _t
        self.get_calls = []
        holder = {}

        def run_once(agent_id, cases, ids, scenarios, spec, sink):
            _t.sleep(sim_s)
            for c in cases:                                         # like run_synthetic: reads every call once
                holder["fr"].get_call("call-" + c["id"])
            rows = [{"id": c["id"], "pass": c["id"] not in fail, "failed": ["x"] if c["id"] in fail else [],
                     "tools": [], "call_id": "call-" + c["id"]} for c in cases]
            return rows, "sim1", "it1", "completed", sim_s, 1.0

        def get_call(cid):
            t.get_calls.append(cid)
            return {"messages": [{"role": "assistant", "message": "hello " + cid}]}

        def save(slot, wf, state_file=None):
            if save_hang:
                _t.sleep(5)
            t.saved.append(slot)
            return {"workflow_id": "w", "agent_id": "a"}
        rs = types.SimpleNamespace(HOST="https://bank.invalid", scenario_extras=lambda c, f, m: {},
                                   personas=lambda cases, style: {c["id"]: "p" + c["id"] for c in cases},
                                   run_once=run_once)
        tm = types.SimpleNamespace(scenario_for_case=lambda c, e: {}, SINK_VAR="sink", to_test_mode=lambda wf, oa, sink=None: wf,
                                   spec_from_openapi=lambda oa: {})
        fr = types.SimpleNamespace(URL="https://freya.invalid", HEADERS={}, save_workflow=save, get_call=get_call)
        holder["fr"] = fr
        return (rs, tm, fr)

    def make_tester(self, probe_ok=True, sim_s=0.0, **kw):
        os.environ.pop("FREYA_BANK_TEAM_KEY", None)
        t = self.st.SelfTester(self.dir / "openapi.json", "x-selftest", self.dir / "state.json", runner=self.runner(sim_s, **kw))
        t.probe = lambda url, headers, timeout_s: (probe_ok, "HTTP 200" if probe_ok else "HTTP 530")
        return t

    def test_service_down_skips_without_waiting(self):
        res = self.make_tester(probe_ok=False).run({}, [{"id": "A"}], timeout_s=600)
        self.assertFalse(res["ok"])
        self.assertIn("service down", res["error"])
        self.assertEqual(self.saved, [])                              # nothing saved, no simulation

    def test_runs_and_scores(self):
        os.environ.pop("ORCH_SIM_LOCK", None)
        res = self.make_tester().run({}, [{"id": "A"}, {"id": "B"}], timeout_s=600)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["passed"], res["total"]), (2, 2))
        self.assertEqual(self.saved, ["x-selftest"])

    def test_transcripts_reuse_calls_read_during_run(self):
        os.environ.pop("ORCH_SIM_LOCK", None)
        t = self.make_tester(fail={"B"})
        res = t.run({}, [{"id": "A"}, {"id": "B"}], timeout_s=600)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.get_calls, ["call-A", "call-B"])        # no second get_call for the failing case
        row = [r for r in res["rows"] if r["id"] == "B"][0]
        self.assertIn("hello call-B", row["transcript"])
        self.assertNotIn("transcript", [r for r in res["rows"] if r["id"] == "A"][0])

    def test_one_deadline_covers_the_save(self):
        os.environ.pop("ORCH_SIM_LOCK", None)
        t0 = time.time()
        res = self.make_tester(save_hang=True).run({}, [{"id": "A"}], timeout_s=1.0)
        self.assertLess(time.time() - t0, 3)
        self.assertFalse(res["ok"])
        self.assertIn("did not return within", res["error"])

    def test_hard_timeout_abandons_simulation(self):
        os.environ.pop("ORCH_SIM_LOCK", None)
        res = self.make_tester(sim_s=3).run({}, [{"id": "A"}], timeout_s=61.5)
        self.assertTrue(res["ok"])                                    # 61.5 s limit > 3 s sim
        res2 = self.make_tester(sim_s=2).run({}, [{"id": "A"}], timeout_s=1.5)
        self.assertFalse(res2["ok"])
        self.assertIn("abandoned", res2["error"])
        self.assertLess(res2["duration_s"], 2.0)

    def test_sim_lock_per_simulation(self):
        fake = self.dir / "fake_orch.py"
        log = self.dir / "lock.log"
        fake.write_text("import subprocess, sys\n"
                        "args = sys.argv[1:]\n"
                        "assert args[:3] == ['with-lock', 'db', '--'], args\n"
                        "open(%r, 'a').write('lock\\n')\n"
                        "rc = subprocess.call(args[3:])\n"
                        "open(%r, 'a').write('unlock\\n')\n"
                        "sys.exit(rc)\n" % (str(log), str(log)))
        os.environ["ORCH_SIM_LOCK"] = "1"
        os.environ["ORCH_CMD"] = "%s %s" % (sys.executable, fake)
        res = self.make_tester().run({}, [{"id": "A"}], timeout_s=600)
        self.assertTrue(res["ok"], res)
        self.assertEqual(log.read_text().split(), ["lock", "unlock"])

    def test_busy_workspace_gives_up(self):
        calls = []

        def api(method, path, body=None, **params):
            calls.append(path)
            raise RuntimeError("POST /api/v2/simulations -> 409: too many concurrent simulations")
        fr = types.SimpleNamespace(api=api)
        restore = self.st.guard_busy(fr, 0)
        with self.assertRaises(self.st.WorkspaceBusy):
            fr.api("POST", "/api/v2/simulations", {})
        restore()
        self.assertIs(fr.api, api)
        restore = self.st.guard_busy(fr, 3600)
        with self.assertRaises(RuntimeError) as cm:
            fr.api("POST", "/api/v2/simulations", {})
        self.assertIn("concurrent simulations", str(cm.exception))      # still waiting inside the budget
        restore()

    def test_sim_lock_off_by_default(self):
        os.environ.pop("ORCH_SIM_LOCK", None)
        with self.st.SimLock(5) as lk:
            self.assertTrue(lk.acquired)
            self.assertIsNone(lk.proc)

    def test_estimates(self):
        self.assertEqual(self.st.estimate_sim_s(0), 0)
        self.assertGreater(self.st.estimate_sim_s(10, 5), self.st.estimate_sim_s(5, 5))
        self.assertEqual(self.st.max_cases_within(self.st.estimate_sim_s(5, 5), 5), 5)
        self.assertEqual(self.st.max_cases_within(10, 5), 0)


class HelperTests(unittest.TestCase):
    def test_round_robin_by_family(self):
        cases = [{"id": "1", "family": "A"}, {"id": "2", "family": "A"}, {"id": "3", "family": "A"},
                 {"id": "4", "family": "B"}, {"id": "5", "family": "C"}, {"id": "6", "family": "C"}]
        self.assertEqual([c["id"] for c in pipeline.round_robin(cases, 3)], ["1", "4", "5"])
        self.assertEqual([c["id"] for c in pipeline.round_robin(cases, 5)], ["1", "2", "4", "5", "6"])
        self.assertEqual(len(pipeline.round_robin(cases, 99)), 6)
        self.assertEqual(pipeline.round_robin(cases, 0), [])

    def test_score_log_default_is_kit_relative(self):
        old = os.environ.pop("SCORE_LOG", None)
        try:
            self.assertEqual(pipeline.score_log_path(), str(KIT / "results" / "log.jsonl"))
            os.environ["SCORE_LOG"] = "/tmp/x.jsonl"
            self.assertEqual(pipeline.score_log_path(), "/tmp/x.jsonl")
        finally:
            os.environ.pop("SCORE_LOG", None)
            if old:
                os.environ["SCORE_LOG"] = old

    def test_apply_policy_patch(self):
        p = {"calendar": {"weekend": [6, 7]}, "bands": [], "tools": {"a": 1}}
        out = pipeline.apply_policy_patch(p, {"calendar": {"weekend": [7]}, "bands": "bad type", "tools": {},
                                              "evil": 1})
        self.assertEqual(out["calendar"], {"weekend": [7]})
        self.assertEqual(out["bands"], [])
        self.assertEqual(out["tools"], {"a": 1})
        self.assertNotIn("evil", out)
        self.assertEqual(p["calendar"], {"weekend": [6, 7]})

    def test_slot_from_paths(self):
        self.assertEqual(pipeline.slot_from_paths(["/x/brief-a.md", "/x/brief-b.md"]), "brief-a")
        self.assertEqual(pipeline.slot_from_paths(["/x/My Brief (v2).md"]), "My-Brief-v2")

    def test_clean_prompts(self):
        wf = base_workflow({})
        out, dropped = prompts_mod.clean_prompts({"edits": [
            {"step": "system_prompt", "op": "rewrite", "new": "No placeholder"},
            {"step": "greet", "op": "append", "new": "Use {{made_up}}."},
            {"step": "greet", "op": "replace", "old": "not there", "new": "x"},
            {"step": "end", "op": "append", "new": "bye"},
            {"step": "talk", "op": "append", "new": "Say {{bank}} {{w_facts}}."},
            {"step": "talk", "op": "replace", "old": "Say {{bank}}", "new": "Tell {{bank}}"},
            {"step": "talk", "op": "append", "new": "Bad }} braces"},
            {"bogus": 1}]}, wf)
        self.assertEqual([(e["step"], e["op"]) for e in out["edits"]], [("talk", "append"), ("talk", "replace")])
        self.assertEqual(len(dropped), 6)
        self.assertTrue(any("made_up" in d for d in dropped))
        w2 = prompts_mod.apply_prompts(wf, out)
        self.assertEqual(w2["nodes"]["talk"]["task_message"], "Negotiate. {{w_facts}}\n\nTell {{bank}} {{w_facts}}.")
        self.assertEqual(wf["nodes"]["talk"]["task_message"], "Negotiate. {{w_facts}}")      # input untouched
        merged = prompts_mod.merge_prompts(out, {"edits": [{"step": "greet", "op": "append", "new": "Hi."}]})
        self.assertEqual(len(merged["edits"]), 3)

    def test_build_cli_parses(self):
        import build
        self.assertTrue(callable(build.main))


if __name__ == "__main__":
    unittest.main()
