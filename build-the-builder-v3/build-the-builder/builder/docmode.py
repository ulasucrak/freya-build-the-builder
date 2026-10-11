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
 "notes": ["anything about the services the agent must know: formats, codes, limits"]
}
Rules:
- Only services the agent itself calls during a call. Use paths, methods and field names exactly as documented.
- A field that always comes from a call variable gets from_variable; everything the conversation decides gets null.
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


def _docs_text(docs, limit=150000):
    parts = []
    for name, text in docs:
        parts.append("=== %s%s ===\n%s" % (name, (" (" + docs.dates[name] + ")") if getattr(docs, "dates", {}).get(name) else "", text))
    out = "\n\n".join(parts)
    return out[:limit]


def _braces_safe(text, variables):
    """Keep {{name}} only for call variables and our own w_* variables; any other double brace is broken up."""
    keep = set(variables) | {"w_calendar"}
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: "{{" + m.group(1) + "}}" if m.group(1) in keep else "{ " + m.group(1) + " }", str(text or ""))


def service_tool(svc, base_url, variables):
    """One api_call tool from an extracted service description."""
    props, required = {}, []
    for f in svc.get("fields") or []:
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        typ = f.get("type") if f.get("type") in ("string", "number", "integer", "boolean") else "string"
        var = f.get("from_variable")
        leaf = {"type": typ}
        if var and var in variables:
            leaf["default"] = "{{" + var + "}}"               # filled from the call variable, never by the model
        else:
            leaf["default"] = "{{" + name + "}}"
            leaf["description"] = str(f.get("description") or "")[:300]
            if f.get("required"):
                required.append(name)
        props[name] = leaf
    path = "/" + str(svc.get("path") or "").lstrip("/")
    desc = (str(svc.get("purpose") or "") + " Result: " + str(svc.get("response") or ""))[:1000]
    return {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
            "function": {"name": re.sub(r"[^a-z0-9_]", "_", str(svc.get("name") or "service").lower())[:60],
                         "handler": "__function__:api_call", "description": desc,
                         "parameters": {"type": "object", "properties": {
                             "url": {"type": "string", "default": base_url.rstrip("/") + path},
                             "method": {"type": "string", "default": str(svc.get("method") or "POST").upper()},
                             "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                             "body": {"type": "object", "properties": props, "required": required}}}}}


def assemble(api, flow, base_url):
    """Extracted services + written procedure -> a Freya workflow (deterministic)."""
    variables = [str(v.get("name")) for v in api.get("variables") or [] if v.get("name")]
    now_var = next((v for v in variables if v.lower() in ("now", "call_time", "calltime", "now_iso")), "now")
    rt = {"names": {"now_var": now_var}}
    tools = [service_tool(s, base_url, variables) for s in api.get("services") or []]
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


def build(briefs, base_url, slot, out_dir, trace_dir, state_file, budget_min=28.0, freya=None):
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
