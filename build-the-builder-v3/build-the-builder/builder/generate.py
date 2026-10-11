"""Deterministic generator: policy.json (contract v1, see policy_schema.py) -> Freya workflow JSON.

    from builder.generate import generate_workflow
    wf = generate_workflow(policy, prompts=None, openapi=None, options=None)

    python -m builder.generate policies/x.policy.json -o out/x.workflow.json [--openapi tools/openapi.json]
                               [--prompts prompts.json] [--validate] [--save dev-T04-a]

Graph (every value comes from the policy; nothing here knows a brief):

    setup (code)            date facts from the call's "now" variable + calendar, sentinel values
      -> greet_verify       opening, who is on the line, third-party cases, security questions
                            tools: verify_identity, schedule_callback, transfer_to_agent, close_call
         -> load_account    (api_request) get_account, when the identity check returned all-verified
         -> prepare_account (code) per product: band, deadline, floor/full, offers -> {{w_account_facts}}; the first
                            line of negotiate -> {{w_entry_line}} (option static_entry, when the policy fixes it)
         -> negotiate       debt statement (tts {{w_entry_line}}, else the model), negotiation, read-back, promises,
                            notes, triggers, closing
                            tools: create_promise, add_note, transfer_to_agent, close_call
                            extraction x_reply (+ x_product): refusal / too_much / no_talk / dispute / other
            -> turn_state   (code) a labelled message: per-product refusal ladder, 'too much', the N-th 'do not want
                            to talk', disputes -> next offer in w_next_action and back (on_enter {{w_enter_prompt}}),
                            or close_check when the ladder / the count is used up (option extract_signals)
      close_call result     -> end_closed (end_call)
      code-forced close     -> close_check -> finish -> farewell (tts goodbye) -> close_api -> end_closed
                            (also schedule_callback's last stopped try); turn_state also counts turns -> turn_cap
      goodbye without close -> goodbye_close (code) -> close_check -> finish -> close_api (routing intent; option goodbye_close)
      turn cap reached      -> turn_cap (code) -> close_check -> finish -> farewell (tts goodbye) -> close_api -> end_forced
    workflow tool final_close (end_of_call): safety-net close_call when none was sent (stopped otherwise).

Hooks (python, embedded from builder/oracle.py + builder/codegen/*.py) normalise answers/dates/amounts, stop
requests that break the policy before they reach the bank, add `what_to_do_next` to every tool result, and
derive close_call's outcome/reason from recorded facts (transfer, failed identity, promise, callback, notes).

`prompts` (all optional; generated defaults are used for anything missing). Flat keys or nested
{"nodes": {node: {"task": ..}}} are both accepted:
    system_prompt                  replaces the global system prompt
    opening_line                   fixed first sentence (spoken without a model call); {{var}} allowed
    greet_verify.task / .on_enter / .extra
    negotiate.task / .on_enter / .extra
    tool.<tool name>               replaces a tool description
    farewell                       spoken by the farewell node before a code-decided close (turn cap too)
"""
import argparse
import ast
import copy
import json
import os
import pathlib
import re
import sys

try:
    from . import policy_schema
except ImportError:  # run as a plain script
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from builder import policy_schema

HERE = pathlib.Path(__file__).resolve().parent
CODEGEN = HERE / "codegen"
BLOCK_URL = "https://blocked.invalid/not-sent"

ROLES = {
    "verify": ("verify_identity", ["verify", "identity"]),
    "account": ("get_account", ["account"]),
    "promise": ("create_promise", ["promise"]),
    "note": ("add_note", ["note"]),
    "callback": ("schedule_callback", ["callback"]),
    "transfer": ("transfer_to_agent", ["transfer"]),
    "close": ("close_call", ["close"]),
}

DEFAULT_OPTIONS = {
    "guard": True,                 # stop policy-breaking create_promise/schedule_callback/add_note requests
    "final_close_fallback": False,  # end_of_call safety-net close_call; off until a live run proves it is suppressed after a close
    "greet_max_turns": 30,
    "negotiate_max_turns": 80,
    "timeout_ms": 12000,
    "check_tool": True,            # check_offer: code_exec tool that checks a customer's own date/amount before the read-back
    "goodbye_close": False,        # both sides said goodbye but no close_call came: the engine closes. Off: needs a
                                   # routing-model call (matches_intent) on every turn; no deterministic signal exists
    # v4 enforcement in code, no model-callable tool added:
    "extract_signals": False,      # negotiate: the per-turn extraction labels the customer's last message (refusal,
                                   # too much, does not want to talk, dispute); a code node counts per product and
                                   # writes the next offer, closes when the ladder or the 'twice' count is used up.
                                   # Off by default: the extraction is a model call before every negotiate reply
                                   # (live v4: median 3 s, up to 12.5 s; callers hung up after ~60 s of silence)
    "enforce_callback": True,      # schedule_callback: after max_asks stopped tries the engine closes (no_callback)
    "static_entry": True,          # negotiate's first message (one sentence per product + the first payment question)
                                   # is built by prepare_account and spoken as a fixed line (tts), when the policy fixes
                                   # its content (static_entry); the slowest regular turn (median 3.4 s, p90 10.7 s)
    "reasoning_off": False,        # reasoning_override "disabled" on the conversation nodes. Untested live: off
}

# Safety net (HANDOFF TP-09: a goodbye loop of ~50 turns without close_call). Judged by the routing model on the
# conversation; it fires only after the agent's own goodbye, so a customer's early "bye" mid-call does not end it.
GOODBYE_CLOSE = 'vset(vs, "w_goodbye_close", "yes")\n'   # code node: marks the call, then close_check
GOODBYE_INTENT = ("The agent's previous message already said goodbye to end the call, and the user's last message only "
                  "says goodbye, thanks or a similar farewell back, with no question, request or new information.")


# ----------------------------------------------------------------------------------------------- code embedding

def _embed_region(src):
    m = re.search(r"# EMBED-BEGIN\n(.*?)# EMBED-END", src, re.S)
    return m.group(1) if m else src


def _defs(src):
    """Top-level definitions of a source -> list of (names, code, refs, is_import), docstrings removed."""
    tree = ast.parse(src)
    lines = src.splitlines()
    out = []
    for node in tree.body:
        start, end = node.lineno, node.end_lineno
        if isinstance(node, (ast.FunctionDef,)):
            names = {node.name}
            body_lines = lines[start - 1:end]
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str) and len(node.body) > 1:
                ds, de = first.lineno - start, first.end_lineno - start
                body_lines = body_lines[:ds] + body_lines[de + 1:]
            code = "\n".join(body_lines)
        elif isinstance(node, (ast.Assign,)):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            code = "\n".join(lines[start - 1:end])
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append((set(), "\n".join(lines[start - 1:end]), set(), True))
            continue
        else:
            continue
        refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        out.append((names, code, refs, False))
    return out


_LIB = None


def _library():
    global _LIB
    if _LIB is None:
        oracle_src = _embed_region((HERE / "oracle.py").read_text())
        common_src = (CODEGEN / "common.py").read_text()
        _LIB = _defs(oracle_src) + _defs(common_src)
    return _LIB


def _squeeze(line):
    """One space per indentation level (the sources use 4) and no trailing blanks. The code runs inside a sandbox
    whose payload (code + variables) is limited, so every byte of the embedded snippets counts. Lines continued
    inside brackets may get any indentation; statement lines keep a consistent one."""
    stripped = line.lstrip(" ")
    n = len(line) - len(stripped)
    return " " * (n // 4 + n % 4) + stripped.rstrip()


def _py_literal(text):
    """A compact Python string literal for JSON text, with no doubled braces in it.

    Single-quoted, so the JSON's double quotes need no escaping (the hooks are sometimes embedded once more as a
    string, and every escape doubles there). A doubled brace is written with one escaped brace, which Python turns
    back into the same character, so the value is exact."""
    lit = text.replace("\\", "\\\\").replace("'", "\\'")
    while "}}" in lit:
        lit = lit.replace("}}", "}\\u007d")
    while "{{" in lit:
        lit = lit.replace("{{", "{\\u007b")
    return "'" + lit + "'"


def build_code(body_name, rt, mode, source=None):
    """Python source for a code node ('node'), pre-hook ('pre') or post-hook ('post').

    Everything runs inside one function so that helper functions see each other whatever globals/locals the
    sandbox uses; the call is wrapped in try/except so a snippet never raises."""
    body = source if source is not None else (CODEGEN / (body_name + ".py")).read_text()
    lib = _library()
    need = {n.id for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Name)}
    defined = set()
    for names, _c, _r, _i in lib:
        defined |= names
    want = set()
    stack = [n for n in need if n in defined]
    while stack:
        n = stack.pop()
        if n in want:
            continue
        want.add(n)
        for names, _c, refs, _i in lib:
            if n in names:
                stack.extend(r for r in refs if r in defined and r not in want)
    parts = []
    for names, code, _r, is_import in lib:
        if not is_import and names & want:
            parts.append(code)
    parts.append(body)
    inner = "\n".join(parts)
    # only the RT sections the code mentions by name ("names" always)
    sub = {k: v for k, v in rt.items() if k == "names" or ('"%s"' % k) in inner}
    rt_line = "RT = json.loads(" + _py_literal(json.dumps(sub, separators=(",", ":"), ensure_ascii=True)) + ")"
    inner = "\n".join(["import datetime", "import json", "import re", rt_line, inner])
    inner = "\n".join(_squeeze(ln) for ln in inner.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    inner = "\n".join((" " + ln) if ln.strip() else "" for ln in inner.splitlines())
    # 'tool': a code_exec tool the model calls; its arguments are the sandbox global `params`, and the snippet's
    # top-level `return` is the tool result (verified for code_exec tools, see testmode.py)
    extra = {"node": "None", "pre": "config", "post": "result", "tool": "params"}[mode]
    run = ("try:\n    freya_main(freya_vs, freya_extra)\nexcept Exception:\n    pass\n" if mode != "tool" else
           "freya_out = dict(ok=False, code='error', what_to_do_next='The check did not work. Use the account facts to decide.')\n"
           "try:\n    freya_out = freya_main(freya_vs, freya_extra) or freya_out\nexcept Exception:\n    pass\nreturn freya_out\n")
    if mode == "tool":
        inner += "\n return out"
    src = ("def freya_main(vs, extra):\n" + inner + "\n\n\n"
           "try:\n    freya_vs = vars if hasattr(vars, 'get') else dict()\nexcept Exception:\n    freya_vs = dict()\n"
           "try:\n    freya_extra = " + extra + "\nexcept Exception:\n    freya_extra = None\n" + run)
    assert "{{" not in src and "}}" not in src, body_name
    return src


# ----------------------------------------------------------------------------------------------- openapi / tools

def _openapi_tools(openapi):
    """openapi dict -> {tool: {url, method, fields, response}} (only what the generator needs)."""
    out = {}
    if not isinstance(openapi, dict):
        return out
    servers = openapi.get("servers") or []
    base = (servers[0].get("url") if servers and isinstance(servers[0], dict) else "") or ""
    for path, ops in (openapi.get("paths") or {}).items():
        for method, op in (ops or {}).items():
            if not isinstance(op, dict):
                continue
            name = op.get("operationId") or path.strip("/").split("/")[-1]
            schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            req = set(schema.get("required") or [])
            fields = {f: {"type": s.get("type") or "string", "enum": s.get("enum"), "required": f in req,
                          "description": s.get("description") or ""}
                      for f, s in (schema.get("properties") or {}).items()}
            r200 = ((op.get("responses") or {}).get("200") or {})
            rs = (((r200.get("content") or {}).get("application/json") or {}).get("schema") or {}).get("properties") or {}
            out[name] = {"url": base.rstrip("/") + path, "method": method.upper(), "fields": fields, "response": rs}
    return out


def _merge_openapi(policy, openapi):
    p = copy.deepcopy(policy)
    tools = p.setdefault("tools", {})
    for name, t in _openapi_tools(openapi).items():
        cur = tools.setdefault(name, {})
        cur.setdefault("url", t["url"])
        cur.setdefault("method", t["method"])
        if not cur.get("fields"):
            cur["fields"] = t["fields"]
        if not cur.get("response") and not cur.get("response_fields"):
            cur["response"] = t["response"]
    return p


def _tool_name(p, role):
    name, kws = ROLES[role]
    if name in p["tools"]:
        return name
    for t in p["tools"]:
        if any(k in t.lower() for k in kws):
            return t
    return ""


def _tool_url(p, name):
    t = p["tools"].get(name) or {}
    if t.get("url"):
        return t["url"]
    base = p["api"]["base_url"]
    path = t.get("path") or ("/" + name)
    if not base:
        raise ValueError("no URL for tool %s: give api.base_url, tools.%s.url or the openapi file" % (name, name))
    return base.rstrip("/") + "/" + path.lstrip("/")


def _resp(p, name):
    return (p["tools"].get(name) or {}).get("response") or {}


def _pick(fields, default, pred):
    for f, s in fields.items():
        if pred(f, s or {}):
            return f
    return default


def _names(p, opts):
    """Tool and field names the hooks use, taken from the policy's tool definitions."""
    t = {r: _tool_name(p, r) for r in ROLES}
    vr = _resp(p, t["verify"])
    is_bool = lambda s: (s.get("type") or "") == "boolean"
    vf = (p["tools"].get(t["verify"]) or {}).get("fields") or {}
    pf = (p["tools"].get(t["promise"]) or {}).get("fields") or {}
    nf = (p["tools"].get(t["note"]) or {}).get("fields") or {}
    cf = (p["tools"].get(t["callback"]) or {}).get("fields") or {}
    tf = (p["tools"].get(t["transfer"]) or {}).get("fields") or {}
    xf = (p["tools"].get(t["close"]) or {}).get("fields") or {}
    ok_of = lambda tool: _pick(_resp(p, tool), "ok", lambda f, s: f.lower() in ("ok", "success") and is_bool(s)) if _resp(p, tool) else "ok"
    names = {
        "now_var": p["clock"]["now_variable"],
        "next_weekday": p["dates"].get("next_weekday") or "",
        "guard": "on" if opts["guard"] else "off",
        "block_url": BLOCK_URL,
        "verify_tool": t["verify"], "promise_tool": t["promise"], "note_tool": t["note"],
        "callback_tool": t["callback"], "transfer_tool": t["transfer"], "close_tool": t["close"],
        "account_node": "load_account", "close_node": "close_api",
        "list_field": p["products"]["list_field"],
        "verify_all": _pick(vr, "all_verified", lambda f, s: is_bool(s) and "all" in f.lower()),
        "verify_one": _pick(vr, "verified", lambda f, s: is_bool(s) and "all" not in f.lower() and f.lower() not in ("ok", "success")),
        "verify_left": _pick(vr, "wrong_answers_left", lambda f, s: (s.get("type") or "") in ("integer", "number")),
        "verify_hint": _pick(vr, "hint", lambda f, s: "hint" in f.lower() or (bool(s.get("enum")) and f.lower() != "error")),
        "verify_ok": ok_of(t["verify"]),
        "verify_field": _pick(vf, "field", lambda f, s: bool(s.get("enum"))),
        "verify_answer": _pick(vf, "answer", lambda f, s: "answer" in f.lower() or "value" in f.lower()),
        "promise_ok": ok_of(t["promise"]),
        "promise_product": _pick(pf, "product_id", lambda f, s: "product" in f.lower()),
        "promise_amount": _pick(pf, "amount", lambda f, s: "amount" in f.lower() or (s.get("type") in ("number", "integer"))),
        "promise_date": _pick(pf, "date", lambda f, s: "date" in f.lower() or s.get("format") == "date"),
        "note_ok": ok_of(t["note"]),
        "note_product": _pick(nf, "product_id", lambda f, s: "product" in f.lower()),
        "note_reason": _pick(nf, "reason", lambda f, s: bool(s.get("enum"))),
        "callback_ok": ok_of(t["callback"]),
        "callback_date": _pick(cf, "date", lambda f, s: "date" in f.lower() or s.get("format") == "date"),
        "callback_time": _pick(cf, "time", lambda f, s: "time" in f.lower()),
        "transfer_ok": ok_of(t["transfer"]),
        "queue_field": _pick(tf, "queue", lambda f, s: bool(s.get("enum"))),
        "close_ok": ok_of(t["close"]),
        "close_outcome": _pick(xf, "outcome", lambda f, s: "outcome" in f.lower() or bool(s.get("enum"))),
        "close_reason": _pick(xf, "reason", lambda f, s: "reason" in f.lower()),
    }
    calls = set(_var_names(p))
    pr = p["products"]
    # the product id field of create_promise / add_note: named like a product, else the get_account id field
    for key, fields in (("promise_product", pf), ("note_product", nf)):
        if names[key] not in fields and fields:
            if pr["id_field"] in fields:
                names[key] = pr["id_field"]
            else:
                rest = [f for f, sp in fields.items() if f not in calls and not sp.get("enum")
                        and f not in (names["promise_amount"], names["promise_date"]) and (sp.get("type") or "string") == "string"]
                if rest:
                    names[key] = rest[0]
    return names


def runtime_policy(p, names):
    """The part of the policy the embedded code needs."""
    return {
        "names": names,
        "calendar": {k: p["calendar"][k] for k in ("weekend", "holidays", "roll_rule")},
        "bands": [{"name": b["name"], "min_days_late": b["min_days_late"], "max_days_late": b["max_days_late"],
                   "deadline": {k: v for k, v in b["deadline"].items() if k != "notes"},
                   "attempts": [{k: a.get(k) for k in ("n", "amount", "date", "text")} for a in b["attempts"]]}
                  for b in p["bands"]],
        "products": dict({k: p["products"][k] for k in ("id_field", "type_field", "days_late_field", "list_field", "max_count")},
                         types={k: {f: t.get(f) for f in ("speak_as", "floor_field", "floor_expr", "full_field") if t.get(f) is not None}
                                for k, t in p["products"]["types"].items()}),
        "callback": {k: p["callback"][k] for k in ("days", "start", "end", "end_inclusive", "must_be_after_now", "max_asks")},
        "dates": {"next_weekday": p["dates"].get("next_weekday") or ""},
        "identity": {"questions": [dict({k: q.get(k) for k in ("order", "tool_field", "ask", "answer_format", "normalise")},
                                        digits=_expected_digits(q, p) if q.get("normalise") == "digits_only" or q.get("answer_format") == "digits" else 0)
                                   for q in p["identity"]["questions"]],
                     "max_wrong": p["identity"]["max_wrong"], "fail": p["identity"]["fail"],
                     "wrong_counts_rules": p["identity"]["wrong_counts_rules"]},
        "promise": {k: p["promise"][k] for k in ("retry_on_failure", "success_say", "failure_say")},
        "notes": {k: p["notes"][k] for k in ("enabled", "tool_reasons", "forbidden_on_transfer", "min_products")},
        "closing": dict({k: v for k, v in p["closing"].items() if k != "notes"},
                        pre_verification_outcomes=sorted(_pre_verification_outcomes(p))),
    }


def _expected_digits(q, p):
    """How many digits an answer has: from the question text, a number at the end of the field name (..._last4),
    else the tool's field descriptions (0 = unknown)."""
    from builder import oracle
    n = oracle.expected_digits(q.get("ask") or "")
    if n:
        return n
    m = re.search(r"(\d+)$", q.get("tool_field", ""))
    if m and 0 < int(m.group(1)) < 13:
        return int(m.group(1))
    verify = _tool_name(p, "verify")
    for spec in ((p["tools"].get(verify) or {}).get("fields") or {}).values():
        n = oracle.expected_digits(spec.get("description") or "")
        if n:
            return n
    return 0


def _var_names(p):
    return [v["name"] for v in p["variables"]]


def _field_desc(spec, extra=""):
    d = (spec.get("description") or "").strip()
    if spec.get("enum"):
        d = (d + " " if d else "") + "One of: " + ", ".join(spec["enum"]) + "."
    if extra and d and d.rstrip(".").lower() in extra.lower():
        d = extra                       # the hint already says it all ("HH:MM" + "HH:MM, 24 h")
    elif extra and extra.rstrip(".").lower() not in d.lower():
        d = (d + " " if d else "") + extra
    return d


def _api_tool(p, name, description, field_hints=None, baked=None, pre=None, post=None, rt=None, retries=0,
              timeout=12000, mapping=None, config_extra=None, hide=()):
    """An api_call tool from the policy's tool definition. Call variables with the field's name are baked,
    `baked` gives fixed values, every other field is filled by the model."""
    t = p["tools"][name]
    props, required = {}, []
    calls = set(_var_names(p))
    field_hints = field_hints or {}
    baked = baked or {}
    for f, spec in t["fields"].items():
        if f in hide and not spec.get("required") and f not in calls:
            continue                    # an optional field the hooks overwrite: not shown to the model
        leaf = {"type": spec.get("type") or "string"}
        if leaf["type"] not in ("string", "number", "integer", "boolean"):
            leaf["type"] = "string"
        if f in baked:
            leaf["default"] = baked[f]
        elif f in calls:
            leaf["default"] = "{{" + f + "}}"
        else:
            leaf["default"] = "{{" + f + "}}"
            desc = _field_desc(spec, field_hints.get(f, ""))
            if desc:
                leaf["description"] = desc
            if spec.get("enum"):
                leaf["enum"] = list(spec["enum"])
        props[f] = leaf
        if spec.get("required") and f not in calls and f not in baked:
            required.append(f)
    cfg = {"timeoutMs": timeout, "maxRetries": retries}
    if config_extra:
        cfg.update(config_extra)
    tool = {
        "type": "function",
        "config": cfg,
        "function": {
            "name": name,
            "handler": "__function__:api_call",
            "description": description,
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "default": _tool_url(p, name)},
                "method": {"type": "string", "default": t.get("method") or "POST"},
                "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                "body": {"type": "object", "properties": props, "required": required},
            }},
        },
    }
    if pre:
        tool["pre_process_code"] = build_code(pre, rt, "pre")
    if post:
        tool["post_process_code"] = build_code(post, rt, "post")
    if mapping:
        tool["response_mapping"] = mapping
    return tool


def _code_tool(name, description, fields, body, rt):
    """A code_exec tool the model calls: `fields` {name: description} are filled by the model and arrive as `params`."""
    props = {f: {"type": "string", "description": d} for f, d in fields.items()}
    props["code"] = {"type": "string", "default": build_code(body, rt, "tool")}
    props["timeout_ms"] = {"type": "number", "default": 10000}
    return {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0}, "function": {
        "name": name, "handler": "__function__:code_exec", "description": description,
        "parameters": {"type": "object", "properties": props, "required": [next(iter(fields))]}}}


def _code_node(name, body, rt, target, on_enter=None, source=None):
    return {
        "name": name, "node_type": "code", "passthrough": True, "passthrough_default_transition": target,
        "on_enter": on_enter or {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [{"type": "function", "function": {
            "name": "code_exec", "handler": "__function__:code_exec",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "string", "default": build_code(body, rt, "node", source)},
                "timeout_ms": {"type": "number", "default": 10000}}}}}],
        "transitions": [{"target_node": target, "description": "always", "condition": {"operator": "always"}}],
    }


def _end_node(name, fn_name, spoken=""):
    props = {"message_mode": {"type": "string", "default": "custom" if spoken else "none"}}
    if spoken:
        props["spoken_message"] = {"type": "string", "default": spoken}
    return {"name": name, "node_type": "end_call", "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
            "transitions": [], "terminal": {"type": "function", "function": {
                "name": fn_name, "handler": "__function__:end_call",
                "parameters": {"type": "object", "properties": props}}}}


def _or(*conds):
    return {"logic": "or", "conditions": list(conds)}


def _eq(var, val):
    return {"variable": var, "operator": "equals", "operand": val}


def _gt(var, val):
    return {"variable": var, "operator": "greater_than", "operand": val}


# ----------------------------------------------------------------------------------------------- prompts

class _Prompts:
    def __init__(self, prompts):
        self.p = prompts if isinstance(prompts, dict) else {}

    def get(self, key, default=""):
        if key in self.p and isinstance(self.p[key], str) and self.p[key].strip():
            return self.p[key]
        if "." in key:
            node, part = key.split(".", 1)
            nd = (self.p.get("nodes") or {}).get(node) or {}
            if isinstance(nd, dict) and isinstance(nd.get(part), str) and nd[part].strip():
                return nd[part]
            if node == "tool":
                td = (self.p.get("tools") or {}).get(part)
                if isinstance(td, str) and td.strip():
                    return td
        return default


def _bul(items, prefix="- "):
    return "\n".join(prefix + i for i in items if i)


def _brace_vars(text, p):
    """'{title}' for a known call variable -> '{{title}}' (for fixed spoken lines)."""
    names = set(_var_names(p))
    return re.sub(r"(?<!\{)\{(\w+)\}(?!\})", lambda m: "{{" + m.group(1) + "}}" if m.group(1) in names else m.group(0), text)


def _quoted_in_rules(p, text, phases=None):
    """The text is quoted word for word in a rule (or in the opening notes): the brief fixes its exact words."""
    t = re.sub(r"\s+", " ", text.strip())
    ph = rule_phases(p) if phases else {}
    srcs = [r["text"] for r in p["rules"] if not phases or ph.get(r["id"]) in phases] + [x for x in (p["opening"].get("notes") or []) if isinstance(x, str)]
    return bool(t) and any(re.search(r"[\"“]" + re.escape(t) + r"[\"”]", re.sub(r"\s+", " ", x)) for x in srcs)


def static_opening(p):
    """The first words of the call when the brief fixes them completely: the opening sentence quoted word for word and
    the quoted question that asks for the customer, built only from call variables. '' when anything is left to the
    model (e.g. an opening given as content to convey), then the model speaks the opening."""
    names = set(_var_names(p))
    line = (p["opening"].get("line") or "").strip()
    if line and set(re.findall(r"\{+(\w+)\}+", line)) <= names:
        return line                         # extracted: quoted word for word in the brief
    opening = (p["opening"].get("text") or "").strip()
    if not opening or not _quoted_in_rules(p, opening):
        return ""
    for v in p["phrases"].values():
        if not isinstance(v, str) or not v.strip().endswith("?"):
            continue
        used = re.findall(r"\{+(\w+)\}+", v)
        if used and set(used) <= names and _quoted_in_rules(p, v, ("greet", "all")):
            return opening + " " + v.strip()
    return ""


_MANY_RX = re.compile(r"\b(all|every|everything|multi|multiple|several|them|both)\b", re.I)
_PAY_RX = re.compile(r"\b(pay|paid|clear|settle|payment)\b", re.I)
_DEBT_RX = re.compile(r"days late|days overdue|days in arrears|days past due|how many days|number of days|one sentence each"
                      r"|(past.due|overdue) amount|amount (past due|overdue)", re.I)
_INTRO_RX = re.compile(r"\bsay (?:that )?(?:you are|you're|we are|we're) calling ((?:from [^.;,:\n\"`]+? )?about [^.;,:\n\"`]+?)"
                       r"(?=\s*(?:[.;,:\n]|\band\b|$))", re.I)
_ASK_ALL_RX = re.compile(r"\b(?:whether|if) (?:the customer|the member|the caller|they|you) can (pay|clear|settle) "
                         r"(all of (?:them|it)|everything|them all|all (?:the |their )?\w+|the whole \w+) today", re.I)
_ASK_ALL2_RX = re.compile(r"\b(?:whether|if) everything can be (paid|cleared|settled) today", re.I)


def static_entry(p):
    """What the first message after verification says, when the policy fixes it from the account data alone: one
    sentence per late product (product name in words, days late, the past-due amount) and the first payment question,
    quoted in the brief (one product / several products). The prepare_account code fills it in per call, so the model
    is not asked to write the longest line of the call. None when anything is left open (then the model speaks it)."""
    texts = [r["text"] for r in p["rules"]] + [x for x in (p["products"].get("notes") or []) if isinstance(x, str)]
    debt = [t for t in texts if _DEBT_RX.search(t)]
    if not debt:
        return None
    one, many = "", ""
    for k, v in p["phrases"].items():
        v = (v or "").strip() if isinstance(v, str) else ""
        if not v.endswith("?") or "today" not in v.lower() or not _PAY_RX.search(v + " " + k.replace("_", " ")) or "{" in v:
            continue
        if _MANY_RX.search(k.replace("_", " ") + " " + v):
            many = many or v
        else:
            one = one or v
    if not many:
        for t in texts:
            m = _ASK_ALL_RX.search(t)
            if m:
                many = "Can you %s %s today?" % (m.group(1).lower(), m.group(2))
                break
            m = _ASK_ALL2_RX.search(t)
            if m:
                many = "Can you %s everything today?" % {"paid": "pay", "cleared": "clear", "settled": "settle"}[m.group(1).lower()]
                break
    if not one or (p["products"]["max_count"] > 1 and not many):
        return None
    intro = ""
    for t in debt + texts:
        m = _INTRO_RX.search(t)
        if m:
            intro = "I am calling " + m.group(1).strip() + "."
            break
    low = " ".join(debt).lower()
    noun = next((n for k, n in (("past-due amount", "past-due amount"), ("past due amount", "past-due amount"), ("overdue amount", "overdue amount"),
                                ("amount in arrears", "amount in arrears"), ("arrears", "amount in arrears"), ("past-due", "past-due amount"),
                                ("past due", "past-due amount"), ("overdue", "overdue amount")) if k in low), "past-due amount")
    late = next((n for k, n in (("days late", "late"), ("days overdue", "overdue"), ("days in arrears", "in arrears"),
                                ("days past due", "past due")) if k in low), "late")
    return {"intro": intro, "noun": noun, "late": late, "one": one, "many": many or one}


def _pre_verification_outcomes(p):
    out = {c["outcome"] for c in p["third_party"]["cases"] if c["outcome"]}
    if p["identity"]["fail"]["outcome"]:
        out.add(p["identity"]["fail"]["outcome"])
    return out


def _post_outcomes(p):
    """Outcomes that need the account (a promise, a product note, a refused offer, a failed recording)."""
    cl = p["closing"]
    out = {(cl.get(k) or {}).get("outcome") for k in ("promise", "promise_mixed", "service_error", "refused")}
    out |= {(v or {}).get("outcome") for v in (cl.get("note_map") or {}).values() if isinstance(v, dict)}
    out -= {(v or {}).get("outcome") for v in (cl.get("transfer") or {}).values() if isinstance(v, dict)}
    return {o for o in out if o} - _pre_verification_outcomes(p)


# ----------------------------------------------------------------------------------------------- v4 enforcement

_TWICE_RX = re.compile(r"\s*\b(twice|two times|a second time|the second time|three times|a third time|the third time)\b", re.I)


def _count_trigger(p):
    """(trigger, count) for the post-verification trigger that acts when the customer says something N times
    ('says twice that they do not want to talk'), else (None, 0)."""
    pre = _pre_verification_outcomes(p)
    for t in p["triggers"]:
        m = _TWICE_RX.search(t["detect"] or "")
        if m and t["outcome"] and t["outcome"] not in pre and t["action"] in ("close", "note"):
            return t, (3 if re.search(r"three|third", m.group(1), re.I) else 2)
    return None, 0


def _dispute_trigger(p):
    """The post-verification trigger of a dispute (its codes name it), else None."""
    pre = _pre_verification_outcomes(p)
    for t in p["triggers"]:
        codes = " ".join(str(t.get(k) or "") for k in ("name", "reason", "note_reason")).lower()
        if "disput" in codes and t["outcome"] not in pre:
            return t
    return None


def _note_for(p, outcome, reason, prefer=""):
    """The note reason whose closing pair is (outcome, reason); `prefer` when it is one of the tool's reasons."""
    reasons = list(p["notes"].get("tool_reasons") or [])
    if prefer and (prefer in reasons or not reasons):
        return prefer
    for k, v in (p["closing"].get("note_map") or {}).items():
        if isinstance(v, dict) and (v.get("outcome") or "") == (outcome or "") and (v.get("reason") or "") == (reason or ""):
            return k
    return reason if reason in reasons else ""


def enforce_rt(p, opts):
    """Runtime section 'enforce': what the code enforces and with which policy values (counts and outcomes come from
    the policy). `signals`: the extraction labels negotiate shows (x_reply values)."""
    cl = p["closing"]
    refused = cl.get("refused") or {}
    talk, n_talk = _count_trigger(p)
    disp = _dispute_trigger(p)
    nocb = cl.get("no_callback") or {}
    se = cl.get("service_error") or {}
    floors = any(t.get("floor_field") or t.get("floor_expr") for t in (p["products"].get("types") or {}).values())
    sig = []
    if opts.get("extract_signals"):
        if any(b.get("attempts") for b in p["bands"]):
            sig += ["refusal"] + (["too_much"] if floors else [])
        if talk is not None:
            sig.append("no_talk")
        if disp is not None:
            sig.append("dispute")
    pr = lambda x: {"outcome": (x or {}).get("outcome") or "", "reason": (x or {}).get("reason") or ""}
    return {
        "signals": sig,
        "close_on_ladder_end": bool(refused.get("outcome")) and "refusal" in sig,
        "refused": pr(refused),
        "refused_note": _note_for(p, refused.get("outcome"), refused.get("reason")),
        "talk_count": n_talk if "no_talk" in sig else 0,
        "talk": pr(talk),
        "talk_note": _note_for(p, (talk or {}).get("outcome"), (talk or {}).get("reason"), (talk or {}).get("note_reason") or ""),
        "dispute": pr(disp),
        "dispute_note": _note_for(p, (disp or {}).get("outcome"), (disp or {}).get("reason"), (disp or {}).get("note_reason") or "") if disp else "",
        "dispute_on": "dispute" in sig,
        "service_note": _note_for(p, se.get("outcome"), se.get("reason")),
        "callback_close": bool(opts.get("enforce_callback")) and bool(nocb.get("outcome")),
        "no_callback": pr(nocb),
        "max_turns": int(opts.get("negotiate_max_turns") or 80),
    }


def signal_schema(p, ef):
    """extraction_schema of negotiate: one small enum about the customer's last message (+ the product it is about
    when the account can have several). [] when nothing is enforced."""
    sig = ef["signals"]
    if not sig:
        return []
    talk, _n = _count_trigger(p)
    disp = _dispute_trigger(p)
    what = {
        "refusal": "says no to, ignores or does not answer the agent's request or offer to pay, and names no day, no period"
                   " and no amount of their own (a plain no, 'I can't', 'not then')",
        "too_much": "says the amount itself is too much or more than they can pay, naming no amount",
        "no_talk": _TWICE_RX.sub("", (talk or {}).get("detect") or "").strip().rstrip("."),
        "dispute": ((disp or {}).get("detect") or "").strip().rstrip("."),
    }
    desc = ("What the customer's LAST message does, judged against the agent's message just before it. "
            + " ".join("%s = %s;" % (s, what[s]) for s in sig)
            + " other = anything else: agrees, names a day, a period or an amount, asks a question, mentions illness, a death"
              " or job loss, says they already paid, asks for a person or a new plan, or anything not listed."
            + " Tie-break: a message that fits " + ("dispute or " if "dispute" in sig else "") + "other in any part is labelled"
              " that, never " + " or ".join(x for x in ("refusal", "too_much", "no_talk") if x in sig) + ": e.g. 'no, I lost my"
              " job', 'no, why are you calling?', 'I can do 200 on Friday', 'no, but next week' are other. When unsure, other.")
    # 0.8: a refusal or a 'do not want to talk' moves the ladder or closes the call; a doubtful label is dropped
    out = [{"name": "x_reply", "type": "string", "enum_values": sig + ["other"], "description": desc, "min_confidence": 0.8}]
    if p["products"]["max_count"] > 1:
        out.append({"name": "x_product", "type": "string", "min_confidence": 0.5,
                    "description": "The late product the customer's last message is about, in the words used in the call"
                                   " (e.g. 'the card', 'car loan'); empty when it names none."})
    return out


def _trigger_phases(p, t, ph):
    """Phases of the rules a trigger's notes come from (empty when none matches)."""
    notes = [_norm_rule(n) for n in (t.get("notes") or []) if isinstance(n, str) and len(n) > 25]
    out = set()
    for r in p["rules"]:
        x = _norm_rule(r["text"])
        if any(x[:60] in n or n[:60] in x for n in notes):
            out.add(ph.get(r["id"]))
    return out


def _trigger_lines(p, cl, verified, ef=None):
    """One short line per trigger that can happen in this phase and needs an action (a tool, a note, an outcome).
    The verbatim rule text is in the system prompt or the step's rules, so a trigger that only says something is
    left to its rule, and a trigger whose rules all belong to the other step is left out; trigger notes are added
    only when the policy has no rules list."""
    out = []
    pre_out = _pre_verification_outcomes(p)
    post_out = _post_outcomes(p)            # outcomes that need the account: not before verification
    case_pairs = {(c["outcome"], c["reason"]) for c in p["third_party"]["cases"]}
    roles = rule_roles(p) if p["rules"] else {}
    # a rule that is not shown (replaced, computed by the hooks) does not describe the situation in the prompt
    ph = {k: ("hidden" if k in roles else v) for k, v in rule_phases(p).items()} if p["rules"] else {}
    other = "greet" if verified else "negotiate"
    here = "negotiate" if verified else "greet"
    for t in p["triggers"]:
        act = t["action"]
        if verified and t["outcome"] in pre_out:
            continue            # third-party / identity situations cannot happen after verification
        if not verified and (act == "note" or (t["outcome"], t["reason"]) in case_pairs or t["outcome"] in post_out):
            continue            # product notes need the account; third-party cases are listed separately
        fail = p["identity"]["fail"]
        if not verified and act == "close" and not t["say"] and fail["outcome"] and (t["outcome"], t["reason"]) == (fail["outcome"], fail["reason"]):
            continue            # the failed identity check has its own line in the step
        if ph:
            tph = _trigger_phases(p, t, ph)
            shown = bool(tph & {here, "all"})
            if not (t["outcome"] or (t["say"] and not shown) or (act == "transfer" and t["queue"]) or (act == "note" and t["note_reason"])):
                continue        # nothing to do beyond its rule, which is in the prompt (with its words to say)
            if tph == {other}:
                continue        # every rule of it applies only in the other step
        else:
            tph, shown = set(), False
        # the situation is described by its rule when that rule is in this prompt; the name is enough to point at it
        twice = re.search(r"\btwice\b|\bsecond time\b|\btwo times\b", t["detect"] or "")
        # a transfer or a close is final: its exact condition stays next to it (e.g. "insists after being asked once")
        full = twice or act in ("transfer", "close") or not shown
        line = t["name"] + ((" (" + (t["detect"] or "when it applies") + ")") if full else "") + ":"
        if t["say"]:
            line += " Say: \"" + t["say"] + "\"."
        if act == "transfer" and t["queue"]:
            line += (" Call transfer_to_agent with queue '" + t["queue"] + "', wait for its result, then close "
                     + t["outcome"] + (" / " + t["reason"] if t["reason"] else "") + ".")
        elif act == "note" and t["note_reason"] and p["notes"]["enabled"]:
            line += " Note reason '" + t["note_reason"] + "'."
        elif t["outcome"]:
            pr_ = t["outcome"] + (" / " + t["reason"] if t["reason"] else "")
            line += (" Close: " + pr_ + ".") if act != "continue" else (" When it ends the call, the outcome is " + pr_ + ".")
            if twice and verified and ef and ef.get("talk_count"):
                line += " Counted by the system, which closes the call the last time; before that, acknowledge it and make the next offer."
            elif twice:
                line += (" The first time: do not mention security; acknowledge it and make the current product's next offer." if verified else
                         " The first time: explain once, as the rules say, why the security questions are asked, and ask again.")
                line += ((" Count it in any words, amid other talk too." if verified else "")
                         + " The second time: close at once (thank, goodbye and close_call in that message).")
        if not p["rules"]:
            for n in t.get("notes") or []:
                if isinstance(n, str) and n.strip():
                    line += " " + n.strip()
        out.append(line)
    return out


def _outcome_lines(p):
    rows = []
    for r in p["outcomes"]["table"]:
        rows.append(r["outcome"] + (" / " + r["reason"] if r["reason"] else "") + (": " + r["when"] if r["when"] else ""))
    return rows


_PRE_SECTIONS = ("opening", "third_party", "identity")
_POST_SECTIONS = ("products", "bands", "amounts", "dates", "promise", "calendar", "notes")
_ANY_SECTIONS = ("triggers", "outcomes", "closing")


def _texts_in(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _texts_in(v)
    elif isinstance(o, list):
        for v in o:
            yield from _texts_in(v)


def _norm_rule(t):
    return re.sub(r"^\s*\d+[.)]\s*", "", re.sub(r"\s+", " ", str(t))).strip().lower()


def rule_phases(p):
    """rule id -> 'greet' | 'negotiate' | 'all' | 'drop', from where the extracted policy files the same text.

    Before verification ('greet'): filed only under opening/third party/identity or a trigger that ends the call
    with a pre-verification outcome; or a rule whose only outcome codes are pre-verification ones (they cannot apply
    after it). After verification ('negotiate'): filed under products/bands/amounts/dates/promise/notes (a trigger
    that refers to such a rule, e.g. a refusal of an offer, happens there too), or only under a product-note
    trigger. A rule filed for both phases, only under triggers/outcomes/closing that can happen in any step, or
    nowhere, stays in the system prompt ('all'). A row of the call variables table repeats
    the system prompt's own variable list ('drop'). Nothing else is left out (a test enforces it)."""
    def filed(objs):
        return [_norm_rule(t) for o in objs for t in _texts_in(o) if len(t) > 25]
    pre_out = _pre_verification_outcomes(p)
    early = [t for t in p["triggers"] if t["outcome"] in pre_out]      # third-party / identity endings: before verification only
    noted = [t for t in p["triggers"] if t not in early and t["action"] == "note"]   # product notes need the account
    pre = filed([p.get(s) for s in _PRE_SECTIONS] + early)
    post = filed([p.get(s) for s in _POST_SECTIONS])
    post_trig = filed(noted)
    rest = [t for t in p["triggers"] if t not in early and t not in noted]
    # a trigger that only continues the conversation (e.g. a refusal of an offer) does not pull an offer rule
    # back into every step; one that closes, transfers or answers something does
    cont = filed([t for t in rest if t["action"] == "continue"])
    any_trig = filed([t for t in rest if t["action"] != "continue"])
    any_ = any_trig + filed([p.get(s) for s in _ANY_SECTIONS if s != "triggers"])
    # outcomes that need the account (a promise, a product note, a refused offer, a failed recording)
    cl = p["closing"]
    post_out = {(cl.get(k) or {}).get("outcome") for k in ("promise", "promise_mixed", "service_error", "refused")}
    post_out |= {(v or {}).get("outcome") for v in (cl.get("note_map") or {}).values() if isinstance(v, dict)}
    post_out -= {(v or {}).get("outcome") for v in (cl.get("transfer") or {}).values() if isinstance(v, dict)}
    post_out = {o for o in post_out if o} - pre_out
    all_out = {r["outcome"] for r in p["outcomes"]["table"] if r["outcome"]} | pre_out
    calls = set(_var_names(p))
    pr = p["products"]
    acct_fields = {pr.get(k) for k in ("id_field", "type_field", "days_late_field", "list_field") if pr.get(k)}
    for t in (pr.get("types") or {}).values():
        acct_fields |= {t.get(k) for k in ("floor_field", "full_field") if t.get(k)}
    free = {}

    def hit(x, texts):
        return any(x[:60] in t or t[:60] in x for t in texts)
    out = {}
    for r in p["rules"]:
        named = re.findall(r"`(\w+)`", re.split(r"[|;]", r["text"])[0])
        if re.match(r"\s*Variable:", r["text"]) and named and set(named) <= calls:
            out[r["id"]] = "drop"
            continue
        x = _norm_rule(r["text"])
        # outcome codes the rule names (quoted, or a code with an underscore: a plain word may be ordinary prose)
        said = {o for o in all_out if ("`" + o.lower() + "`") in r["text"].lower()
                or ("_" in o and re.search(r"(?<![\w-])" + re.escape(o.lower()) + r"(?![\w-])", x))}
        g, n_sec, n_trig, b = hit(x, pre), hit(x, post), hit(x, post_trig), hit(x, any_)
        only_pre = bool(said) and said <= pre_out
        acct = bool(set(re.findall(r"`(\w+)`", r["text"])) & acct_fields)
        if not (n_sec or n_trig) and (only_pre or (g and not b and not hit(x, cont))) or (g and only_pre and not n_trig and not b):
            out[r["id"]] = "greet"
        elif not g and not only_pre and (not b and (n_sec or n_trig or (acct and not hit(x, cont)))
                                         or (said and said <= post_out)):
            # an account field in a rule filed nowhere, or only outcomes that need the account: after verification
            out[r["id"]] = "negotiate"
        else:
            out[r["id"]] = "all"
        free[r["id"]] = not (g or n_sec or n_trig or b)
    # "[overrides <ids>]": a rule filed nowhere that replaces rules of one step belongs to that step
    for r in p["rules"]:
        m = re.search(r"\[(?:overrides|changes) ([^\]]+)\]", r["text"])
        if m and out.get(r["id"]) == "all" and free.get(r["id"]):
            targets = {out.get(t.strip()) for t in m.group(1).split(",")}
            if len(targets) == 1 and targets <= {"greet", "negotiate"}:
                out[r["id"]] = targets.pop()
    return out


# "[overrides <ids>]": the later rule replaces the earlier one as a whole; "[changes <ids>]": it changes a part only
_OVERRIDE_RX = re.compile(r"\[\s*(overrides|replaces|supersedes|changes|amends)\s+([^\]]+)\]\s*", re.I)
_PARTIAL_KINDS = ("changes", "amends")
_DATE_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\b(?:january|february|march|april|may|june|july|"
                      r"august|september|october|november|december)\s+\d{4}\b", re.I)


def _overridden(p):
    """Rule ids a later rule replaces ("[overrides <ids>] ..." marks from the rules extraction). Read from the last
    rule back, so a mark counts only when its own rule is still in force: a memo item that a later memo withdraws (or
    one struck through) no longer replaces anything, and the rule it replaced applies again."""
    return {t for t, marks in _override_marks(p).items() if any(k not in _PARTIAL_KINDS for k, _r in marks)}


def _override_marks(p):
    """target rule id -> [(kind, later rule)] for the marks of rules still in force (see _overridden)."""
    ids = {r["id"] for r in p["rules"]}
    out, gone = {}, set()
    for r in reversed(p["rules"]):
        if r["id"] in gone or _is_void(r["text"]):
            continue
        for m in _OVERRIDE_RX.finditer(r["text"]):
            kind = m.group(1).lower()
            for t in re.split(r"[,;]|\band\b", m.group(2)):
                t = t.strip().strip("`")
                if t in ids and t != r["id"]:
                    out.setdefault(t, []).append((kind, r))
                    if kind not in _PARTIAL_KINDS:
                        gone.add(t)
    return out


_NEG_RX = re.compile(r"\b(never|not|no|don't|cannot)\b", re.I)


def _contradicts_on_tool(p, old, new):
    """Both rules talk about the same tool and one says not to use it where the other says to (e.g. 'Brief A never
    uses add_note' replaced by 'call add_note for each product without a promise')."""
    tools = set(p["tools"]) | {v[0] for v in ROLES.values()}

    def stance(text, tool):
        sents = [x for x in re.split(r"(?<=[.!?])\s+", _rule_text(text)) if re.search(r"\b" + re.escape(tool) + r"\b", x)]
        return {bool(_NEG_RX.search(x)) for x in sents}
    for tool in tools:
        a, b = stance(old, tool), stance(new, tool)
        if a and b and a != b:
            return True
    return False


def _rule_text(t):
    """A rule as the model reads it: without the extraction's "[overrides ...]" mark (the replaced rule is not shown)."""
    return _OVERRIDE_RX.sub("", t).strip()


def _hidden_by_override(p, r, marks, new_marks):
    """A replaced rule is hidden. Policies with '[changes]' marks: every '[overrides]' is a full replacement and every
    '[changes]' keeps the old rule. Older policies (one kind of mark): the old rule stays when it still says what to say
    or not to do, unless the later rule contradicts it on the same tool."""
    full = [x for k, x in marks if k not in _PARTIAL_KINDS]
    if not full:
        return False
    if new_marks:
        return True
    return not _keeps_content(r["text"]) or any(_contradicts_on_tool(p, r["text"], x["text"]) for x in full)


def _is_void(t):
    """Struck through as a whole (~~...~~): cancelled text, nothing left to follow."""
    if "~~" not in t:
        return False
    cells = _cells(t)
    if cells and any("~~" in v for _k, v in cells):
        # a table row whose struck values leave nothing (a footnote mark or a row number at most)
        return all(not any(c.isalpha() for c in re.sub(r"~~.*?~~", "", v, flags=re.S)) or "~~" not in v and len(v) < 4
                   for _k, v in cells)
    rest = re.sub(r"~~.*?~~", "", t, flags=re.S)
    return "~~" in t and len(re.sub(r"[\W\d_]+", "", re.sub(r"\b\w+\s*:", "", rest))) < 12


def _cells(t):
    """A table row written as 'Header: value | Header: value' -> [(header, value)] (else [])."""
    body = _rule_text(t)
    segs = [s.strip() for s in body.split("|")]
    if len(segs) < 2:
        segs = [s.strip() for s in body.split(";")]     # 'Header: value; Header: value'
    if len(segs) < 2:
        return []
    out = []
    for s in segs:
        m = re.match(r"([^:]{1,40}):\s*(.*)$", s, re.S)
        if not m:
            return []
        out.append((m.group(1).strip().strip("`*").lower(), m.group(2).strip()))
    return out


def _codes(p):
    """Outcome, close reason and note reason codes of the policy."""
    out = set()
    for r in p["outcomes"]["table"]:
        out |= {r["outcome"], r["reason"]}
    out |= set(p["notes"].get("tool_reasons") or [])
    return {c for c in out if isinstance(c, str) and c.strip()}


def rule_roles(p):
    """rule id -> why the rule is not shown in a step prompt, for rules something else already carries:

    'over'     a later rule replaces it (its replacement is shown);
    'void'     struck through as a whole;
    'hook'     date/deadline/band arithmetic the code computes: calendar and deadline tables, business-day and
               promise-date rules, the attempt ladder (filed under calendar/dates/bands); the account facts and
               check_offer give the result for each product;
    'outcome'  a row of the outcome table (a 'when' column next to outcome codes): the close is derived from the
               recorded facts; these rows are shown only in the wrap-up step, with the note and outcome rules.
    Rules without a role are shown as before (rule_phases decides where)."""
    def filed(objs):
        return [_norm_rule(t) for o in objs for t in _texts_in(o) if len(t) > 25]
    marks = _override_marks(p)
    new_marks = any(m.group(1).lower() in _PARTIAL_KINDS for r in p["rules"] for m in _OVERRIDE_RX.finditer(r["text"]))
    cal = filed([p.get("calendar"), p.get("dates")])
    bands = filed([p.get("bands")])
    amounts = filed([p.get("amounts")])
    third = filed([p.get("third_party"), p.get("callback"), p.get("identity"), p.get("opening")])
    band_names = {str(b.get("name") or "").strip().lower() for b in p["bands"]} - {""}
    codes = {c.lower() for c in _codes(p)}

    def hit(x, texts):
        return any(x[:60] in t or t[:60] in x for t in texts)
    floor_fields = {t.get(k) for t in (p["products"].get("types") or {}).values() for k in ("floor_field", "full_field")} - {None, ""}
    ph = rule_phases(p)
    # the sentences of a call-variables section around its (dropped) variable rows say nothing the list does not
    var_secs = {r["id"].rsplit(".", 1)[0] for r in p["rules"] if ph.get(r["id"]) == "drop" and "." in r["id"]}
    out = {}
    for r in p["rules"]:
        t = r["text"]
        if _hidden_by_override(p, r, marks.get(r["id"]) or [], new_marks):
            out[r["id"]] = "over"
            continue
        if ph.get(r["id"]) != "drop" and "." in r["id"] and r["id"].rsplit(".", 1)[0] in var_secs and len(t) < 90:
            out[r["id"]] = "over"
            continue
        if _is_void(t):
            out[r["id"]] = "void"
            continue
        x = _norm_rule(_rule_text(t))
        cells = _cells(t)
        vals = {re.sub(r"[`*~]", "", v).strip().lower() for _k, v in cells}
        if cells and any(re.search(r"\bwhen\b", k) for k, _v in cells) and \
                any(re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", t.lower()) for c in codes):
            out[r["id"]] = "outcome"
        elif cells and (vals & band_names or _DATE_RX.search(t)) and not _SPEAKS_RX.search(t):
            out[r["id"]] = "hook"               # a deadline-table row or a holiday row
        elif not cells and not hit(x, third) and (hit(x, cal) or (hit(x, bands) and not hit(x, amounts))) \
                and not _SPEAKS_RX.search(_rule_text(t)):
            out[r["id"]] = "hook"               # no hook records, reads back or says anything for the agent
        elif floor_fields and len(t) < 240 and any(f in t for f in floor_fields) and not _keeps_content(t) \
                and not re.search(r"\b(ask|offer|accept|refus\w*|propos\w*)\b", t, re.I):
            out[r["id"]] = "hook"               # a definition of the floor / full amount: each product's facts give it
    # a rule a shown rule refers to by number ("treat it as a flat refusal (rule 30)") stays, so no reference dangles
    labels = _rule_labels(p)
    for _ in range(2):
        shown_text = " ".join(_rule_text(r["text"]) for r in p["rules"] if r["id"] not in out and ph.get(r["id"]) != "drop")
        named = set()
        for m in re.finditer(r"\brules?\s+((?:[\w.]+(?:\s*,\s*|\s+and\s+|\s+or\s+)?)+)", shown_text, re.I):
            named |= {x.strip(".") for x in re.split(r"\s*,\s*|\s+and\s+|\s+or\s+", m.group(1)) if x.strip(".")}
        back = [i for i, role in out.items() if role in ("hook", "over") and ph.get(i) != "drop"
                and _rule_number(labels[i]) in named]
        if not back:
            break
        for i in back:
            del out[i]
    return out


def _rule_number(label):
    """'30' for a rule labelled 30 or a.30 (how other rules cite it: 'rule 30'), else None."""
    m = re.fullmatch(r"(?:[a-z]\.)?(\d+[a-z]?)", label)
    return m.group(1) if m else None


_SPEAKS_RX = re.compile(r"\b(record\w*|read\w*|says?|tell\w*)\b", re.I)


def _keeps_content(t):
    """A replaced rule that still tells the agent what to say or not to do: a later rule often changes only part of it
    (e.g. the outcome or a count), so it stays, and the later rule is shown as changing it."""
    return bool(re.search(r"\bdo not\b|\bdon't\b|\bnever\b|\basks?\b|\boffers?\b|\btell\w*\b|\bsays?\b", _rule_text(t), re.I)) \
        and not _cells(t)


def _changes(p, t, labels, roles):
    """'(changes <labels>) ' for the rules a later rule changes that are still shown."""
    keep = []
    for m in _OVERRIDE_RX.finditer(t):
        for x in re.split(r"[,;]|\band\b", m.group(2)):
            x = x.strip().strip("`")
            if x in labels and x not in roles:
                keep.append(labels[x])
    return ("(changes " + ", ".join(keep) + ") ") if keep else ""


def _rule_labels(p):
    """Short rule labels: the id without its document name when that stays unique, else a short document tag."""
    ids = [r["id"] for r in p["rules"]]
    rest = {i: i.split(".", 1)[1] if "." in i else i for i in ids}
    tag = {i: re.split(r"[-_ ]", i.split(".", 1)[0])[-1] + "." + rest[i] if "." in i else i for i in ids}
    count = lambda m, v: sum(1 for x in m.values() if x == v)
    return {i: rest[i] if count(rest, rest[i]) == 1 else (tag[i] if count(tag, tag[i]) == 1 else i) for i in ids}


def _rule_line(p, r, labels=None, roles=None):
    labels = labels or _rule_labels(p)
    roles = rule_roles(p) if roles is None else roles
    return "[" + labels[r["id"]] + "] " + _changes(p, r["text"], labels, roles) + _rule_text(r["text"])


def shown_rules(p, phase):
    """The rules a step prompt shows for a phase ('all' = the system prompt), in document order."""
    ph = rule_phases(p)
    roles = rule_roles(p)
    return [r for r in p["rules"] if ph.get(r["id"]) == phase and r["id"] not in roles]


def _phase_rules(p, phase):
    labels, roles = _rule_labels(p), rule_roles(p)
    return _bul([_rule_line(p, r, labels, roles) for r in shown_rules(p, phase)])


def wrap_rules(p):
    """Rules for the wrap-up step (notes and the outcome): outcome-table rows and rules that name the note tool or an
    outcome/note code, after verification, that the system prompt does not already show."""
    ph = rule_phases(p)
    roles = rule_roles(p)
    note_tool = _tool_name(p, "note") if p["tools"] else ""
    codes = _codes(p)
    pre_only = _pre_verification_outcomes(p)

    def names_code(t):
        low = t.lower()
        found = {c for c in codes if ("`" + c.lower() + "`") in low
                 or (("_" in c or "-" in c) and re.search(r"(?<![\w-])" + re.escape(c.lower()) + r"(?![\w-])", low))}
        return found and not found <= pre_only
    out = []
    for r in p["rules"]:
        role, phase = roles.get(r["id"]), ph.get(r["id"])
        if role in ("over", "void") or phase in ("greet", "drop"):
            continue
        if phase == "all" and role is None:
            continue                        # already in the system prompt
        if role == "outcome" or (note_tool and note_tool in r["text"]) or (phase == "negotiate" and names_code(r["text"])):
            out.append(r)
    return out


# data minimisation and disclosure only to the verified person (KVKK / GDPR principles); asking for the customer by
# the name the bank gives is not a disclosure
# long replies are slow (live: under 60 characters 0.7 s, over 300 characters 5 s) and the customer hears every word
SHORT_LINE = "one or two short sentences (max 120 chars), one question at a time; no repeats but the read-back."
PRIVACY_LINE = ("Privacy: never read back or repeat an identity answer (date of birth, digits); before verification say nothing about"
                " the account; give no personal or account details to anyone but the verified customer.")


def default_system_prompt(p):
    vars_ = _bul(["%s = {{%s}}%s" % (v["name"], v["name"], (" (" + v["meaning"] + ")") if v["meaning"] else "") for v in p["variables"]])
    rules = _phase_rules(p, "all")
    # a phrase quoted word for word in a rule is already in the prompt of every step where that rule applies
    kept = " ".join(r["text"] for ph_ in ("all", "greet", "negotiate") for r in shown_rules(p, ph_))
    phrases = _bul(["%s: \"%s\"" % (k, v) for k, v in p["phrases"].items() if isinstance(v, str) and v.strip() not in kept])
    return "\n".join(x for x in [
        "You are a bank's phone agent on a live call. Speak %s: " % p["language"] + SHORT_LINE,
        "Use only facts from this prompt, the call variables and tool results; never invent amounts, dates, reasons or offers.",
        PRIVACY_LINE,
        "",
        "Call variables:",
        vars_,
        "",
        "Tools:",
        "- When a rule says to call a tool, call it. One tool call per message, then wait for its result (parallel calls: results can be lost). Follow a result's what_to_do_next and speak in the same turn; never end a turn with nothing to say.",
        "- A failed or stopped call recorded nothing ('Status of your last tool request'): say something was recorded, scheduled or transferred only after a result says so; a read-back asks for confirmation, never 'I have recorded' before the result.",
        "- close_call: last and once, after all other results, with your thanks and goodbye.",
        "",
        "The bank's rules for every step:",
        rules,
        "",
        ("Fixed phrases (word for word where they apply):\n" + phrases) if phrases else "",
    ] if x is not None)


STATUS_LINE = "Status of your last tool request: {{w_last_event}}"
# one-line state hint written by the hooks (may be empty; the setup node gives it a value so it never shows literally)
NEXT_ACTION = "{{w_next_action}}"
# the rules on dates, deadlines, bands, floors, the offer ladder and the outcome table are computed by the hooks
# (prepare_account, check_offer, close_check, note_pre) and are not repeated in the step prompt (rule_roles 'hook')
HOOK_POINTER = ("The facts above and tool results give business days, deadlines, floors, offers and the outcome: use them as given"
                " (weekdays and years only from the calendar facts). A promise: an amount from the floor to the full amount, a business day"
                " from today to the product's deadline. A date or an amount the customer names is never a refusal.")


def _codes_in(outcome, reason, text):
    """Both codes are named in the (lower-case) text."""
    def has(c):
        return not c or re.search(r"(?<![\w-])" + re.escape(c.lower()) + r"(?![\w-])", text)
    return bool(outcome) and bool(has(outcome)) and bool(has(reason))


NEGOTIATE_ENTER = ("Identity is verified. State the debt as the rules require, using the account facts (every late product, one"
                   " sentence each), then ask the first payment question the rules require. Do not call a tool in this turn.")


def default_greet_task(p, cl, ef=None):
    q = p["identity"]["questions"]
    tp = p["third_party"]
    verbatim = not p["rules"]          # rules missing: carry the section texts here instead
    nocb = p["closing"].get("no_callback") or {}
    cases = []
    shown = " ".join(r["text"] for ph_ in ("all", "greet") for r in shown_rules(p, ph_)).lower()
    asked = False
    for c in tp["cases"]:
        line = "If " + c["trigger"] + ":"
        again = not asked and (c["action"] == "ask_again" or (c["outcome"] and (c["outcome"], c["reason"]) == (nocb.get("outcome"), nocb.get("reason"))))
        if again and ef and ef.get("callback_close"):
            continue            # schedule_callback counts the tries; its result asks again, the engine closes at the last
        if c["action"] != "callback" and not again and (
                not (c["outcome"] or c["say"]) or (p["rules"] and _codes_in(c["outcome"], c["reason"], shown))):
            continue            # a rule shown in this step already says it (or there is nothing to do)
        if c["say"]:
            line += " say \"" + c["say"] + "\";"
        if c["action"] == "callback":
            line += (" ask when the customer can be reached, then call schedule_callback (date YYYY-MM-DD, time HH:MM 24 h) with each day and"
                     " time offered, before you reply, even one outside the window (never judge it yourself): the result says what to do next;")
        elif again:
            asked = True
            n = max(1, int(p["callback"].get("max_asks") or 2) - 1)
            line += " ask for another time only " + ("once" if n == 1 else "%d times" % n) + ";"
        if c["outcome"]:
            line += " close " + c["outcome"] + (" / " + c["reason"] if c["reason"] else "") + "."
        cases.append(line)
    # with a rules list the answer formats are in the rules (and the hooks normalise the answers): the field is enough
    qlines = ["%d. %s verify_identity field '%s'." % (i + 1, x["ask"] or ("Ask for " + x["tool_field"].replace("_", " ") + "."), x["tool_field"])
              for i, x in enumerate(q)] if p["rules"] else ["%d. %s Then call verify_identity with field '%s' and the answer (%s)." % (
        i + 1, x["ask"] or ("Ask for " + x["tool_field"].replace("_", " ") + "."), x["tool_field"],
        {"dmy_to_iso": "the date as YYYY-MM-DD; a date said only in numbers is read day, month, year",
         "mdy_to_iso": "the date as YYYY-MM-DD; a date said only in numbers is read month, day, year",
         "digits_only": "digits only"}.get(x["normalise"], "the date as YYYY-MM-DD" if x["answer_format"] == "date" else "as said"))
        for i, x in enumerate(q)]
    fail = p["identity"]["fail"]
    forb = p["opening"]["forbidden_before_verification"]
    wins = "Allowed callback windows: {{w_callback_windows}}" if p["callback"]["days"] else ""
    opening = p["opening"]["text"]
    said_in_rules = opening and any(_norm_rule(opening)[:60] in _norm_rule(r["text"]) for r in p["rules"])
    return "\n".join(x for x in [
        "Current step: make sure you speak with the right person, then check identity, by the bank's rules. The account is not loaded: say nothing about it.",
        ("Never say these words before identity is verified: " + ", ".join(forb) + ".") if forb else "",
        ("Opening: " + opening) if opening and not said_in_rules else "",
        _bul([x for x in (p["opening"].get("notes") or []) if isinstance(x, str)]) if verbatim else "",
        _bul(tp["rules"]) if verbatim else "",
        _bul(cases),
        ("With anyone else, say nothing about an account, a product, money or a payment, not even that you cannot discuss them. "
         "If they ask why you call or offer to pay, say only that you need to speak with the customer personally, then ask when they can be reached.") if tp["cases"] else "",
        wins,
        "Security questions: only to the customer, after they confirmed who they are, one at a time. An answer in pieces (day and month, then the year; two digits, then two more) is not a refusal or a wrong answer: call verify_identity with each piece (they are joined) and ask only for the missing part. Count wrong answers only from verify_identity results.",
        _bul(qlines, ""),
        "Ask each question without giving a reason. Explain why only if the customer will not answer or asks why, and only once in the call.",
        _bul(p["identity"]["wrong_counts_rules"]) if verbatim else "",
        ("If the check fails: " + fail["say"] + " Close " + fail["outcome"] + (" / " + fail["reason"] if fail["reason"] else "") + ".")
        if fail["outcome"] and not (p["rules"] and _codes_in(fail["outcome"], fail["reason"], shown)) else "",
        "Other situations:",
        _bul(_trigger_lines(p, cl, False, ef)),
        ("Rules for this step:\n" + _phase_rules(p, "greet")) if _phase_rules(p, "greet") else "",
        NEXT_ACTION,
        STATUS_LINE,
    ] if x)


def _ladder_line(p, ef=None):
    """The offer sequence, counted from the policy's bands: how many persuasion attempts each band allows. With the
    signals extracted the code counts refusals and names the next offer (state line), so the line is short."""
    per = ", ".join("%s %d" % (b["name"], len(b.get("attempts") or [])) for b in p["bands"] if b.get("name"))
    if ef and "refusal" in ef.get("signals", []):
        return ("Persuasion attempts" + (" (" + per + ")" if per else "") + ": the system counts refusals and gives the next offer in"
                " the state line; make exactly that offer, once. One sympathy sentence, once.")
    return ("Persuasion attempts" + (" (" + per + ")" if per else "") + ": each product's, in order, once; a refusal or non-answer"
            " ('not on <day>' with no new day too; not 'too much') uses the next; after the last, close. One sympathy sentence, once.")


ENTRY_SAID = "Your first message in this step was already spoken (do not repeat it): \"{{w_entry_line}}\""


def default_negotiate_task(p, cl, check=False, ef=None, entry=False):
    multi = p["products"]["max_count"] > 1
    notes = p["notes"]
    pm = p["promise"]
    verbatim = not p["rules"]
    pre_out = _pre_verification_outcomes(p)
    lines = [
        "Current step: the customer is verified. Handle the late products below by the bank's rules, from stating the debt to closing the call."
        " The security questions are over: never mention them or why they were asked.",
        ENTRY_SAID if entry else "",
        "{{w_account_facts}}",
        "{{w_date_facts}}",
        HOOK_POINTER,
        _ladder_line(p, ef),
        _bul(p["amounts"]["rules"] + [p["dates"]["customer_date_after_deadline"], p["dates"]["vague_date"]] + p["dates"]["rules"]) if verbatim else "",
        "Read-back: " + pm["readback"] if verbatim else "",
    ]
    if check:
        lines.append("A date, a period ('end of the month') or an amount the customer names: call check_offer before you answer."
                     " Not for the full amount today, an offer from the facts or a value already accepted: go to the read-back.")
    lines.append("After a clear yes to the read-back: create_promise once per product, one per message, after each result. "
                 "If it fails, call it again with the same values, at most %d more time(s); then: %s."
                 % (pm["retry_on_failure"], pm["failure_say"] or "say it could not be recorded"))
    if notes["enabled"]:
        lines.append("Notes: {{w_notes_rule}} close_call only after the last note result."
                     + (" Unless a rule says to send notes before a transfer, a transfer means no notes; never a note after a transfer."
                        if notes["forbidden_on_transfer"] else " Before a transfer, follow the rules on whether notes are sent first."))
    else:
        lines.append("Do not call add_note in this call.")
    rows = [r for r, t in zip(_outcome_lines(p), p["outcomes"]["table"]) if t["outcome"] not in pre_out]
    lines += ["Things the customer may say:", _bul(_trigger_lines(p, cl, True, ef)),
              # with a rules list the outcome table is in the rules (system prompt or this step) and in close_call
              ("Closing (outcome / reason): " + "; ".join(rows) + ".") if verbatim else "",
              _bul(p["outcomes"]["precedence"]) if verbatim else "",
              p["outcomes"]["multi_product"] if multi and verbatim else "",
              ("Rules for this step:\n" + _phase_rules(p, "negotiate")) if _phase_rules(p, "negotiate") else "",
              NEXT_ACTION, STATUS_LINE]
    return "\n".join(x for x in lines if x)


def _close_reason_desc(p):
    by = {}
    for r in p["outcomes"]["table"]:
        by.setdefault(r["outcome"], [])
        if r["reason"]:
            by[r["outcome"]].append(r["reason"])
    parts = ["%s: %s" % (o, ", ".join(rs) if rs else "no reason") for o, rs in by.items()]
    return "Exactly as in the outcome table: " + "; ".join(parts) + "."


def _setup_source():
    """codegen/setup.py, plus a value for every state variable a prompt shows ({{w_next_action}}), in case the setup
    code does not give one: an unset variable would show to the model as a literal placeholder."""
    src = (CODEGEN / "setup.py").read_text()
    if '"w_next_action"' not in src:
        src += '\nif vget(vs, "w_next_action", None) is None:\n    vset(vs, "w_next_action", "")\n'
    return src


# ----------------------------------------------------------------------------------------------- main

def generate_workflow(policy, prompts=None, openapi=None, options=None):
    """policy dict (contract v1) -> Freya workflow dict. `openapi` (dict) fills tool URLs/fields/responses that
    the policy lacks. Raises ValueError when the policy cannot produce a workflow."""
    opts = dict(DEFAULT_OPTIONS)
    opts.update(options or {})
    if openapi:
        policy = _merge_openapi(policy, openapi)
    p = policy_schema.normalise(policy)
    for role in ("verify", "account", "promise", "close"):
        if not _tool_name(p, role):
            raise ValueError("policy has no %s tool" % ROLES[role][0])
    names = _names(p, opts)
    rt = runtime_policy(p, names)
    ef = enforce_rt(p, opts)
    rt["enforce"] = ef
    # call variables (first_name, title, ...) that the setup code defaults to "" when a call does not pass them; the
    # clock variable is left alone (the setup reads it and falls back to the sandbox clock), and so is any name a tool
    # field has (a variable with a model-filled field's name would bake that field)
    fields = {f for t in p["tools"].values() for f in ((t or {}).get("fields") or {})}
    rt["call_vars"] = [v for v in _var_names(p) if v != names["now_var"] and v not in fields]
    cl = p["closing"]
    P = _Prompts(prompts)
    neg_enter = P.get("negotiate.on_enter", NEGOTIATE_ENTER)
    if ef["signals"]:
        rt["enter"] = neg_enter
    # fixed first line of negotiate (not with the per-turn signals, whose re-entry speaks {{w_enter_prompt}}, nor when
    # the prompts give their own entry instruction)
    entry = static_entry(p) if opts["static_entry"] and not ef["signals"] and not P.get("negotiate.on_enter") else None
    if entry:
        rt["entry"] = entry
    tn = {r: _tool_name(p, r) for r in ROLES}
    to = opts["timeout_ms"]

    def tdesc(role, default):
        name = tn[role]
        d = P.get("tool." + name, default)
        # the tool's own notes repeat the brief's rules; they are added only when there is no rules list
        note = (p["tools"].get(name) or {}).get("notes") or "" if not p["rules"] else ""
        return d + ((" " + note) if note and note not in d else "")

    # --- tools
    t_verify = _api_tool(p, tn["verify"], tdesc("verify", "CALL it each time the customer answers a security question (each piece too), with that question's field and the answer as given. Only for the customer."),
                         {names["verify_answer"]: "The answer to this one question."},
                         pre="verify_pre", post="verify_post", rt=rt, timeout=to)
    # the model's close_call is a close REQUEST: the pre-hook records it and stops it; the engine closes the call
    # from close_api after close_check/finish, so close_call is always the last call and never runs in parallel
    t_close = _api_tool(p, tn["close"], tdesc("close", "End the call: once, last, in the message with your goodbye, after all other results. The system records the outcome."),
                        {names["close_reason"]: _close_reason_desc(p), names["close_outcome"]: "From the bank's outcome table."},
                        pre="close_request", rt=rt, retries=0, timeout=to)
    t_transfer = _api_tool(p, tn["transfer"], tdesc("transfer", "Hand the call to a person. CALL it only when a rule says to transfer, with that rule's queue."),
                           pre="transfer_pre", post="transfer_post", rt=rt, timeout=to) if tn["transfer"] else None
    t_callback = _api_tool(p, tn["callback"], tdesc("callback", "Book a callback. CALL it with each day and time offered to reach the customer."),
                           {names["callback_date"]: "YYYY-MM-DD", names["callback_time"]: "HH:MM, 24 h"},
                           pre="callback_pre", post="callback_post", rt=rt, timeout=to) if tn["callback"] else None
    t_promise = _api_tool(p, tn["promise"], tdesc("promise", "Record ONE product's payment promise. CALL it only after a clear yes to the read-back."),
                          {names["promise_product"]: "As in the account facts.",
                           names["promise_amount"]: "A number, e.g. 420.5.",
                           names["promise_date"]: "YYYY-MM-DD."},
                          pre="promise_pre", post="promise_post", rt=rt, timeout=to)
    t_note = _api_tool(p, tn["note"], tdesc("note", "Record why ONE product ended without a promise. CALL it at the end, once per product without a promise, when every product is handled and there was no transfer."),
                       {names["note_product"]: "As in the account facts."},
                       pre="note_pre", post="note_post", rt=rt, timeout=to,
                       hide=[f for f in ((p["tools"].get(tn["note"]) or {}).get("fields") or {})
                             if f not in (names["note_product"], names["note_reason"])]) if tn["note"] and p["notes"]["enabled"] else None

    t_check = _code_tool("check_offer", "Check a day and/or an amount the customer named for one product. CALL it before you answer them.",
                         {"product_id": "As in the account facts.",
                          "date": "As the customer said it (words are fine) or YYYY-MM-DD; empty if none.",
                          "amount": "The amount as the customer said it (words are fine), or a number. Leave empty if they named no amount."},
                         "check_offer", rt) if opts["check_tool"] else None

    # leave on a successful close, or after two close_call requests that really went out (counted in the
    # pre-hook: a held-back close is not a send, and the engine does not retry, maxRetries 0)
    closed = {"target_node": "close_check", "description": "the model asked to close the call",
              "condition": _or(_eq("w_close_requested", "yes"))}
    # both sides said goodbye and no close_call came: close_check / finish close from the recorded facts (a missing
    # note still gets its one wrap-up round). Listed after the deterministic close request.
    goodbye = [{"target_node": "goodbye_close", "description": "both sides said goodbye but close_call was not called",
                "condition": {"operator": "matches_intent", "description": GOODBYE_INTENT}}] if opts["goodbye_close"] else []

    # --- nodes
    nodes = {}
    nodes["setup"] = _code_node("setup", "setup", rt, "greet_verify", source=_setup_source())

    opening_line = P.get("opening_line", static_opening(p))
    greet_on_enter = {"type": "tts", "text": _brace_vars(opening_line, p)} if opening_line else {
        "type": "llm", "prompt": P.get("greet_verify.on_enter", "\n".join(
            ["This is the first sentence of the call. Say the opening exactly as the bank requires and then ask whether you are speaking with the customer, as the rules below say. Nothing else. Do not call a tool in this turn.",
             _bul([p["opening"]["text"]]
                  + [x for x in (p["opening"].get("notes") or []) if isinstance(x, str)][:4])]))}
    greet_tools = [t_verify] + [t for t in (t_callback, t_transfer) if t] + [t_close]
    task = P.get("greet_verify.task", default_greet_task(p, cl, ef))
    extra = P.get("greet_verify.extra")
    verify_tool = tn["verify"]
    nodes["greet_verify"] = {
        "name": "greet_verify", "node_type": "conversation", "on_enter": greet_on_enter,
        "on_enter_first_entry_only": True,
        "task_message": task + (("\n\n" + extra) if extra else ""),
        "tools": greet_tools,
        "max_node_turns": opts["greet_max_turns"], "max_turn_transition": "turn_cap",
        "transitions": [
            closed,
            {"target_node": "load_account", "description": "The identity check passed",
             "condition": _or(_eq(verify_tool + "_response.data." + names["verify_all"], "true"),
                              _eq(verify_tool + "_response.data." + names["verify_all"], "True"),
                              _eq("w_id_state", "verified"))},
        ] + ([{"target_node": "close_check", "description": "the last allowed callback time was outside the window",
               "condition": _or(_eq("w_cb_over", "yes"))}] if ef["callback_close"] and t_callback else []) + goodbye,
    }

    account_tool = _api_tool(p, tn["account"], "Get the late products.", rt=rt, retries=2, timeout=to)
    nodes["load_account"] = {
        "name": "load_account", "node_type": "api_request", "passthrough": True,
        "passthrough_default_transition": "prepare_account", "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True, "suppress_tool_results": True,
        "pre_actions": [account_tool],
        "response_mapping": [{"path": "data." + names["list_field"], "variable": "w_account_products"}],
        "transitions": [{"target_node": "prepare_account", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["prepare_account"] = _code_node("prepare_account", "prepare_account", rt, "negotiate")

    neg_tools = [t for t in (t_check, t_promise, t_note, t_transfer) if t] + [t_close]
    task = P.get("negotiate.task", default_negotiate_task(p, cl, bool(t_check), ef, bool(entry)))
    extra = P.get("negotiate.extra")
    signals = signal_schema(p, ef)
    # the extraction labels the customer's last message; a labelled message goes through turn_state (code), which
    # counts it and comes back with the next action, or closes. Re-entry runs on_enter again: its instruction is a
    # variable (the debt statement on the first entry, the answer to the last message after turn_state)
    to_state = [{"target_node": "turn_state", "description": "the customer's last message is a counted event",
                 "condition": _or(*[_eq("x_reply", v) for v in ef["signals"]])}] if signals else []
    nodes["negotiate"] = {
        "name": "negotiate", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "{{w_entry_line}}"} if entry else
                    {"type": "llm", "prompt": "{{w_enter_prompt}}" if signals else neg_enter},
        "on_enter_first_entry_only": not signals,
        "task_message": task + (("\n\n" + extra) if extra else ""),
        "tools": neg_tools,
        "max_node_turns": opts["negotiate_max_turns"], "max_turn_transition": "turn_cap",
        "transitions": [closed] + to_state + goodbye,
    }
    if signals:
        nodes["negotiate"]["extraction_schema"] = signals
        ts = _code_node("turn_state", "turn_state", rt, "negotiate")
        ts["transitions"] = [{"target_node": "close_check", "description": "the ladder or the count is used up: close",
                              "condition": _or(_eq("w_turn_route", "close"))},
                             {"target_node": "turn_cap", "description": "negotiate turns (summed over re-entries) over the cap",
                              "condition": _or(_eq("w_turn_route", "cap"))}] + ts["transitions"]
        nodes["turn_state"] = ts

    nodes["turn_cap"] = _code_node("turn_cap", "turn_cap", rt, "close_check")
    if opts["goodbye_close"]:
        nodes["goodbye_close"] = _code_node("goodbye_close", "goodbye_close", rt, "close_check", source=GOODBYE_CLOSE)
    close_check = _code_node("close_check", "close_check", rt, "finish")
    close_check["transitions"] = [
        {"target_node": "wrap_up", "description": "something is missing before the close",
         "condition": _or(_eq("w_close_route", "wrap"))},
        {"target_node": "finish", "description": "always", "condition": {"operator": "always"}}]
    nodes["close_check"] = close_check
    wrap_tools = [t for t in (t_note, t_transfer, t_promise) if t] + [t_close]
    # the to-do asks for notes "with the reason that applies" and the outcome follows the notes: the step's rules
    # (note reasons, refusals, outcome table) are needed here; this step is entered at most once per call
    labels, roles = _rule_labels(p), rule_roles(p)
    wr = _bul([_rule_line(p, r, labels, roles) for r in wrap_rules(p)])
    wrap_text = ("\nRules for the notes and the outcome:\n" + wr) if wr else ""
    nodes["wrap_up"] = {
        "name": "wrap_up", "node_type": "conversation",
        "on_enter": {"type": "llm", "prompt": P.get("wrap_up.on_enter",
                                                   "Before the call can be closed: {{w_wrap_todo}} Do it now with the tools, one tool call per message. The customer already heard your goodbye: do not say it again or describe what you record; if you must speak, say only 'One moment, please.'")},
        "task_message": P.get("wrap_up.task", "Before the close: {{w_wrap_todo}}\n"
                              "Use the tools, one call per message, after each result. The customer heard your goodbye: do not say it again or describe what you record; if you must speak, say only 'One moment, please.' "
                              "Then call close_call again.\n" + STATUS_LINE + wrap_text),
        "tools": wrap_tools,
        "max_node_turns": 4, "max_turn_transition": "finish",
        "transitions": [{"target_node": "finish", "description": "the model asked to close the call",
                         "condition": _or(_eq("w_close_requested", "yes"))}],
    }
    nodes["finish"] = _code_node("finish", "finish", rt, "close_api")
    # a close the code decided (ladder used up, the N-th 'do not want to talk', the last callback try, a turn cap):
    # the farewell is spoken here, before the engine sends close_call (rule: thank and say goodbye BEFORE close_call)
    nodes["finish"]["transitions"].insert(0, {"target_node": "farewell", "description": "the code closed the call: say goodbye first",
                                              "condition": _or(_eq("w_end_say", "yes"), _eq("w_forced", "yes"))})
    nodes["farewell"] = _code_node("farewell", "farewell", rt, "close_api",
                                   on_enter={"type": "tts", "text": P.get("farewell", "Thank you for your time. Goodbye.")},
                                   source='vset(vs, "w_farewell_done", "yes")\n')
    close_fixed = _api_tool(p, tn["close"], "Close the call.", baked={names["close_outcome"]: "{{w_close_outcome}}",
                                                                     names["close_reason"]: "{{w_close_reason}}"},
                            pre="close_final_pre", rt=rt, retries=0, timeout=to)
    nodes["close_api"] = {
        "name": "close_api", "node_type": "api_request", "passthrough": True,
        "passthrough_default_transition": "end_closed", "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True, "pre_actions": [close_fixed],
        "transitions": [{"target_node": "end_forced", "description": "the call hit a turn limit",
                         "condition": _or(_eq("w_forced", "yes"))}]
                       + [{"target_node": "end_closed", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["end_closed"] = _end_node("end_closed", "call_closed")
    # the farewell node already said goodbye before close_api: the end node says nothing more
    nodes["end_forced"] = _end_node("end_forced", "call_closed_by_limit")


    if opts["reasoning_off"]:
        for n in nodes.values():
            if n.get("node_type") == "conversation":
                n["reasoning_override"] = "disabled"

    wf = {
        "initial_node": "setup",
        "system_prompt": P.get("system_prompt", default_system_prompt(p)),
        "nodes": nodes,
        "global_nodes": {},
    }
    tv = {v["name"]: str(v["example"]) for v in p["variables"] if v.get("example") not in (None, "")}
    if tv:
        wf["test_variables"] = tv
    if opts["final_close_fallback"]:
        fc = _api_tool(p, tn["close"], "Safety net: close the call if it ended without close_call.",
                       baked={names["close_outcome"]: "{{w_close_outcome}}", names["close_reason"]: "{{w_close_reason}}",
                              **{f: "{{" + f + "}}" for f in p["tools"][tn["close"]]["fields"]
                                 if f not in (names["close_outcome"], names["close_reason"])}},
                       pre="final_close_pre", rt=rt, timeout=to, config_extra={"alwaysRunsAt": ["end_of_call"]})
        fc["function"]["name"] = "final_close"
        wf["tools"] = [fc]
    _fill_placeholders(wf, p)
    _strip_nulls(wf)
    return wf


def _fill_placeholders(wf, p):
    """Brief text writes call variables as {name}; the platform substitutes {{name}}."""
    wf["system_prompt"] = _brace_vars(wf["system_prompt"], p)
    for n in wf["nodes"].values():
        if isinstance(n.get("task_message"), str):
            n["task_message"] = _brace_vars(n["task_message"], p)
        oe = n.get("on_enter") or {}
        for k in ("prompt", "text"):
            if isinstance(oe.get(k), str):
                oe[k] = _brace_vars(oe[k], p)


def _strip_nulls(x):
    if isinstance(x, dict):
        for k in [k for k, v in x.items() if v is None]:
            del x[k]
        for v in x.values():
            _strip_nulls(v)
    elif isinstance(x, list):
        for v in x:
            _strip_nulls(v)


def visible_tool_schema(t, call_vars=()):
    """What the dialogue model sees of a tool (docs/tools-and-code.md): name, description and the parameters without a
    baked default. An api_call body field defaulting to its own unset {{name}} is model-filled; a call variable is baked."""
    f = t["function"]
    params = (f.get("parameters") or {}).get("properties") or {}
    props = {}
    if f.get("handler", "").endswith("api_call"):
        for k, s in ((params.get("body") or {}).get("properties") or {}).items():
            m = re.fullmatch(r"\{\{(\w+)\}\}", str(s.get("default", "")))
            if m and m.group(1) not in call_vars:
                props[m.group(1)] = {x: s[x] for x in ("type", "description", "enum") if x in s}
    else:
        props = {k: {x: s[x] for x in ("type", "description", "enum") if x in s} for k, s in params.items() if "default" not in s}
    return {"name": f["name"], "description": f.get("description") or "", "parameters": props}


def prompt_sizes(wf, call_vars=()):
    """Per conversation node: characters of the per-turn prompt (system prompt + task + model-visible tool schemas).
    Tokens are about characters / 4."""
    sp = len(wf.get("system_prompt") or "")
    out = {}
    for k, n in wf["nodes"].items():
        if n.get("node_type") == "conversation":
            out[k] = sp + len(n.get("task_message") or "") + sum(
                len(json.dumps(visible_tool_schema(t, set(call_vars)))) for t in n.get("tools") or [])
    return out


def lint_workflow(wf):
    """Offline structural checks of the documented pitfalls. Returns a list of problems."""
    errs = []
    nodes = wf.get("nodes") or {}
    if wf.get("initial_node") not in nodes:
        errs.append("initial_node missing")
    if "agent_config" in wf:
        errs.append("agent_config present")

    def walk(x, path):
        if x is None:
            errs.append("null at " + path)
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v, path + "." + k)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, path + "[%d]" % i)
    walk(wf, "wf")
    for k, n in nodes.items():
        t = n.get("node_type")
        if n.get("name") != k:
            errs.append("%s: name differs from key" % k)
        if t != "conversation" and n.get("tools"):
            errs.append("%s: tools on non-conversation node" % k)
        if t == "conversation" and n.get("pre_actions"):
            errs.append("%s: pre_actions on conversation node" % k)
        if n.get("passthrough"):
            tr = n.get("transitions") or []
            if not tr or (tr[-1].get("condition") or {}).get("operator") != "always":
                errs.append("%s: passthrough node does not end with always" % k)
            if n.get("passthrough_default_transition") not in nodes:
                errs.append("%s: passthrough_default_transition unknown" % k)
        if t == "end_call":
            if n.get("transitions"):
                errs.append("%s: end_call with transitions" % k)
            if not n.get("terminal"):
                errs.append("%s: end_call without terminal" % k)
        if n.get("max_node_turns") and n.get("max_turn_transition") not in nodes:
            errs.append("%s: max_turn_transition missing/unknown" % k)
        for tr in n.get("transitions") or []:
            if tr.get("target_node") not in nodes:
                errs.append("%s: transition to unknown node %s" % (k, tr.get("target_node")))
            c = tr.get("condition") or {}
            if c.get("logic") == "and":
                errs.append("%s: top-level and" % k)
        for ex in n.get("extraction_schema") or []:
            if "min_confidence" not in ex:
                errs.append("%s: extraction %s without min_confidence" % (k, ex.get("name")))
        for tool in (n.get("tools") or []) + (n.get("pre_actions") or []):
            for hk in ("pre_process_code", "post_process_code"):
                if "{{" in tool.get(hk, "") or "}}" in tool.get(hk, ""):
                    errs.append("%s: braces in %s" % (k, hk))
            code = (((tool.get("function") or {}).get("parameters") or {}).get("properties") or {}).get("code")
            if code and ("{{" in code.get("default", "") or "}}" in code.get("default", "")):
                errs.append("%s: braces in code" % k)
    # every node reaches an end_call node
    ends = {k for k, n in nodes.items() if n.get("node_type") == "end_call"}
    for k in nodes:
        seen, stack, ok = set(), [k], False
        while stack:
            x = stack.pop()
            if x in ends:
                ok = True
                break
            if x in seen or x not in nodes:
                continue
            seen.add(x)
            stack += [t.get("target_node") for t in nodes[x].get("transitions") or []]
            if nodes[x].get("max_turn_transition"):
                stack.append(nodes[x]["max_turn_transition"])
        if not ok:
            errs.append("%s: no path to an end_call node" % k)
    return errs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("policy")
    ap.add_argument("-o", "--out")
    ap.add_argument("--openapi")
    ap.add_argument("--prompts")
    ap.add_argument("--validate", action="store_true", help="dry-run validation on Freya (needs FREYA_API_KEY)")
    ap.add_argument("--save", metavar="SLOT", help="save via freya.save_workflow under this slot")
    ap.add_argument("--state-file", default=str(HERE.parent / ".freya-agents.json"))
    a = ap.parse_args(argv)
    policy = json.loads(pathlib.Path(a.policy).read_text())
    errors, warnings = policy_schema.check(policy)
    for w in warnings:
        print("warning:", w, file=sys.stderr)
    for e in errors:
        print("error:", e, file=sys.stderr)
    openapi = json.loads(pathlib.Path(a.openapi).read_text()) if a.openapi else None
    prompts = json.loads(pathlib.Path(a.prompts).read_text()) if a.prompts else None
    wf = generate_workflow(policy, prompts, openapi)
    problems = lint_workflow(wf)
    for pr in problems:
        print("lint:", pr, file=sys.stderr)
    text = json.dumps(wf, indent=1)
    if a.out:
        pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(a.out).write_text(text)
        print("wrote %s (%d nodes, %d bytes)" % (a.out, len(wf["nodes"]), len(text)))
    else:
        print(text)
    if a.validate or a.save:
        sys.path.insert(0, str(HERE.parent))
        import freya
        if a.validate:
            freya.validate_workflow("dry-run " + pathlib.Path(a.policy).stem, wf)
            print("dry-run validation: ok")
        if a.save:
            print(json.dumps(freya.save_workflow(a.save, wf, state_file=a.state_file)))
    return 2 if errors else (1 if problems else 0)


if __name__ == "__main__":
    sys.exit(main())
