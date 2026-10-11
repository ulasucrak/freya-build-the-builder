"""Offline tests for the generic (non-collection) path of the pipeline glue: policy_schema accepts a policy whose
tools take no collection role without a policy-fix round, the build skips the self-test for a generic service
agent (no casegen, no simulation), the prompt stage still runs, inputs expand recursively and the trace can be
written to a fixed path. Every external call is faked; nothing here knows a brief.

    cd build-the-builder && python -m unittest tests.test_pipeline_generic -v
"""
import copy
import datetime
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest

KIT = pathlib.Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(KIT))

from builder import pipeline, policy_schema as S, prompts as prompts_mod  # noqa: E402
from builder.pipeline import Builder, Deps  # noqa: E402

pipeline.QUIET = True
FIX = KIT / "tests" / "fixtures_policy"


def envelope_op(op_id, data_props, required=()):
    """A contract-shaped operation: body {"data": {...}, "call": {...}} with a constant in `call`."""
    return {"post": {"operationId": op_id, "requestBody": {"content": {"application/json": {"schema": {
        "type": "object", "required": ["data", "call"],
        "properties": {"data": {"type": "object", "required": list(required), "properties": data_props},
                       "call": {"type": "object", "properties": {"flow": {"type": "string", "default": "Svc"}}}}}}}},
        "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {
            "result": {"type": "object"}, "data": {"type": "object"}}}}}}}}}


GENERIC_OPENAPI = {"openapi": "3.0.0", "servers": [{"url": "https://svc.example.invalid"}], "paths": {
    "/parcels/list": envelope_op("parcels.list", {"clientNo": {"type": "string"}}, ["clientNo"]),
    "/parcels/status": envelope_op("parcels.status.get", {"parcelId": {"type": "string"}}),
    "/delivery/slot/change": envelope_op("delivery.slot.change", {"parcelId": {"type": "string"},
                                                                  "slot": {"type": "string"}}),
    "/receipt/send": envelope_op("receipt.send", {"email": {"type": "string"}}),
}}

COLLECTION_OPENAPI = {"paths": {"/verify_identity": {"post": {}}, "/get_account": {"post": {}},
                                "/create_promise": {"post": {}}, "/close_call": {"post": {}}}}


def generic_policy():
    tools = {}
    for path, ops in GENERIC_OPENAPI["paths"].items():
        op = ops["post"]
        props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
        tools[op["operationId"]] = {"path": path, "method": "POST",
                                    "fields": {"data": {"type": "object", "required": True,
                                                        "properties": props["data"]["properties"]},
                                               "call": {"type": "object", "required": True,
                                                        "properties": props["call"]["properties"]}}}
    return {"source": {"briefs": ["brief.md"]},
            "variables": [{"name": "clientNumber", "meaning": "the caller's client number; never ask",
                           "example": "100200"},
                          {"name": "now", "meaning": "current date and time; every date decision uses it",
                           "examples": ["2026-10-12T10:30:00+02:00"]}],
            "clock": {"now_variable": "now", "timezone": "Europe/Berlin", "date_format": "dd.MM.yyyy"},
            "language": "German",
            "identity": {"questions": []}, "bands": [], "products": {}, "outcomes": {"table": []},
            "triggers": [{"name": "agent_request", "detect": "caller asks for a person", "action": "transfer"}],
            "phrases": {"greeting": "Guten Tag, wie kann ich helfen?"},
            "tools": tools,
            "rules": [{"id": "1", "text": "Read the parcel status from the service; never guess it."},
                      {"id": "2", "text": "Offer a slot change only once per call."}]}


def generic_workflow(policy, meta=None):
    tools = [{"type": "function", "function": {"name": n.replace(".", "_")}} for n in policy.get("tools") or {}]
    wf = {"initial_node": "assistant",
          "system_prompt": "Speak %s. clientNumber = {{clientNumber}}, now = {{now}}." % policy.get("language", "English"),
          "nodes": {"assistant": {"name": "assistant", "node_type": "conversation",
                                  "task_message": "Help the caller with the services.", "tools": tools},
                    "end": {"name": "end", "node_type": "end_call", "terminal": {}}}}
    if meta:
        wf["meta"] = meta
    return wf


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Fakes:
    def __init__(self, clock, policy, meta=None, generate_fails=0, schema=None):
        self.clock = clock
        self.policy = policy
        self.meta = meta
        self.generate_fails = generate_fails
        self.schema_mod = schema or S
        self.purposes, self.saved, self.generated = [], [], []
        self.casegen_calls, self.testers = [], []
        self.hooks = []

    def deps(self):
        f = self

        def ask_json(prompt, **kw):
            f.purposes.append(kw.get("purpose", ""))
            f.clock.advance(20)
            if kw.get("purpose", "").startswith("write node prompts"):
                return {"edits": [{"step": "assistant", "op": "append", "new": "Keep answers short."}]}
            if kw.get("purpose", "").startswith("fix policy"):
                return {}
            return {}

        llm = types.SimpleNamespace(on_call=lambda fn: f.hooks.append(fn) or fn, remove_hook=lambda fn: None,
                                    ask_json=ask_json, fast_model=lambda: "fake-fast")

        def extract_policy(paths, openapi, budget_s=None, warnings=None, replicates=2):
            f.clock.advance(60)
            return copy.deepcopy(f.policy)
        extract = types.SimpleNamespace(extract_policy=extract_policy,
                                        read_documents=lambda paths: [("brief.md", "Rule one.")],
                                        documents_block=lambda docs: "=== brief.md ===\nRule one.",
                                        parse_openapi=lambda path: {"tools": copy.deepcopy(f.policy.get("tools") or {})},
                                        numbered_rules=lambda docs: [{"id": "1", "text": "Rule one."}])

        def generate_workflow(policy, prompts=None, openapi=None):
            f.generated.append(policy)
            if len(f.generated) <= f.generate_fails:
                raise ValueError("policy has no verify_identity tool")
            return generic_workflow(policy, f.meta)
        generate = types.SimpleNamespace(generate_workflow=generate_workflow)

        def save_workflow(slot, wf, state_file=None):
            f.saved.append(copy.deepcopy(wf))
            return {"workflow_id": "wf_g", "agent_id": "ag_g", "version": len(f.saved)}

        def export_workflow(wid, path):
            pathlib.Path(path).write_text(json.dumps({"workflow_id": wid}))
            return path
        freya = types.SimpleNamespace(validate_workflow=lambda name, wf: {"ok": True}, save_workflow=save_workflow,
                                      export_workflow=export_workflow)

        def generate_cases(policy, **kw):
            f.casegen_calls.append(policy)
            return [{"id": "SELF-01", "family": "F"}], []
        casegen = types.SimpleNamespace(generate_cases=generate_cases)

        class Tester:
            def __init__(self, *a, **k):
                f.testers.append(a)

            def run(self, wf, cases, **kw):
                return {"ok": True, "passed": 1, "total": 1, "rows": [{"id": "SELF-01", "pass": True}]}

        return Deps(llm=llm, extract=extract, schema=self.schema_mod, generate=generate, freya=freya,
                    casegen=casegen, selftester=Tester)


class GenericSchemaTests(unittest.TestCase):
    def test_generic_policy_validates_clean(self):
        p = generic_policy()
        self.assertTrue(S.is_generic(p))
        self.assertEqual(S.validate(p), [])
        errors, warnings = S.check(p)
        self.assertEqual(errors, [])
        self.assertTrue(any(w.startswith("generic policy") for w in warnings), warnings)
        # the collection sections may be missing altogether
        bare = {"tools": p["tools"], "rules": p["rules"]}
        self.assertEqual(S.validate(bare), [])

    def test_generic_policy_needs_rules(self):
        p = generic_policy()
        p["rules"] = []
        self.assertEqual(S.validate(p), ["rules is empty: a generic workflow follows the brief's rules"])
        del p["rules"]
        self.assertIn("missing top-level key: rules", S.validate(p))

    def test_collection_policies_unchanged(self):
        for f in ("fixture-a", "brief-a", "brief-b"):
            p = json.loads((FIX / (f + ".policy.json")).read_text())
            self.assertFalse(S.is_generic(p), f)
            self.assertEqual(S.validate(p), [], f)
        p = json.loads((FIX / "fixture-a.policy.json").read_text())
        p["bands"] = []
        self.assertIn("bands is empty", S.validate(p))              # collection checks still apply

    def test_roles_by_keyword_like_the_generator(self):
        tools = {"verifyCaller": {}, "getAccountInfo": {}, "createPromise": {}, "closeCall": {}}
        self.assertFalse(S.is_generic({"tools": tools}))
        self.assertEqual(S.role_tools({"tools": tools})["get_account"], "getAccountInfo")
        del tools["createPromise"]
        self.assertTrue(S.is_generic({"tools": tools}))
        self.assertTrue(S.is_generic({"tools": {}}))
        self.assertTrue(S.is_generic({}))
        self.assertEqual(S.tool_names({"tools": {"base_url": "https://x", "a.b": {}}}), ["a.b"])

    def test_normalise_keeps_language_and_examples(self):
        n = S.normalise(generic_policy())
        self.assertEqual(n["language"], "German")
        ex = {v["name"]: v.get("example") for v in n["variables"]}
        self.assertEqual(ex, {"clientNumber": "100200", "now": "2026-10-12T10:30:00+02:00"})
        self.assertEqual(S.normalise({"language": {"name": "German", "code": "de"}})["language"], "German")
        self.assertEqual(S.normalise({"language": ["German"]})["language"], "German")
        self.assertEqual(S.normalise({})["language"], "English")
        self.assertEqual(S.normalise({"variables": [{"name": "n", "example": 5}]})["variables"][0]["example"], 5)


class GenericHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expand_briefs_recursive_like_read_documents(self):
        root = self.dir / "case"
        for rel in ("BRIEF.md", "sources/emails/2026-10-01-mail.md", "sources/documents/faq.md", "flow.pdf",
                    ".hidden.md", ".git/config", "sources/~$lock.docx", "__MACOSX/x.md", "sources/.DS_Store"):
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        got = [str(pathlib.Path(p).relative_to(root)) for p in pipeline.expand_briefs([str(root)])]
        self.assertEqual(got, ["BRIEF.md", "flow.pdf", "sources/documents/faq.md", "sources/emails/2026-10-01-mail.md"])
        one = root / "BRIEF.md"
        self.assertEqual(pipeline.expand_briefs([str(one)]), [str(one)])

    def test_openapi_tool_names(self):
        self.assertEqual(pipeline.openapi_tool_names(GENERIC_OPENAPI),
                         ["parcels.list", "parcels.status.get", "delivery.slot.change", "receipt.send"])
        self.assertEqual(pipeline.openapi_tool_names({"paths": {"/a/b": {"get": {}, "parameters": []}}}), ["b"])
        self.assertEqual(pipeline.openapi_tool_names(None), [])

    def test_generic_reason(self):
        self.assertTrue(pipeline.generic_reason({}, GENERIC_OPENAPI))
        self.assertTrue(pipeline.generic_reason(generic_policy(), {}))
        self.assertEqual(pipeline.generic_reason({}, COLLECTION_OPENAPI), "")
        self.assertEqual(pipeline.generic_reason({}, {}), "")                 # no tools: no evidence, old behaviour
        self.assertTrue(pipeline.generic_reason({}, COLLECTION_OPENAPI, {"meta": {"generic": True}}))
        self.assertEqual(pipeline.workflow_meta({"meta": "x"}), {})

    def test_prompt_request_names_the_call_language(self):
        req = prompts_mod.build_request("brief", generic_policy(), generic_workflow(generic_policy()))
        self.assertIn("LANGUAGE OF THE CALLS:\nGerman", req)
        self.assertIn("verbatim", req)
        self.assertNotIn("state the debt", prompts_mod.RULES)
        self.assertNotIn("account data", prompts_mod.RULES)
        self.assertEqual(prompts_mod.call_language({"language": {"name": "German"}}), "German")
        self.assertEqual(prompts_mod.call_language({}), "")
        self.assertIn("not stated", prompts_mod.build_request("brief", {}, {}))


class GenericBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.brief = self.dir / "case"
        (self.brief / "sources").mkdir(parents=True)
        (self.brief / "BRIEF.md").write_text("brief")
        (self.brief / "sources" / "mail.md").write_text("mail")
        self.openapi = self.dir / "openapi.json"
        self.openapi.write_text(json.dumps(GENERIC_OPENAPI))
        self.clock = Clock()
        self.env = dict(os.environ)
        os.environ["SCORE_LOG"] = str(self.dir / "results" / "log.jsonl")
        os.environ.pop(pipeline.TRACE_OUT_ENV, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def build(self, fakes, **kw):
        now0 = datetime.datetime(2026, 10, 18, 9, 0, tzinfo=datetime.timezone.utc)
        kw.setdefault("trace_dir", str(self.dir / "traces"))
        b = Builder([str(self.brief)], str(self.openapi), slot="case", out_dir=str(self.dir / "out"),
                    state_file=str(self.dir / "state.json"), deps=fakes.deps(), clock=self.clock,
                    now=lambda: now0 + datetime.timedelta(seconds=self.clock.t), seed=1, **kw)
        return b, b.run()

    def test_generic_build_ok_without_selftest_or_policy_fix(self):
        f = Fakes(self.clock, generic_policy())
        b, res = self.build(f)                                       # no --no-test
        self.assertEqual(res["status"], "ok")
        self.assertEqual(f.casegen_calls, [])                        # no casegen
        self.assertEqual(f.testers, [])                              # no simulation runner
        self.assertNotIn("fix policy validation errors", f.purposes)  # validate == [] -> no wasted LLM round
        self.assertIn("write node prompts", f.purposes)              # the prompt stage runs for a generic workflow
        self.assertEqual(len(f.saved), 1)
        wf = f.saved[0]
        self.assertIn("assistant", wf["nodes"])                      # the generated workflow, not the minimal one
        self.assertTrue(wf["nodes"]["assistant"]["task_message"].endswith("Keep answers short."))
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("validate: ok", txt)
        self.assertIn("generate: ok", txt)
        self.assertIn("generic service agent", txt)
        self.assertIn("self-test skipped: generic service agent", txt)
        self.assertIn("FINAL RESULT: status=ok", txt)
        self.assertEqual(sorted(b.read_briefs()), sorted([str(self.brief / "BRIEF.md"), str(self.brief / "sources" / "mail.md")]))

    def test_prompts_complete_marker_skips_prompt_stage(self):
        f = Fakes(self.clock, generic_policy(), meta={"generic": True, "prompts_complete": True})
        b, res = self.build(f)
        self.assertEqual(res["status"], "ok")
        self.assertNotIn("write node prompts", f.purposes)
        self.assertEqual(f.casegen_calls, [])
        txt = pathlib.Path(res["trace"]).read_text()
        self.assertIn("prompts stage skipped: the generated workflow carries complete prompts", txt)
        self.assertIn("marked generic by the generator", txt)

    def test_generator_failure_still_skips_selftest_and_keeps_variables(self):
        f = Fakes(self.clock, generic_policy(), generate_fails=99)
        b, res = self.build(f)
        self.assertEqual(res["status"], "ok")
        wf = f.saved[0]
        self.assertEqual(list(wf["nodes"]), ["conversation"])        # minimal last resort
        self.assertIn("Speak German", wf["system_prompt"])
        self.assertIn("clientNumber = {{clientNumber}}", wf["system_prompt"])
        self.assertEqual(wf["test_variables"], {"clientNumber": "100200", "now": "2026-10-12T10:30:00+02:00"})
        self.assertEqual(f.casegen_calls, [])

    def test_collection_build_still_runs_selftest(self):
        self.openapi.write_text(json.dumps(COLLECTION_OPENAPI))
        pol = {"tools": {n: {} for n in ("verify_identity", "get_account", "create_promise", "close_call")},
               "rules": [{"id": "1", "text": "x"}]}
        schema = types.SimpleNamespace(validate=lambda p: [])
        f = Fakes(self.clock, pol, schema=schema)
        b, res = self.build(f)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(f.casegen_calls), 1)
        self.assertNotIn("self-test skipped: generic", pathlib.Path(res["trace"]).read_text())

    def test_trace_out_fixed_path(self):
        out = self.dir / "traces" / "final.log"
        f = Fakes(self.clock, generic_policy())
        b, res = self.build(f, trace_out=str(out), no_test=True)
        self.assertEqual(res["trace"], str(out))
        txt = out.read_text()
        self.assertIn("BUILD TRACE", txt)
        self.assertIn("FINAL RESULT: status=ok", txt)
        self.assertEqual(json.loads((self.dir / "traces" / "final.json").read_text())["result"]["status"], "ok")
        self.assertEqual(sorted(p.name for p in (self.dir / "traces").iterdir()), ["final.json", "final.log"])

    def test_trace_out_from_environment(self):
        out = self.dir / "elsewhere" / "build.log"
        os.environ[pipeline.TRACE_OUT_ENV] = str(out)
        f = Fakes(self.clock, generic_policy())
        b, res = self.build(f, no_test=True)
        self.assertEqual(res["trace"], str(out))
        self.assertIn("FINAL RESULT: status=ok", out.read_text())
        self.assertFalse((self.dir / "traces").exists() and any((self.dir / "traces").iterdir()))


if __name__ == "__main__":
    unittest.main()
