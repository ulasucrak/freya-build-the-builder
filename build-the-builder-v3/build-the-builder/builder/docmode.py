"""Document mode: a brief folder WITHOUT a tool API file. The bank's services are described in the documents
(an integration document, data models, examples); the builder reads them, works out the services and the call
procedure with an LLM, and assembles a workflow whose tools call the given service base URL (--mock-url).

Generic on purpose: nothing here knows a bank, a case, a language or a service. Everything comes from the
documents and the command line.

    python build.py <case folder> --mock-url https://<mock>.workers.dev --slot final
"""
import json
import pathlib
import re
import time

from . import extract, generate as G, llm
from .trace import Budget, Trace, one_line

API_SYSTEM = ("You read a bank's project documents and extract the HTTP services a phone agent calls during a "
              "call. You answer with JSON only.")

API_PROMPT = """The documents below describe a phone assistant the bank wants and the bank's services (an integration
document, data models, service examples, mails, meeting notes, FAQ). They were written months apart and can disagree:
when they do, the later or explicitly corrected version wins (mails and meeting notes can correct a document).

Return one JSON object:
{
 "language": "the language the calls are in, as a BCP-47 tag, e.g. en-GB or tr-TR",
 "variables": [{"name": "exact name of a variable the call starts with", "meaning": "...", "example": "..."}],
 "services": [{
    "name": "a short snake_case tool name",
    "method": "the HTTP method",
    "path": "the path exactly as the integration document defines it, starting with /",
    "purpose": "when the agent calls it, in one or two sentences",
    "fields": [{"name": "exact field name", "type": "string|number|integer|boolean", "required": true,
                "description": "meaning and format", "from_variable": "name of a call variable that fills it, or null"}],
    "response": "the response fields the agent uses, their meaning, and every documented error or status (HTTP code too)"
 }],
 "envelope": {"fields_under": "the key the service fields are nested under in every request body, or null when they are top-level",
              "constant": {"other top-level parts of every request body with fixed values, e.g. metadata objects": "..."}},
 "notes": ["anything about the services the agent must know: formats, codes, limits"]
}
Rules:
- Only services the agent itself calls during a call. Use paths, methods and field names exactly as documented.
- A field that always comes from a call variable gets from_variable; a field the documents give one fixed value gets
  "fixed": "<that value>"; everything the conversation decides gets neither.
- Fields are the service's own fields (the ones inside the envelope), never the envelope keys.
- Copy nothing that is not in the documents.

DOCUMENTS:
{docs}
"""

FLOW_SYSTEM = ("You write the instructions of a bank's phone assistant from the bank's documents. The instructions "
               "are complete, exact and short. You answer with JSON only.")

FLOW_PROMPT = """Write the phone assistant for the call the documents below describe. The assistant has these tools
(already defined; you only decide when and how they are used):
{services}

Call variables (available as {{{{name}}}} in your texts): {variables}
The calls are in: {language}

Return one JSON object:
{{
 "system_prompt": "who the assistant is, the language, the tone and turn length, the facts it may use (tool results, call variables, the calendar), what it must never do",
 "procedure": "the complete call procedure as numbered steps: every branch of the flow, every rule, every sentence the bank requires word for word, which tool to call when and how to read its result, how each kind of call ends",
 "endings": [{{"name": "snake_case name of one way the call ends", "when": "..."}}]
}}
Rules:
- Write system_prompt and procedure in the language of the calls; keep tool names, field names and codes as they are.
- When documents disagree, the later or explicitly corrected version wins. Say nothing the documents do not support.
- Every date decision uses the call's date-time variable and the calendar the assistant is given; never the real clock.
- One tool call per message; wait for its result. Never say something was done unless the tool result says so.
- Facts about the customer come only from tool results; never invent amounts, dates or data.
- The call ends with the tool end_call_now (argument "ending": one of the ending names) in the same message as the goodbye.

DOCUMENTS:
{docs}
"""


TEST_PROMPT = """From the documents below, write up to {n} test calls for the bank's phone assistant: the example
conversations the documents contain first (as close to them as possible), then the most important other situations.
Each test uses one of the bank's test customers exactly as the documents list them.

Return one JSON object: {{"tests": [{{
  "id": "T1",
  "title": "short",
  "variables": {{"<call variable name>": "<value>"}},
  "caller": "who the caller is and exactly what they say and want, step by step, in the language of the calls",
  "success": ["checkable facts about what the assistant must say or do, from the documents"]
}}]}}
Call variables: {variables}

DOCUMENTS:
{docs}
"""

REPAIR_PROMPT = """The assistant below failed some test calls. Rewrite its procedure so that it handles them, without
breaking what works. Keep the format and language. Use only what the documents support.

Return one JSON object: {{"procedure": "the complete new procedure", "diagnosis": "one or two sentences"}}

CURRENT PROCEDURE:
{procedure}

FAILED TESTS (success criteria, the judge's reason, the transcript end):
{failures}

DOCUMENTS:
{docs}
"""

CALLER = ("OUTPUT RULE: Write only the words you say on the phone. No stage directions. One or two short sentences per "
          "turn, then wait.\n\nWHO YOU ARE AND WHAT HAPPENS: {caller}\n\nWhen the assistant says goodbye, say goodbye and nothing more.")


def run_tests(freya, agent_id, tests, label, trace, max_wait_s):
    """One simulation of the tests; returns rows [{id, pass, reason, transcript}] (Freya's judge decides)."""
    specs = [{"name": "%s %s" % (t["id"], str(t.get("title") or "")[:40]), "prompt": CALLER.format(caller=t.get("caller") or ""),
              "successCriteria": "All of these are true:\n- " + "\n- ".join(t.get("success") or ["the call is handled correctly"]),
              "tags": ["docmode-selftest"]} for t in tests]
    made = freya.create_personas(specs)["personas"]
    ids = {p["id"]: t for p, t in zip(made, tests)}
    t0 = time.time()
    sim_id, it_id, status = freya.run_simulation(agent_id, label, {pid: dict(t.get("variables") or {}) for pid, t in ids.items()})
    rows = []
    for run in freya.get_iteration(sim_id, it_id)["runs"]:
        t = ids.get(run.get("personaId"))
        if not t:
            continue
        tx = ""
        try:
            call = freya.get_call(run["callId"])
            tx = "\n".join("%s: %s" % (m["role"][0].upper(), m.get("message", "")[:300]) for m in call.get("messages", [])
                           if m.get("role") in ("assistant", "user"))[-2500:]
        except Exception:
            pass
        rows.append({"id": t["id"], "pass": run.get("outcome") == "pass", "reason": str(run.get("reason") or "")[:600],
                     "success": t.get("success"), "transcript": tx})
    trace.test_run(label, cases=[t["id"] for t in tests], passed=sum(r["pass"] for r in rows), total=len(rows),
                   duration_s=round(time.time() - t0, 1), detail="simulation %s, status %s" % (sim_id, status))
    return rows


def _docs_text(docs, limit=150000, per_doc=40000):
    parts = []
    for name, text in docs:
        if len(text) > per_doc:                               # a big data file must not push the documents out
            text = text[:per_doc] + "\n[... %d more characters of this file not shown]" % (len(text) - per_doc)
        parts.append("=== %s%s ===\n%s" % (name, (" (" + docs.dates[name] + ")") if getattr(docs, "dates", {}).get(name) else "", text))
    out = "\n\n".join(parts)
    return out[:limit]


def _braces_safe(text, variables):
    """Keep {{name}} only for call variables and our own w_* variables; any other double brace is broken up."""
    keep = set(variables) | {"w_calendar"}
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: "{{" + m.group(1) + "}}" if m.group(1) in keep else "{ " + m.group(1) + " }", str(text or ""))


def _join(base_url, path):
    """Base URL + service path, without doubling a path prefix the base already has (…/v1 + /v1/x -> …/v1/x)."""
    m = re.match(r"(https?://[^/]+)(/.*)?$", base_url.rstrip("/"))
    host, bpath = (m.group(1), m.group(2) or "") if m else (base_url.rstrip("/"), "")
    return host + (path if bpath and path.startswith(bpath + "/") else bpath + path)


def service_tool(svc, base_url, variables, rt=None):
    """One api_call tool from an extracted service description."""
    props, required = {}, []
    for f in svc.get("fields") or []:
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        typ = f.get("type") if f.get("type") in ("string", "number", "integer", "boolean") else "string"
        var = f.get("from_variable")
        leaf = {"type": typ}
        if f.get("fixed") not in (None, ""):
            leaf["default"] = f.get("fixed")                    # a value the documents fix: never the model's
        elif var and var in variables:
            leaf["default"] = "{{" + var + "}}"               # filled from the call variable, never by the model
        else:
            leaf["default"] = "{{" + name + "}}"
            leaf["description"] = str(f.get("description") or "")[:300]
            if f.get("required"):
                required.append(name)
        props[name] = leaf
    path = "/" + str(svc.get("path") or "").lstrip("/")
    if re.match(r"https?://", str(svc.get("path") or "")):    # a full URL in the documents: keep only its path
        path = "/" + re.sub(r"^https?://[^/]+/?", "", str(svc.get("path")))
    desc = (str(svc.get("purpose") or "") + " Result: " + str(svc.get("response") or ""))[:1000]
    return {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
            "function": {"name": re.sub(r"[^a-z0-9_]", "_", str(svc.get("name") or "service").lower())[:60],
                         "handler": "__function__:api_call", "description": desc,
                         "parameters": {"type": "object", "properties": {
                             "url": {"type": "string", "default": _join(base_url, path)},
                             "method": {"type": "string", "default": str(svc.get("method") or "POST").upper()},
                             "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                             "body": {"type": "object", "properties": props, "required": required}}}},
            **({"pre_process_code": G.build_code("doc_wrap", rt, "pre")} if rt and (rt.get("envelope") or {}).get("fields_under") else {})}


def _clean_envelope(env):
    """Constant envelope parts must be JSON values. A description the LLM wrote instead ('Call metadata object
    {"lang": "tr"} ...') is reduced to the JSON object inside it, or dropped."""
    env = dict(env or {})
    const = {}
    for k, v in (env.get("constant") or {}).items():
        if isinstance(v, str):
            m = re.search(r"\{.*\}", v, re.S)
            try:
                v = json.loads(m.group(0)) if m else None
            except ValueError:
                v = None
        if v is not None:
            const[k] = v
    env["constant"] = const
    return env


def assemble(api, flow, base_url):
    """Extracted services + written procedure -> a Freya workflow (deterministic)."""
    variables = [str(v.get("name")) for v in api.get("variables") or [] if v.get("name")]
    now_var = next((v for v in variables if v.lower() in ("now", "call_time", "calltime", "now_iso")), "now")
    rt = {"names": {"now_var": now_var}, "envelope": _clean_envelope(api.get("envelope"))}
    tools = [service_tool(s, base_url, variables, rt) for s in api.get("services") or []]
    endings = [str(e.get("name")) for e in flow.get("endings") or [] if e.get("name")]
    end_tool = G._code_tool("end_call_now", "End the call: CALL it in the same message as your goodbye, once, as the very last action.",
                            {"ending": "How the call ended: one of " + ", ".join(endings or ["done"]) + "."}, "doc_end", rt)
    task = _braces_safe(flow.get("procedure"), variables) + \
        "\n\nCalendar (use these weekdays and dates; never work out a weekday yourself):\n{{w_calendar}}"
    nodes = {
        "setup": G._code_node("setup", "doc_setup", rt, "assistant"),
        "assistant": {"name": "assistant", "node_type": "conversation",
                      "on_enter": {"type": "llm", "prompt": "Start the call exactly as the procedure says for the first turn."},
                      "on_enter_first_entry_only": True, "task_message": task, "tools": tools + [end_tool],
                      "max_node_turns": 60, "max_turn_transition": "end",
                      "transitions": [{"target_node": "end", "description": "the assistant ended the call",
                                       "condition": {"logic": "or", "conditions": [{"variable": "w_end", "operator": "equals", "operand": "yes"}]}}]},
        "end": G._end_node("end", "call_ended"),
    }
    wf = {"initial_node": "setup", "system_prompt": _braces_safe(flow.get("system_prompt"), variables), "nodes": nodes, "global_nodes": {}}
    tv = {str(v["name"]): str(v.get("example")) for v in api.get("variables") or [] if v.get("name") and v.get("example")}
    if tv:
        wf["test_variables"] = tv
    G._strip_nulls(wf)
    return wf


def build(briefs, base_url, slot, out_dir, trace_dir, state_file, budget_min=28.0, freya=None, no_test=False, max_tests=6):
    """Run document mode end to end. Returns the same shape of result as pipeline.Builder.run()."""
    if freya is None:
        import freya as freya_mod
        freya = freya_mod
    budget = Budget(budget_min * 60)
    trace = Trace(slot, trace_dir)
    hook = llm.on_call(trace.model_call)
    saved, export, status, err = {}, None, "failed", ""
    try:
        trace.set_info(briefs=", ".join(map(str, briefs)), mode="document mode (no tool API file: services from the documents)",
                       service_base_url=base_url, slot=slot, agent_model_rule="phone agent runs on freya.AGENT_LLM (unchanged)")
        if not base_url:
            raise ValueError("document mode needs --mock-url (the base URL of the bank's services)")
        docs = extract.read_documents(briefs)
        trace.stage("read", "ok", None, "%d documents" % len(docs))
        text = _docs_text(docs)
        t0 = time.time()
        api = llm.ask_json(API_PROMPT.replace("{docs}", text), system=API_SYSTEM, purpose="document mode: extract services and variables",
                           timeout_s=min(420, budget.remaining() - 300))
        trace.stage("services", "ok", time.time() - t0, "%d services, %d variables, language %s" % (
            len(api.get("services") or []), len(api.get("variables") or []), api.get("language")))
        t0 = time.time()
        svc_txt = json.dumps([{k: s.get(k) for k in ("name", "method", "path", "purpose", "response")} for s in api.get("services") or []], ensure_ascii=False)
        flow = llm.ask_json(FLOW_PROMPT.format(services=svc_txt, variables=json.dumps(api.get("variables") or [], ensure_ascii=False),
                                               language=api.get("language") or "the documents' language", docs=text),
                            system=FLOW_SYSTEM, purpose="document mode: write the call procedure", timeout_s=min(600, budget.remaining() - 180))
        trace.stage("procedure", "ok", time.time() - t0, "%d chars, %d endings" % (len(flow.get("procedure") or ""), len(flow.get("endings") or [])))
        wf = assemble(api, flow, base_url)
        out = pathlib.Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / (slot + ".services.json")).write_text(json.dumps(api, indent=1, ensure_ascii=False))
        (out / (slot + ".workflow.local.json")).write_text(json.dumps(wf, indent=1, ensure_ascii=False))
        try:
            freya.validate_workflow(slot, wf)
            trace.note("dry-run validation: ok")
        except Exception as e:
            trace.note("dry-run validation rejected: %s" % one_line(e, 300))
            raise
        saved = freya.save_workflow(slot, wf, state_file=state_file)
        trace.freya("saved phone agent", slot=slot, workflow_id=saved.get("workflow_id"), agent_id=saved.get("agent_id"), version=saved.get("version"))
        export = str(out / (slot + ".workflow.json"))
        freya.export_workflow(saved["workflow_id"], export)
        trace.freya("exported", workflow_id=saved.get("workflow_id"), path=export)
        status = "ok"
        if no_test or budget.remaining() < 420:
            trace.note("self-test skipped (%s)" % ("--no-test" if no_test else "not enough time"))
        else:
            try:
                variables = [str(v.get("name")) for v in api.get("variables") or [] if v.get("name")]
                tests = llm.ask_json(TEST_PROMPT.format(n=max_tests, variables=json.dumps(variables), docs=text),
                                     system=API_SYSTEM, purpose="document mode: write self-test calls",
                                     timeout_s=min(300, budget.remaining() - 300)).get("tests") or []
                tests = [t for t in tests if isinstance(t, dict) and t.get("caller")][:max_tests]
                rows = run_tests(freya, saved["agent_id"], tests, "self-test v1", trace, budget.remaining() - 200) if tests else []
                failed = [r for r in rows if not r["pass"]]
                if failed and budget.remaining() > 360:
                    fix = llm.ask_json(REPAIR_PROMPT.format(procedure=flow.get("procedure"), docs=text, failures=json.dumps(
                        [{k: r[k] for k in ("id", "success", "reason", "transcript")} for r in failed], ensure_ascii=False)),
                        system=FLOW_SYSTEM, purpose="document mode: repair the procedure from failed tests",
                        timeout_s=min(400, budget.remaining() - 240))
                    if fix.get("procedure"):
                        wf2 = assemble(api, dict(flow, procedure=fix["procedure"]), base_url)
                        freya.validate_workflow(slot, wf2)
                        trace.note("repair 1: %s" % one_line(fix.get("diagnosis") or "", 300))
                        saved2 = freya.save_workflow(slot, wf2, state_file=state_file)
                        rows2 = run_tests(freya, saved2["agent_id"], tests, "self-test v2 (after repair 1)", trace, budget.remaining() - 120)
                        if sum(r["pass"] for r in rows2) > sum(r["pass"] for r in rows):
                            saved, wf = saved2, wf2
                            (out / (slot + ".workflow.local.json")).write_text(json.dumps(wf, indent=1, ensure_ascii=False))
                            trace.note("repair 1 kept: %d -> %d passing" % (sum(r["pass"] for r in rows), sum(r["pass"] for r in rows2)))
                        else:
                            freya.save_workflow(slot, json.loads((out / (slot + ".workflow.local.json")).read_text()), state_file=state_file)
                            trace.note("repair 1 discarded: no gain; the first version is saved again")
                        freya.export_workflow(saved["workflow_id"], export)
                        trace.freya("exported", workflow_id=saved.get("workflow_id"), path=export)
            except Exception as e:  # noqa: BLE001
                trace.note("self-test failed (the saved agent stays): %s" % one_line(e, 300))
    except Exception as e:  # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, one_line(e, 400))
        trace.note("error: " + err)
    finally:
        trace.finish(status, workflow_id=saved.get("workflow_id"), agent_id=saved.get("agent_id"), export=export,
                     build_seconds=round(budget.elapsed()), error=err or None)
        try:
            llm.remove_hook(hook)
        except Exception:
            pass
    return {"status": status, "error": err, "saved": saved, "export": export, "trace": str(trace.path)}
