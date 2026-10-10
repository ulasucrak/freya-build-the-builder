"""Test mode: run a generated workflow against a fake bank that lives inside Freya.

The live bank mock only knows its own stock cases. To run synthetic banks (own identity questions, products,
enums, faults) end to end on the real engine, every bank `api_call` is replaced by a `code_exec` function that
emulates the bank in the sandbox:

    wf_test = to_test_mode(workflow, openapi)

Verified on the platform (probe slot dev-T10-probe, 10 Oct):
- a `code_exec` tool whose snippet ends with a top-level `return <dict>` produces the envelope
  {status: success, data: <dict>, status_code: 200}; the dialogue model sees it, the engine flattens it into
  `<tool>_response.data.*` and routing fires on it, exactly like an api_call result;
- the model's arguments arrive in the sandbox global `params`; `default: "{{var}}"` parameters are baked as for
  api_call; `vars` persists between calls (the fake bank keeps its state there as a JSON string);
- a snippet that raises gives status error / status_code 500 and bumps `<tool>_failure_count` (used for faults);
- `code_exec` in the `pre_actions` of `api_request` and `code` nodes works; node-level response_mapping applies;
- pre/post hooks are dropped by the platform on code_exec, so they are executed inside the snippet with exec();
- results of `end_of_call` lifecycle tools are not visible anywhere afterwards, so those may be pointed at a
  "sink" (an api_call on the kit's bank mock with a throw-away practice session) whose ledger holds the args.

The scenario (identity answers, products, faults, max_wrong, close_reasons) is read at run time from the call
variable named by `scenario_var` (JSON string). Nothing about a particular brief is written here: tool fields
and enums come from the supplied openapi, everything else from the scenario.
"""
import copy
import json
import re

STATE_VAR = "fakebank_state"
SCENARIO_VAR = "scenario_json"
SINK_VAR = "fakebank_sink_id"

# ---------------------------------------------------------------------------------------------------------------
# The fake bank. This text runs in the Freya sandbox (inside a function body, globals `params` and `vars`) and
# locally (tests, ledger replay). Rules for it: no double braces anywhere, stdlib json/re only, Python 3.9.
FAKEBANK_SRC = r'''
import json as _fj
import re as _fre

_WLOG = 'fakebank_w_'
_WRITE = ('create_promise', 'add_note', 'schedule_callback', 'transfer_to_agent', 'close_call')

def _fb_new():
    return dict(verified=dict(), wrong=0, hinted=False, promises=[], notes=[], callbacks=[], transfers=[],
                outcome=None, reason=None, closed=False, failures=dict(), seen=dict(), calls=[])

def _fb_unpack(d):
    """Compact stored form -> working dict. Short keys keep the variable table (sandbox payload) small."""
    st = _fb_new()
    if not isinstance(d, dict):
        return st
    if 'calls' in d:
        st.update(d)
        return st
    st['verified'] = dict((k, True) for k in d.get('v') or [])
    st['wrong'] = int(d.get('w') or 0)
    st['hinted'] = bool(d.get('h'))
    st['promises'] = [dict(promise_id='pr' + str(i + 1), product_id=x[0], type=x[1], amount=x[2], date=x[3]) for i, x in enumerate(d.get('P') or [])]
    st['notes'] = [dict(product_id=x[0], reason=x[1], text=x[2]) for x in d.get('N') or []]
    st['callbacks'] = [dict(date=x[0], time=x[1]) for x in d.get('C') or []]
    st['transfers'] = [dict(queue=x) for x in d.get('T') or []]
    st['outcome'] = d.get('o')
    st['reason'] = d.get('r')
    st['closed'] = bool(d.get('x'))
    st['failures'] = dict(d.get('f') or dict())
    st['seen'] = dict(d.get('s') or dict())
    calls = []
    for i, c in enumerate(d.get('L') or []):
        res = dict(ok=bool(c[2]))
        if len(c) > 3 and c[3]:
            res['error'] = c[3]
        calls.append(dict(n=i + 1, tool=c[0], args=(c[4] if len(c) > 4 else dict()), status=c[1], result=res))
    st['calls'] = calls
    return st

def _fb_pack(st):
    d = dict(v=sorted(k for k, ok in st['verified'].items() if ok), w=st['wrong'], h=1 if st['hinted'] else 0,
             P=[[p['product_id'], p['type'], p['amount'], p['date']] for p in st['promises']],
             N=[[n['product_id'], n['reason'], str(n['text'] or '')[:80]] for n in st['notes']],
             C=[[c['date'], c['time']] for c in st['callbacks']], T=[t['queue'] for t in st['transfers']],
             o=st['outcome'], r=st['reason'], x=1 if st['closed'] else 0, f=st['failures'], s=st['seen'])
    d['L'] = [_fb_centry(c) for c in st['calls']]
    return _fj.dumps(d, separators=(',', ':'))

def _fb_centry(c):
    res = c.get('result') or dict()
    e = [c['tool'], c['status'], 1 if res.get('ok') else 0, str(res.get('error') or '')[:60]]
    if c['tool'] in _WRITE:
        a = dict()
        for k, v in (c.get('args') or dict()).items():
            a[k] = v[:40] if isinstance(v, str) else v
        e.append(a)
    return e

def _fb_load(vars, spec):
    st = vars.get(spec.get('state_var') or 'fakebank_state')
    if isinstance(st, str) and st.strip():
        try:
            st = _fj.loads(st)
        except Exception:
            st = None
    return _fb_unpack(st)

def _fb_scenario(vars, spec):
    sc = vars.get(spec.get('scenario_var') or 'scenario_json')
    if isinstance(sc, str):
        try:
            sc = _fj.loads(sc)
        except Exception:
            sc = None
    return sc if isinstance(sc, dict) else dict()

def _fb_num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).strip().replace(',', ''))
    except Exception:
        return None

def _fb_clean(v):
    if isinstance(v, float) and v == int(v):
        return int(v)
    return v

def _fb_products(sc):
    out = []
    for i, p in enumerate(sc.get('products') or []):
        if not isinstance(p, dict):
            continue
        q = dict()
        q['product_id'] = str(p.get('product_id') or ('p' + str(i + 1)))
        for k, v in p.items():
            if k != 'product_id':
                q[k] = _fb_clean(v)
        out.append(q)
    return out

def _fb_role(spec, tool, role, default=None):
    t = (spec.get('tools') or dict()).get(tool) or dict()
    return (t.get('roles') or dict()).get(role) or default

def _fb_enum(spec, tool, field):
    t = (spec.get('tools') or dict()).get(tool) or dict()
    f = (t.get('fields') or dict()).get(field) or dict()
    return f.get('enum')

_DATE = _fre.compile(r'^\s*(\d{4})-(\d{2})-(\d{2})\s*$')

def _fb_norm(x):
    return _fre.sub(r'\s+', ' ', str(x if x is not None else '').strip()).casefold()

def _fb_logic(tool, args, st, sc, spec):
    """Returns (http_status, body). Mutates st."""
    products = _fb_products(sc)
    ids = [p['product_id'] for p in products]
    ident = sc.get('identity') or dict()
    if st.get('closed'):
        return 200, dict(ok=False, error='the call is already closed')
    for f in sc.get('faults') or []:
        if isinstance(f, dict) and f.get('tool') == tool:
            n = int(st['seen'].get(tool) or 0)
            if n < int(f.get('fail_first_n') or 0):
                st['failures'][tool] = int(st['failures'].get(tool) or 0) + 1
                return 500, dict(ok=False, error='service unavailable, nothing was recorded')
    all_ok = bool(ident) and all(st['verified'].get(k) for k in ident)
    if tool == 'verify_identity':
        fk = _fb_role(spec, tool, 'field', 'field')
        ak = _fb_role(spec, tool, 'answer', 'answer')
        field = str(args.get(fk) or '').strip()
        answer = args.get(ak)
        allowed = _fb_enum(spec, tool, fk) or list(ident.keys())
        if field not in ident or field not in allowed:
            return 200, dict(ok=False, error='field must be ' + ' or '.join(str(a) for a in allowed))
        mw = sc.get('max_wrong')
        left = None if mw is None else max(0, int(mw) - int(st.get('wrong') or 0))
        if left is not None and left <= 0:
            return 200, dict(ok=False, verified=False, wrong_answers_left=0, error='identity check is over')
        expected = str(ident.get(field))
        em = _DATE.match(expected)
        if st['verified'].get(field):
            out = dict(ok=True, verified=True)
            if left is not None:
                out['wrong_answers_left'] = left
            return 200, out
        if em:
            am = _DATE.match(str(answer if answer is not None else ''))
            if not am:
                return 200, dict(ok=False, error='answer is not a date. Send it as YYYY-MM-DD.')
            good = am.groups() == em.groups()
            swapped = (not good) and am.group(1) == em.group(1) and am.group(2) == em.group(3) and am.group(3) == em.group(2)
            if swapped and not st.get('hinted'):
                st['hinted'] = True
                out = dict(ok=True, verified=False)
                if left is not None:
                    out['wrong_answers_left'] = left
                out['hint'] = 'day_month_swapped'
                return 200, out
        else:
            good = _fb_norm(answer) == _fb_norm(expected)
        if good:
            st['verified'][field] = True
        else:
            st['wrong'] = int(st.get('wrong') or 0) + 1
            if left is not None:
                left = max(0, left - 1)
        out = dict(ok=True, verified=bool(good))
        if left is not None:
            out['wrong_answers_left'] = left
        out['all_verified'] = all(st['verified'].get(k) for k in ident)
        return 200, out
    if tool == 'get_account':
        if not all_ok:
            return 200, dict(ok=False, error='identity not verified')
        return 200, dict(ok=True, products=products)
    if tool == 'create_promise':
        if not all_ok:
            return 200, dict(ok=False, error='identity not verified')
        pk = _fb_role(spec, tool, 'product', 'product_id')
        amk = _fb_role(spec, tool, 'amount', 'amount')
        dk = _fb_role(spec, tool, 'date', 'date')
        if not _DATE.match(str(args.get(dk) or '')):
            return 200, dict(ok=False, error=dk + ' must be YYYY-MM-DD')
        pid = str(args.get(pk) or '').strip()
        if pid not in ids:
            return 200, dict(ok=False, error='unknown ' + pk)
        amt = _fb_num(args.get(amk))
        if amt is None or amt <= 0:
            return 200, dict(ok=False, error=amk + ' must be a positive number')
        if any(p.get('product_id') == pid for p in st['promises']):
            return 200, dict(ok=False, error='this product already has a promise')
        prid = 'pr' + str(len(st['promises']) + 1)
        ptype = None
        for p in products:
            if p['product_id'] == pid:
                ptype = p.get('type')
        st['promises'].append(dict(promise_id=prid, product_id=pid, type=ptype, amount=_fb_clean(amt), date=str(args.get(dk)).strip()))
        return 200, dict(ok=True, promise_id=prid)
    if tool == 'add_note':
        pk = _fb_role(spec, tool, 'product', 'product_id')
        rk = _fb_role(spec, tool, 'reason', 'reason')
        tk = _fb_role(spec, tool, 'text', 'text')
        reasons = _fb_enum(spec, tool, rk)
        r = args.get(rk)
        if reasons and r not in reasons:
            return 200, dict(ok=False, error='unknown ' + rk)
        st['notes'].append(dict(product_id=args.get(pk), reason=r, text=args.get(tk) or ''))
        return 200, dict(ok=True)
    if tool == 'schedule_callback':
        dk = _fb_role(spec, tool, 'date', 'date')
        tk = _fb_role(spec, tool, 'time', 'time')
        d, t = str(args.get(dk) or ''), str(args.get(tk) or '')
        if not _DATE.match(d) or not _fre.match(r'^\s*\d{2}:\d{2}\s*$', t):
            return 200, dict(ok=False, error=dk + ' must be YYYY-MM-DD and ' + tk + ' HH:MM')
        st['callbacks'].append(dict(date=d.strip(), time=t.strip()))
        return 200, dict(ok=True)
    if tool == 'transfer_to_agent':
        qk = _fb_role(spec, tool, 'queue', 'queue')
        queues = _fb_enum(spec, tool, qk)
        q = args.get(qk)
        if (queues and q not in queues) or not q:
            return 200, dict(ok=False, error='unknown ' + qk)
        st['transfers'].append(dict(queue=q))
        return 200, dict(ok=True)
    if tool == 'close_call':
        ok_ = _fb_role(spec, tool, 'outcome', 'outcome')
        rk = _fb_role(spec, tool, 'reason', 'reason')
        outcomes = _fb_enum(spec, tool, ok_)
        o = args.get(ok_)
        r = args.get(rk)
        if r is not None and str(r).strip() == '':
            r = None
        if (outcomes and o not in outcomes) or not o:
            return 200, dict(ok=False, error='unknown ' + ok_)
        table = sc.get('close_reasons')
        if isinstance(table, dict) and o in table:
            allowed = [x for x in (table.get(o) or []) if x]
            if (allowed and r not in allowed) or (not allowed and r is not None):
                return 200, dict(ok=False, error='this outcome needs one of these reasons: ' + ', '.join(allowed))
        st['outcome'] = o
        st['reason'] = r
        st['closed'] = True
        return 200, dict(ok=True)
    return 200, dict(ok=True)

def _fb_run(tool, args, vars, spec):
    st = _fb_load(vars, spec)
    sc = _fb_scenario(vars, spec)
    args = dict(args) if isinstance(args, dict) else dict()
    for k in ('customer_id', 'code', 'timeout_ms'):
        args.pop(k, None)
    try:
        status, body = _fb_logic(tool, args, st, sc, spec)
    except Exception as e:
        status, body = 200, dict(ok=False, error='fake bank error: ' + str(e)[:200])
    st['seen'][tool] = int(st['seen'].get(tool) or 0) + 1
    c = dict(n=len(st['calls']) + 1, tool=tool, args=args, status=status, result=body)
    st['calls'].append(c)
    vars[spec.get('state_var') or 'fakebank_state'] = _fb_pack(st)
    if tool in _WRITE:
        # Parallel tool calls in one turn each start from the same state and the last write wins. A small
        # per-call record of every write lets the runner put back what a parallel call recorded.
        e = _fb_centry(c)
        k = _fj.dumps(e, sort_keys=True)
        vars[_WLOG + tool + '_' + str(st['seen'][tool]) + '_' + str(sum(ord(ch) * (i + 1) for i, ch in enumerate(k)) % 99991)] = k
    return status, body

def _fb_entry(tool, params, vars, spec, pre_src, post_src):
    body = dict()
    for k, v in (params or dict()).items():
        if k not in ('code', 'timeout_ms'):
            body[k] = v
    config = dict(url='fakebank://' + tool, method='POST', headers=dict(), body=body)
    if pre_src:
        try:
            exec(pre_src, dict(vars=vars, config=config))
        except Exception:
            pass
    u = str(config.get('url') or '')
    if not u.startswith('fakebank://'):
        h = u.split('://', 1)[-1].split('/', 1)[0].split('?', 1)[0].split('@')[-1]
        raise Exception("DNS resolution failed for '" + h + "'")
    b = config.get('body')
    if isinstance(b, str):
        try:
            b = _fj.loads(b)
        except Exception:
            b = dict()
    status, data = _fb_run(tool, b if isinstance(b, dict) else dict(), vars, spec)
    if status >= 400:
        raise Exception('API request failed: ' + repr(data))
    result = dict(status='success', data=data, message='Successfully executed POST request', status_code=status)
    if post_src:
        try:
            exec(post_src, dict(result=result, vars=vars))
        except Exception:
            pass
    return result.get('data') if isinstance(result, dict) else data
'''

KNOWN_TOOLS = ("verify_identity", "get_account", "create_promise", "add_note", "schedule_callback",
               "transfer_to_agent", "close_call")


def _no_braces_literal(s):
    """A Python string literal for s with no '{' or '}' characters in it (the engine may template {{...}})."""
    return repr(s).replace("{", "\\x7b").replace("}", "\\x7d")


# ---------------------------------------------------------------------------------------------------------------
# openapi -> spec

def _resolve(openapi, node):
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 20:
        ref = node["$ref"]
        cur = openapi
        for part in ref.lstrip("#/").split("/"):
            cur = (cur or {}).get(part) if isinstance(cur, dict) else None
        node = cur or {}
        seen += 1
    return node if isinstance(node, dict) else {}


def _body_schema(openapi, op):
    rb = _resolve(openapi, (op or {}).get("requestBody") or {})
    content = rb.get("content") or {}
    sch = (content.get("application/json") or next(iter(content.values()), {}) or {}).get("schema") or {}
    return _resolve(openapi, sch)


def _pick(fields, pred):
    for name, f in fields.items():
        if pred(name.lower(), f):
            return name
    return None


def _roles(tool, fields):
    """Which body field plays which part. Names first, types/enums as fallback (fields may be renamed)."""
    own = {k: v for k, v in fields.items() if k != "customer_id"}
    has = lambda *words: (lambda n, f: any(w in n for w in words))
    is_enum = lambda n, f: bool(f.get("enum"))
    r = {}
    if tool == "verify_identity":
        r["field"] = _pick(own, lambda n, f: n in ("field", "question")) or _pick(own, is_enum) or _pick(own, has("field", "question"))
        r["answer"] = _pick(own, lambda n, f: n != (r["field"] or "").lower() and "answer" in n) or \
            _pick({k: v for k, v in own.items() if k != r["field"]}, lambda n, f: True)
    elif tool == "create_promise":
        r["product"] = _pick(own, has("product"))
        r["amount"] = _pick(own, has("amount")) or _pick(own, lambda n, f: f.get("type") in ("number", "integer"))
        r["date"] = _pick(own, lambda n, f: f.get("format") == "date") or _pick(own, has("date", "day"))
    elif tool == "add_note":
        r["product"] = _pick(own, has("product"))
        r["reason"] = _pick(own, lambda n, f: n == "reason") or _pick(own, is_enum) or _pick(own, has("reason", "code"))
        r["text"] = _pick(own, lambda n, f: n in ("text", "note", "comment")) or _pick(own, has("text", "note"))
    elif tool == "schedule_callback":
        r["date"] = _pick(own, lambda n, f: f.get("format") == "date") or _pick(own, has("date", "day"))
        r["time"] = _pick(own, has("time", "hour"))
    elif tool == "transfer_to_agent":
        r["queue"] = _pick(own, lambda n, f: n == "queue") or _pick(own, is_enum) or _pick(own, has("queue", "team"))
    elif tool == "close_call":
        r["outcome"] = _pick(own, lambda n, f: n == "outcome") or _pick(own, is_enum) or _pick(own, has("outcome", "result"))
        r["reason"] = _pick(own, lambda n, f: n == "reason") or _pick(own, has("reason"))
    return {k: v for k, v in r.items() if v}


def spec_from_openapi(openapi, scenario_var=SCENARIO_VAR, state_var=STATE_VAR):
    """{tools: {name: {path, fields: {f: {type, enum}}, required, roles}}, scenario_var, state_var}"""
    tools = {}
    for path, ops in ((openapi or {}).get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        op = ops.get("post") or next((v for v in ops.values() if isinstance(v, dict)), {})
        name = op.get("operationId") or path.rstrip("/").split("/")[-1]
        sch = _body_schema(openapi, op)
        fields = {}
        for f, fs in (sch.get("properties") or {}).items():
            fs = _resolve(openapi, fs)
            fields[f] = {"type": fs.get("type"), "enum": fs.get("enum"), "format": fs.get("format")}
        tools[name] = {"path": path, "fields": fields, "required": list(sch.get("required") or []),
                       "roles": _roles(name, fields)}
    return {"tools": tools, "scenario_var": scenario_var, "state_var": state_var}


# ---------------------------------------------------------------------------------------------------------------
# snippet generation and local execution

def _tool_spec(tool, spec):
    """Only the entry of this tool: every snippet carries its own copy, so keep it small."""
    t = (spec.get("tools") or {}).get(tool)
    return {"tools": {tool: t} if t else {}, "scenario_var": spec.get("scenario_var"), "state_var": spec.get("state_var")}


def tool_module(tool, spec, pre_src=None, post_src=None):
    """Plain Python module text run inside the snippet: fake bank + this tool's spec + its hooks."""
    return "\n".join([
        FAKEBANK_SRC.strip("\n"),
        "_FB_SPEC = _fj.loads(" + repr(json.dumps(_tool_spec(tool, spec), sort_keys=True, separators=(",", ":"))) + ")",
        "_fb_out = _fb_entry(" + repr(tool) + ", params, vars, _FB_SPEC, "
        + (repr(pre_src) if pre_src and pre_src.strip() else "None") + ", "
        + (repr(post_src) if post_src and post_src.strip() else "None") + ")"]) + "\n"


def tool_code(tool, spec, pre_src=None, post_src=None):
    """The code_exec source for one fake-bank tool. Ends with a top-level `return`.

    The platform rejects large sandbox payloads (HTTP 413 "Sandbox payload too large", snippet + variable table),
    so the module is zlib-compressed and base64-encoded (verified in the sandbox: zlib, base64 and exec work).
    base64 also keeps braces out of the code, so hooks with dict literals need no escaping."""
    import base64
    import zlib
    blob = base64.b64encode(zlib.compress(tool_module(tool, spec, pre_src, post_src).encode("utf-8"), 9)).decode("ascii")
    code = ("import zlib as _z, base64 as _b\n"
            "_g = dict(params=params, vars=vars, _fb_tool=" + repr(tool) + ")\n"
            "exec(_z.decompress(_b.b64decode('" + blob + "')).decode('utf-8'), _g)\n"
            "return _g.get('_fb_out')\n")
    assert "{{" not in code and "}}" not in code, "fake bank code must not contain double braces"
    return code


def run_code_locally(code, params, vars):
    """Run a code_exec snippet the way the sandbox does: wrapped in a function, `params` and `vars` as globals.
    Returns ("success", data) or ("error", message)."""
    body = "\n".join("    " + ln for ln in code.splitlines())
    src = "def __user_main__():\n" + body + "\n    return None\n"
    g = {"params": dict(params or {}), "vars": vars, "__name__": "__fakebank__"}
    try:
        exec(compile(src, "<code_exec>", "exec"), g)
        return "success", g["__user_main__"]()
    except Exception as e:  # mirrors the platform: status error, status_code 500
        return "error", "%s: %s" % (type(e).__name__, e)


_LOCAL_NS = None
LAST_STATS = {}  # {"converted": n, "sink": n} of the last to_test_mode call


def fakebank_call(tool, args, vars, spec):
    """Run the fake bank logic locally (no hooks). Returns (status, body); state lives in vars[state_var]."""
    return _local_ns()["_fb_run"](tool, args, vars, spec)


WLOG_PREFIX = "fakebank_w_"


def merge_write_log(state, entries, order=None):
    """Put back writes that a parallel tool call recorded but a sibling call overwrote in the state.

    state: stored or working state; entries: compact call entries [tool, status, ok, error, args] from the
    `fakebank_w_*` variables; order: tool names in completion order (from function_call_complete events), used
    to place the restored calls. Returns (ledger, restored_entries)."""
    led = ledger_from_state(state)
    have = {}
    for c in led["calls"]:
        k = json.dumps(_local_ns()["_fb_centry"](c), sort_keys=True)
        have[k] = have.get(k, 0) + 1
    restored = []
    for e in entries or []:
        k = json.dumps(e, sort_keys=True)
        if have.get(k):
            have[k] -= 1
            continue
        restored.append(e)
    for e in restored:
        tool, status, ok, err = e[0], e[1], bool(e[2]), e[3]
        args = e[4] if len(e) > 4 else {}
        res = {"ok": ok}
        if err:
            res["error"] = err
        led["calls"].append({"n": 0, "tool": tool, "args": args, "status": status, "result": res, "restored": True})
        if not ok:
            continue
        g = lambda *names: next((args.get(n) for n in names if args.get(n) is not None), None)
        if tool == "create_promise":
            led["promises"].append({"promise_id": "pr%d" % (len(led["promises"]) + 1), "product_id": g("product_id"),
                                    "type": None, "amount": g("amount"), "date": g("date")})
        elif tool == "add_note":
            led["notes"].append({"product_id": g("product_id"), "reason": g("reason"), "text": g("text") or ""})
        elif tool == "schedule_callback":
            led["callbacks"].append({"date": g("date"), "time": g("time")})
        elif tool == "transfer_to_agent":
            led["transfers"].append({"queue": g("queue")})
        elif tool == "close_call":
            led["outcome"], led["reason"] = g("outcome"), g("reason")
    if restored and order:
        pos, seen = {}, {}
        for i, t in enumerate(order):
            seen[t] = seen.get(t, 0) + 1
            pos[(t, seen[t])] = i
        cnt = {}
        keyed = []
        for j, c in enumerate(led["calls"]):
            cnt[c["tool"]] = cnt.get(c["tool"], 0) + 1
            keyed.append((pos.get((c["tool"], cnt[c["tool"]])), j, c))
        if all(k[0] is not None for k in keyed):  # otherwise keep state order + restored calls at the end
            led["calls"] = [c for _, _, c in sorted(keyed, key=lambda x: (x[0], x[1]))]
    for i, c in enumerate(led["calls"]):
        c["n"] = i + 1
    return led, restored


def _local_ns():
    global _LOCAL_NS
    if _LOCAL_NS is None:
        _LOCAL_NS = {}
        exec(compile(FAKEBANK_SRC, "<fakebank>", "exec"), _LOCAL_NS)
    return _LOCAL_NS


def ledger_from_state(state):
    """The fake bank's state (compact stored form or working form) in the shape of the bank mock's ledger
    (what tools/score.py reads). Calls carry tool, status, ok/error, and args for write tools only."""
    if isinstance(state, str):
        state = json.loads(state) if state.strip() else {}
    st = _local_ns()["_fb_unpack"](copy.deepcopy(state or {}))
    keys = ("verified", "wrong", "hinted", "promises", "notes", "callbacks", "transfers", "outcome", "reason",
            "failures", "calls")
    return {k: st.get(k) for k in keys}


# ---------------------------------------------------------------------------------------------------------------
# workflow transform

def _bank_tool_name(t, names):
    fn = (t or {}).get("function") or {}
    if fn.get("handler") != "__function__:api_call":
        return None
    name = fn.get("name")
    url = str((((fn.get("parameters") or {}).get("properties") or {}).get("url") or {}).get("default") or "")
    path = re.sub(r"[?#].*$", "", url).rstrip("/")
    for n, info in names.items():
        p = (info.get("path") or "").rstrip("/")
        if p and path.endswith(p):
            return n
    return name if name in names else None


def _code_params(body_props, body_required, code):
    props = {}
    for f, fs in (body_props or {}).items():
        props[f] = copy.deepcopy(fs)
    props["code"] = {"type": "string", "default": code}
    props["timeout_ms"] = {"type": "number", "default": 10000}
    return {"type": "object", "properties": props, "required": [r for r in (body_required or []) if r in props]}


def _convert(t, tool, spec, sink=None):
    """One api_call tool -> code_exec fake (or, for end_of_call lifecycle tools with a sink, the sink api_call)."""
    t = copy.deepcopy(t)
    fn = t["function"]
    pp = (fn.get("parameters") or {}).get("properties") or {}
    body = pp.get("body") or {}
    bprops, breq = body.get("properties") or {}, body.get("required") or []
    life = ((t.get("config") or {}).get("alwaysRunsAt") or [])
    if sink and "end_of_call" in life:
        pp["url"] = {"type": "string", "default": sink["base_url"].rstrip("/") + "/" + tool}
        if "customer_id" in bprops:
            bprops["customer_id"] = dict(bprops["customer_id"], default="{{" + sink.get("var", SINK_VAR) + "}}")
        return t
    code = tool_code(tool, spec, t.get("pre_process_code"), t.get("post_process_code"))
    fn["handler"] = "__function__:code_exec"
    fn["parameters"] = _code_params(bprops, breq, code)
    t.pop("pre_process_code", None)
    t.pop("post_process_code", None)
    return t


def to_test_mode(workflow, openapi, scenario_var=SCENARIO_VAR, state_var=STATE_VAR, sink=None):
    """Return a copy of `workflow` whose bank api_call tools (node tools, pre_actions, workflow-level lifecycle
    tools) run the fake bank. Tool names, parameters, response_mapping and config are kept, so result variables
    (`<tool>_response.data.*`, counters, mapped vars) are the same as with the real bank.

    sink: optional {"base_url": <bank url>, "var": <call variable with a sink customer id>}; end_of_call lifecycle
    tools then stay api_calls to that bank (their result is not observable otherwise)."""
    spec = spec_from_openapi(openapi, scenario_var, state_var)
    names = spec["tools"]
    wf = copy.deepcopy(workflow)
    count = {"converted": 0, "sink": 0}

    def conv_list(lst):
        out = []
        for t in lst or []:
            tool = _bank_tool_name(t, names)
            if tool:
                nt = _convert(t, tool, spec, sink)
                count["sink" if nt["function"]["handler"] == "__function__:api_call" else "converted"] += 1
                out.append(nt)
            else:
                out.append(t)
        return out

    for node in (wf.get("nodes") or {}).values():
        if isinstance(node, dict):
            for key in ("tools", "pre_actions"):
                if node.get(key):
                    node[key] = conv_list(node[key])
    for node in (wf.get("global_nodes") or {}).values():
        if isinstance(node, dict):
            for key in ("tools", "pre_actions"):
                if node.get(key):
                    node[key] = conv_list(node[key])
    if wf.get("tools"):
        wf["tools"] = conv_list(wf["tools"])
    tv = wf.get("test_variables")
    if isinstance(tv, dict):
        tv.setdefault(scenario_var, "{}")
    wf.pop("agent_config", None)
    LAST_STATS.clear(); LAST_STATS.update(count)
    return wf


def scenario_for_case(case, extra=None):
    """Scenario JSON for one case in public-test-set.json format (identity, products, faults) plus extras
    (max_wrong, close_reasons) taken from the caller (facts file, defaults of the runner)."""
    sc = {"identity": case.get("identity") or {}, "products": case.get("products") or [],
          "faults": case.get("faults") or []}
    for k, v in (extra or {}).items():
        if v is not None:
            sc[k] = v
    for k in ("max_wrong", "close_reasons"):
        if k in case:
            sc[k] = case[k]
    return sc
