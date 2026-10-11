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
         -> prepare_account (code) per product: band, deadline, floor/full, offers -> {{w_account_facts}}
         -> negotiate       debt statement, negotiation, read-back, promises, notes, triggers, closing
                            tools: create_promise, add_note, transfer_to_agent, close_call
      close_call result     -> end_closed (end_call)
      goodbye without close -> goodbye_close (code) -> close_check -> finish -> close_api (routing intent; option goodbye_close)
      turn cap reached      -> force_close (code) -> close_api (api_request close_call) -> end_forced
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
    farewell                       spoken by end_forced
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
              timeout=12000, mapping=None, config_extra=None):
    """An api_call tool from the policy's tool definition. Call variables with the field's name are baked,
    `baked` gives fixed values, every other field is filled by the model."""
    t = p["tools"][name]
    props, required = {}, []
    calls = set(_var_names(p))
    field_hints = field_hints or {}
    baked = baked or {}
    for f, spec in t["fields"].items():
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


def _pre_verification_outcomes(p):
    out = {c["outcome"] for c in p["third_party"]["cases"] if c["outcome"]}
    if p["identity"]["fail"]["outcome"]:
        out.add(p["identity"]["fail"]["outcome"])
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


def _trigger_lines(p, cl, verified):
    """One short line per trigger that can happen in this phase and needs an action (a tool, a note, an outcome).
    The verbatim rule text is in the system prompt or the step's rules, so a trigger that only says something is
    left to its rule, and a trigger whose rules all belong to the other step is left out; trigger notes are added
    only when the policy has no rules list."""
    out = []
    pre_out = _pre_verification_outcomes(p)
    case_pairs = {(c["outcome"], c["reason"]) for c in p["third_party"]["cases"]}
    ph = rule_phases(p) if p["rules"] else {}
    other = "greet" if verified else "negotiate"
    for t in p["triggers"]:
        act = t["action"]
        if verified and t["outcome"] in pre_out:
            continue            # third-party / identity situations cannot happen after verification
        if not verified and (act == "note" or (t["outcome"], t["reason"]) in case_pairs):
            continue            # product notes need the account; third-party cases are listed separately
        fail = p["identity"]["fail"]
        if not verified and act == "close" and not t["say"] and fail["outcome"] and (t["outcome"], t["reason"]) == (fail["outcome"], fail["reason"]):
            continue            # the failed identity check has its own line in the step
        if ph:
            if not (t["outcome"] or t["say"] or (act == "transfer" and t["queue"]) or (act == "note" and t["note_reason"])):
                continue        # nothing to do beyond its rule, which is in the prompt
            tph = _trigger_phases(p, t, ph)
            if tph == {other}:
                continue        # every rule of it applies only in the other step
        else:
            tph = set()
        here = "negotiate" if verified else "greet"
        # the situation is described by its rule when that rule is in this prompt; the name is enough to point at it
        twice = re.search(r"\btwice\b|\bsecond time\b|\btwo times\b", t["detect"] or "")
        line = t["name"] + ((" (" + (t["detect"] or "when it applies") + ")") if twice or not (tph & {here, "all"}) else "") + ":"
        if t["say"]:
            line += " Say: \"" + t["say"] + "\"."
        if act == "transfer" and t["queue"]:
            line += (" Transfer: call transfer_to_agent with queue '" + t["queue"] + "' and wait for its result, then close "
                     + t["outcome"] + (" / " + t["reason"] if t["reason"] else "") + ".")
        elif act == "note" and t["note_reason"] and p["notes"]["enabled"]:
            line += " Note reason '" + t["note_reason"] + "'."
        elif t["outcome"]:
            pr_ = t["outcome"] + (" / " + t["reason"] if t["reason"] else "")
            line += (" Close: " + pr_ + ".") if act != "continue" else (" When it ends the call, the outcome is " + pr_ + ".")
            if twice:
                line += (" The first time: do not mention security; acknowledge it and make the current product's next offer." if verified else
                         " The first time: explain once, as the rules say, why the security questions are asked, and ask again.")
                line += " The second time: close at once (thank, goodbye and close_call in that message)."
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
        named = re.findall(r"`(\w+)`", r["text"].split("|")[0])
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
                                         or (said and said <= post_out and not hit(x, any_trig))):
            # an account field in a rule filed nowhere, or only outcomes that need the account: after verification
            out[r["id"]] = "negotiate"
        else:
            out[r["id"]] = "all"
        free[r["id"]] = not (g or n_sec or n_trig or b)
    # "[overrides <ids>]": a rule filed nowhere that replaces rules of one step belongs to that step
    for r in p["rules"]:
        m = re.search(r"\[overrides ([^\]]+)\]", r["text"])
        if m and out.get(r["id"]) == "all" and free.get(r["id"]):
            targets = {out.get(t.strip()) for t in m.group(1).split(",")}
            if len(targets) == 1 and targets <= {"greet", "negotiate"}:
                out[r["id"]] = targets.pop()
    return out


def _phase_rules(p, phase):
    ph = rule_phases(p)
    return _bul(["[" + r["id"] + "] " + r["text"] for r in p["rules"] if ph.get(r["id"]) == phase])


def default_system_prompt(p):
    vars_ = _bul(["%s = {{%s}}%s" % (v["name"], v["name"], (" (" + v["meaning"] + ")") if v["meaning"] else "") for v in p["variables"]])
    rules = _phase_rules(p, "all")
    ph = rule_phases(p)
    # a phrase quoted word for word in a rule is already in the prompt of every step where that rule applies
    kept = " ".join(r["text"] for r in p["rules"] if ph.get(r["id"]) != "drop")
    phrases = _bul(["%s: \"%s\"" % (k, v) for k, v in p["phrases"].items() if isinstance(v, str) and v.strip() not in kept])
    return "\n".join(x for x in [
        "You are a bank's digital phone agent on a live call. Speak %s: one to three short sentences per turn, one question at a time." % p["language"],
        "Use only facts from this prompt, the call variables and tool results; never invent amounts, dates, reasons, policies or offers.",
        "",
        "Call variables:",
        vars_,
        "",
        "Tools:",
        "- When a rule says to call a tool, call it; saying you did is not enough. One tool call per message; wait for its result.",
        "- Follow a result's what_to_do_next exactly and speak in the same turn; never end a turn with nothing to say.",
        "- A failed or stopped call recorded nothing: act on 'Status of your last tool request'. Say something was recorded, scheduled or transferred only after a result says it worked (a read-back asks for confirmation).",
        "- close_call comes last and once: after all other results, in the message with your thanks and goodbye, with no other tool call.",
        "",
        "The bank's rules for every step (each step adds its own):",
        rules,
        "",
        ("Fixed phrases (word for word where they apply):\n" + phrases) if phrases else "",
    ] if x is not None)


STATUS_LINE = "Status of your last tool request: {{w_last_event}}"


def default_greet_task(p, cl):
    q = p["identity"]["questions"]
    tp = p["third_party"]
    verbatim = not p["rules"]          # rules missing: carry the section texts here instead
    cases = []
    for c in tp["cases"]:
        line = "If " + c["trigger"] + ":"
        if c["say"]:
            line += " say \"" + c["say"] + "\";"
        if c["action"] == "callback":
            line += (" ask when the customer can be reached, then call schedule_callback (date YYYY-MM-DD, time HH:MM 24 h) with each day and"
                     " time the person offers, even one outside the allowed window: the system checks it and says what to do next;")
        if c["outcome"]:
            line += " close " + c["outcome"] + (" / " + c["reason"] if c["reason"] else "") + "."
        cases.append(line)
    qlines = ["%d. %s Then call verify_identity with field '%s' and the answer (%s)." % (
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
        ("With anyone who is not the customer, say nothing about an account, a product, money or a payment, not even that you cannot discuss them. "
         "If they ask what the call is about or offer to pay, say only that you need to speak with the customer personally, then ask when the customer can be reached.") if tp["cases"] else "",
        wins,
        "Security questions: only to the customer, after they confirmed who they are, one at a time. An answer given in pieces over several turns (day and month, then the year; two digits, then two more) is not a refusal and not a wrong answer: call verify_identity with each piece; pieces are joined automatically; ask only for the missing part. Count wrong answers only from verify_identity results.",
        _bul(qlines, ""),
        "Ask each question without giving a reason. Explain why only if the customer will not answer or asks why, and only once in the call.",
        _bul(p["identity"]["wrong_counts_rules"]) if verbatim else "",
        ("If the check fails: " + fail["say"] + " Close " + fail["outcome"] + (" / " + fail["reason"] if fail["reason"] else "") + ".") if fail["outcome"] else "",
        "Other situations:",
        _bul(_trigger_lines(p, cl, False)),
        ("Rules for this step:\n" + _phase_rules(p, "greet")) if _phase_rules(p, "greet") else "",
        STATUS_LINE,
    ] if x)


def default_negotiate_task(p, cl, check=False):
    multi = p["products"]["max_count"] > 1
    notes = p["notes"]
    pm = p["promise"]
    verbatim = not p["rules"]
    pre_out = _pre_verification_outcomes(p)
    lines = [
        "Current step: the customer is verified. Handle the late products below by the bank's rules, from stating the debt to closing the call.",
        "The security questions are over: never mention them or why they were asked.",
        "{{w_account_facts}}",
        "{{w_date_facts}}",
        "Use only these product ids. Deadlines, floors and attempt offers are computed: use them as given; weekdays and years come only from the calendar facts. "
        "A promise needs an amount from the floor to the full amount and a business day from today up to that product's deadline.",
        "Count persuasion attempts per product; when they are used up, make no further offer for that product. Say sympathy at most once.",
        _bul(p["amounts"]["rules"] + [p["dates"]["customer_date_after_deadline"], p["dates"]["vague_date"]] + p["dates"]["rules"]) if verbatim else "",
        "Read-back: " + pm["readback"] if verbatim else "",
    ]
    if check:
        lines.append("When the customer names a date, a period ('in two weeks', 'end of the month') or an amount themselves, call check_offer before you answer and follow its what_to_do_next. "
                     "No check for the full amount today, for an offer from the account facts, or for a value check_offer already accepted: go straight to the read-back.")
    lines.append("Recording: after a clear yes to the read-back, call create_promise once per product, one per message, after each result. "
                 "If it fails, call it again with the same values, at most %d more time(s); if it still fails: %s."
                 % (pm["retry_on_failure"], pm["failure_say"] or "say it could not be recorded"))
    if notes["enabled"]:
        lines.append("Notes: {{w_notes_rule}} Only after the last note result call close_call, in a separate message."
                     + (" Unless a rule says to send notes before a transfer, a transfer means no notes at all; never send a note after a transfer."
                        if notes["forbidden_on_transfer"] else " Before a transfer, follow the rules on whether notes are sent first."))
    else:
        lines.append("Do not call add_note in this call.")
    rows = [r for r, t in zip(_outcome_lines(p), p["outcomes"]["table"]) if t["outcome"] not in pre_out]
    lines += ["Things the customer may say:", _bul(_trigger_lines(p, cl, True)),
              # with a rules list the outcome table is in the rules (system prompt or this step) and in close_call
              ("Closing (outcome / reason): " + "; ".join(rows) + ".") if verbatim else "",
              _bul(p["outcomes"]["precedence"]) if verbatim else "",
              p["outcomes"]["multi_product"] if multi and verbatim else "",
              ("Rules for this step:\n" + _phase_rules(p, "negotiate")) if _phase_rules(p, "negotiate") else "",
              STATUS_LINE]
    return "\n".join(x for x in lines if x)


def _close_reason_desc(p):
    by = {}
    for r in p["outcomes"]["table"]:
        by.setdefault(r["outcome"], [])
        if r["reason"]:
            by[r["outcome"]].append(r["reason"])
    parts = ["%s: %s" % (o, ", ".join(rs) if rs else "no reason") for o, rs in by.items()]
    return "Exactly as in the outcome table: " + "; ".join(parts) + "."


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
    cl = p["closing"]
    P = _Prompts(prompts)
    tn = {r: _tool_name(p, r) for r in ROLES}
    to = opts["timeout_ms"]

    def tdesc(role, default):
        name = tn[role]
        d = P.get("tool." + name, default)
        note = (p["tools"].get(name) or {}).get("notes") or ""
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
                       pre="note_pre", post="note_post", rt=rt, timeout=to) if tn["note"] and p["notes"]["enabled"] else None

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
    nodes["setup"] = _code_node("setup", "setup", rt, "greet_verify")

    opening_line = P.get("opening_line", p["opening"].get("line") or "")
    greet_on_enter = {"type": "tts", "text": _brace_vars(opening_line, p)} if opening_line else {
        "type": "llm", "prompt": P.get("greet_verify.on_enter", "\n".join(
            ["This is the first sentence of the call. Say the opening exactly as the bank requires and then ask whether you are speaking with the customer, as the rules below say. Nothing else. Do not call a tool in this turn.",
             _bul([p["opening"]["text"]]
                  + [x for x in (p["opening"].get("notes") or []) if isinstance(x, str)][:4])]))}
    greet_tools = [t_verify] + [t for t in (t_callback, t_transfer) if t] + [t_close]
    task = P.get("greet_verify.task", default_greet_task(p, cl))
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
        ] + goodbye,
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
    task = P.get("negotiate.task", default_negotiate_task(p, cl, bool(t_check)))
    extra = P.get("negotiate.extra")
    nodes["negotiate"] = {
        "name": "negotiate", "node_type": "conversation",
        "on_enter": {"type": "llm", "prompt": P.get("negotiate.on_enter",
                                                   "Identity is verified. State the debt as the rules require, using the account facts (every late product, one sentence each), then ask the first payment question the rules require. Do not call a tool in this turn.")},
        "on_enter_first_entry_only": True,
        "task_message": task + (("\n\n" + extra) if extra else ""),
        "tools": neg_tools,
        "max_node_turns": opts["negotiate_max_turns"], "max_turn_transition": "turn_cap",
        "transitions": [closed] + goodbye,
    }

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
    nodes["wrap_up"] = {
        "name": "wrap_up", "node_type": "conversation",
        "on_enter": {"type": "llm", "prompt": P.get("wrap_up.on_enter",
                                                   "Before the call can be closed: {{w_wrap_todo}} Do it now with the tools, one tool call per message. The customer already heard your goodbye: do not say it again or describe what you record; if you must speak, say only 'One moment, please.'")},
        "task_message": P.get("wrap_up.task", "The call is about to be closed. Before that: {{w_wrap_todo}}\n"
                              "Use the tools for it, one tool call per message, and wait for each result. The customer already heard your goodbye: do not say it again or describe what you record; if you must speak, say only 'One moment, please.' "
                              "Then call close_call again with the outcome that applies.\n" + STATUS_LINE),
        "tools": wrap_tools,
        "max_node_turns": 4, "max_turn_transition": "finish",
        "transitions": [{"target_node": "finish", "description": "the model asked to close the call",
                         "condition": _or(_eq("w_close_requested", "yes"))}],
    }
    nodes["finish"] = _code_node("finish", "finish", rt, "close_api")
    close_fixed = _api_tool(p, tn["close"], "Close the call.", baked={names["close_outcome"]: "{{w_close_outcome}}",
                                                                     names["close_reason"]: "{{w_close_reason}}"},
                            pre="close_final_pre", rt=rt, retries=0, timeout=to)
    nodes["close_api"] = {
        "name": "close_api", "node_type": "api_request", "passthrough": True,
        "passthrough_default_transition": "end_closed", "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True, "pre_actions": [close_fixed],
        "transitions": [{"target_node": "end_forced", "description": "the call hit a turn limit",
                         "condition": _or(_eq("w_forced", "yes"))},
                        {"target_node": "end_closed", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["end_closed"] = _end_node("end_closed", "call_closed")
    nodes["end_forced"] = _end_node("end_forced", "call_closed_by_limit", P.get("farewell", "Thank you for your time. Goodbye."))

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
