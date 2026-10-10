"""policy.json contract v1: validation, defaults and normalisation.

    from builder.policy_schema import validate, normalise
    errors = validate(policy)          # list of human-readable problems, [] when usable
    policy = normalise(policy)         # a copy with every optional field filled and shapes made uniform

The contract (shared by the extractor and the generator). All top-level keys are required unless noted;
every section may carry "notes": [verbatim brief sentences].

  source       {briefs:[file names], overrides_applied:[text]}
  variables    [{name, meaning, example?}]                      call variables
  clock        {now_variable, timezone|null, date_format}
  calendar     {weekend:[ISO weekday ints], holidays:["YYYY-MM-DD"], roll_rule:"next_business_day"|"previous_business_day"|"none"}
  opening      {text, disclosures:[...], forbidden_before_verification:[words], line?}
  third_party  {rules:[text], cases:[{trigger, say, action:"callback"|"end"|..., outcome, reason}]}
  identity     {questions:[{order, tool_field, ask, answer_format:"date"|"digits"|"text",
                            normalise:"dmy_to_iso"|"digits_only"|"none"}],
                max_wrong:int, wrong_counts_rules:[text], fail:{outcome, reason, say}}
  products     {max_count:int, types:{<type>:{speak_as, floor_field, full_field}}, order:"as_returned",
                id_field?, type_field?, days_late_field?, list_field?}   speak_as may use {field} placeholders
  bands        [{name, min_days_late, max_days_late,
                 deadline:{kind:"nth_business_day"|"calendar_days"|"fixed_date"|"end_of_month", n:int,
                           counted_from:"today"|"tomorrow"|"including_today"},
                 attempts:[{n, amount:"full"|"floor"|<expr>, date:"deadline"|"end_of_week_business_day"|<expr>, text}]}]
  amounts      {min:"floor", max:"full", below_min:"reject", ask_open_amount:bool, rules:[text]}
  dates        {customer_date_after_deadline:text, vague_date:text, rules:[text],
                next_weekday?:"following_week"|"next_occurrence"}
  promise      {readback:text, confirm_required:bool, retry_on_failure:int, success_say:text, failure_say:text,
                per_product:bool}
  callback     {days:["today","next_business_day"|"tomorrow"|"business_days:N"], start:"HH:MM", end:"HH:MM",
                end_inclusive:bool, must_be_after_now:bool, max_asks?:int}
  triggers     [{name, detect, action:"transfer"|"note"|"callback"|"close"|"continue"|"say", queue, outcome, reason,
                 note_reason, say, precedence:int}]
  notes        {tool_reasons:[enum], when:text, forbidden_on_transfer:bool, enabled?:bool}
  outcomes     {table:[{outcome, reason|null, when}], precedence:[text], multi_product:text}
  phrases      {<key>: verbatim text}
  tools        {<tool name>:{fields:{<field>:{type, enum|null, required, description?}}, notes:text,
                 path?, response?:{<field>:{type, enum}}}}  plus optional "base_url" in tools or in api.base_url
  rules        [{id, text}]

Optional extensions (filled by normalise() when absent, may be given explicitly by the extractor):
  identity     max_wrong_scope "total"|"per_question"; questions[].tool_format (the tool's answer format, text) ->
               derived questions[].date_format ("DD-MM-YYYY"...; "" = ISO) and text_ops (["upper", "space_before_last:3"...]);
               a sentence in questions[].normalise is mapped to one of NORMALISERS
  products     types[t].floor_expr: amount expression over the product's fields, e.g. "min(50, <field>)"; wins over floor_field
  callback     per_weekday {"<ISO weekday 1-7>": {start, end}} ({} = no window that day); other days use start/end
  triggers     [].condition {over: all_products_sum|any_product|every_product|max|min|product_count, field, op, value}:
               decided in code after get_account
  language     "English"
  api          {base_url}
  closing      {promise, id_fail, callback, no_callback, refused, service_error: {outcome, reason},
                promise_mixed: {outcome, reason} (a promise recorded and some product noted), promise_mixed_except:
                [note reasons that do not count for it], note_severity: [note reasons, most serious first, for a
                no-promise close with mixed notes; empty = first product in get_account order],
                transfer: {<queue>: {outcome, reason}}, note_map: {<note reason>: {outcome, reason}},
                transfer_overrides_promise: bool, no_reason_outcomes: [outcome]}

Nothing here knows a brief: defaults are structural, derived values come from the policy itself.
"""
import copy
import re

REQUIRED_KEYS = ["source", "variables", "clock", "calendar", "opening", "third_party", "identity", "products",
                 "bands", "amounts", "dates", "promise", "callback", "triggers", "notes", "outcomes", "phrases",
                 "tools", "rules"]
ROLL_RULES = ["next_business_day", "previous_business_day", "none"]
DEADLINE_KINDS = ["nth_business_day", "calendar_days", "fixed_date", "end_of_month"]
COUNTED_FROM = ["today", "tomorrow", "including_today"]
COUNTED_FROM_ALIASES = {"today_inclusive": "including_today", "inclusive": "including_today",
                        "today_included": "including_today", "after_today": "today", "today_exclusive": "today"}
ANSWER_FORMATS = ["date", "digits", "text"]
NORMALISERS = ["dmy_to_iso", "mdy_to_iso", "digits_only", "none"]
TRIGGER_ACTIONS = ["transfer", "note", "callback", "close", "continue", "say"]
NEXT_WEEKDAY = ["following_week", "next_occurrence", ""]
TOOL_ROLES = ["verify_identity", "get_account", "create_promise", "add_note", "schedule_callback",
              "transfer_to_agent", "close_call"]

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM = re.compile(r"^\d{2}:\d{2}$")


# ----------------------------------------------------------------------------------------------- helpers

def _s(x):
    """Text or ''. Never None."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    return str(x)


def _list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, (tuple, set)):
        return list(x)
    return [x]


def _dict(x):
    return x if isinstance(x, dict) else {}


def _int(x, default=None):
    try:
        if isinstance(x, bool):
            return default
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _bool(x, default=False):
    if isinstance(x, bool):
        return x
    if x is None or x == "":
        return default
    return _s(x).lower() in ("true", "yes", "1", "y")


def _iso(x):
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", _s(x))
    if not m:
        return ""
    return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _hhmm(x):
    m = re.match(r"^\s*(\d{1,2})(?:[:.h](\d{2}))?\s*$", _s(x))
    if not m:
        return _s(x)
    return "%02d:%02d" % (int(m.group(1)), int(m.group(2) or 0))


def _texts(x):
    """List of non-empty strings (dicts with a 'text' key are accepted)."""
    out = []
    for v in _list(x):
        if isinstance(v, dict):
            v = v.get("text") or v.get("rule") or ""
        v = _s(v)
        if v:
            out.append(v)
    return out


def split_reasons(x):
    """'a, b / c' -> ['a', 'b', 'c']; None/'none'/'-' -> []."""
    if isinstance(x, list):
        out = []
        for v in x:
            out.extend(split_reasons(v))
        return out
    s = _s(x)
    if not s or s.lower() in ("none", "null", "-", "n/a", "—"):
        return []
    parts = re.split(r"\s*(?:,|/|;|\|| or )\s*", s)
    return [p.strip().strip("`'\"") for p in parts if p.strip().strip("`'\"")]


def tool_field(policy, tool, field):
    return _dict(_dict(_dict(_dict(policy.get("tools")).get(tool)).get("fields")).get(field))


def tool_enum(policy, tool, field):
    return [_s(v) for v in _list(tool_field(policy, tool, field).get("enum")) if _s(v)]


def find_tool_field(policy, tool, *keywords):
    """First request field of a tool whose name contains one of the keywords."""
    fields = _dict(_dict(_dict(policy.get("tools")).get(tool)).get("fields"))
    for kw in keywords:
        for f in fields:
            if kw in f.lower():
                return f
    return ""


# ----------------------------------------------------------------------------------------------- extensions

WEEKDAY_KEYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
CONDITION_OPS = [">", ">=", "<", "<=", "==", "!="]
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


def norm_kind(text, fmt):
    """identity question 'normalise' (a keyword or a sentence from the brief) -> one of NORMALISERS."""
    t = _s(text).lower().strip()
    if t in NORMALISERS:
        return t
    if not t:
        return {"digits": "digits_only"}.get(fmt, "none")
    if fmt == "date" or "date" in t:
        if re.search(r"\bmonth\b.{0,15}\bday\b|\bmm\W?dd\b", t):
            return "mdy_to_iso"
        if re.search(r"\bday\b.{0,15}\bmonth\b|\bdd\W?mm\b", t):
            return "dmy_to_iso"
        return "none"
    if fmt == "digits" or re.search(r"\bdigits?\b", t):
        return "digits_only"
    return "none"


def date_pattern(text):
    """'DD-MM-YYYY' (or MM/DD/YYYY, YYYYMMDD, DD.MM.YY...) named in a format text -> that pattern; '' for ISO or none."""
    m = re.search(r"\b(DD|MM|YYYY|YY)([-/. ]?)(DD|MM|YYYY|YY)\2(DD|MM|YYYY|YY)\b", _s(text), re.I)
    if not m:
        return ""
    pat = m.group(0).upper()
    parts = {m.group(1).upper()[:2], m.group(3).upper()[:2], m.group(4).upper()[:2]}
    if parts != {"DD", "MM", "YY"} or pat == "YYYY-MM-DD":
        return ""
    return pat


def text_ops(text):
    """Text conversions a tool format describes ('capitals, one space before the last three characters')."""
    t = _s(text).lower()
    ops = []
    if re.search(r"\b(upper ?case|capitals?|capital letters|all caps)\b", t):
        ops.append("upper")
    elif re.search(r"\blower ?case\b", t):
        ops.append("lower")
    if re.search(r"\b(no|without) spaces?\b", t):
        ops.append("nospace")
    m = re.search(r"space before the last (\d+|" + "|".join(NUMBER_WORDS) + r")\b", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1)]
        ops.append("space_before_last:%d" % n)
    return ops


def per_weekday(x):
    """callback.per_weekday -> {"<ISO weekday 1-7>": {start, end}} ({} = that day has no window)."""
    out = {}
    for k, v in _dict(x).items():
        key = _int(k)
        if key is None:
            key = WEEKDAY_KEYS.get(_s(k).lower()[:3])
        if not key or not 1 <= key <= 7:
            continue
        v = _dict(v)
        out[str(key)] = {"start": _hhmm(v.get("start")), "end": _hhmm(v.get("end"))} if v.get("start") and v.get("end") else {}
    return out


def condition(x):
    """trigger.condition -> {over, field, op, value} or None."""
    c = _dict(x)
    if not c:
        return None
    op = _s(c.get("op")).strip() or ">"
    op = {"=": "==", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "==", "ne": "!="}.get(op.lower(), op)
    try:
        value = float(c.get("value"))
    except (TypeError, ValueError):
        value = None
    return {"over": _s(c.get("over")).lower() or "all_products_sum", "field": _s(c.get("field")), "op": op, "value": value}


# ----------------------------------------------------------------------------------------------- normalise

def normalise(policy):
    """Return a normalised deep copy. Never raises on odd shapes: unknown values are kept, missing ones get
    neutral defaults."""
    p = copy.deepcopy(policy) if isinstance(policy, dict) else {}

    src = _dict(p.get("source"))
    p["source"] = {**src, "briefs": _texts(src.get("briefs")), "overrides_applied": _texts(src.get("overrides_applied"))}

    variables = []
    for v in _list(p.get("variables")):
        if isinstance(v, str):
            v = {"name": v}
        if isinstance(v, dict) and _s(v.get("name")):
            nv = {**v, "name": _s(v.get("name")).strip("{} `"), "meaning": _s(v.get("meaning"))}
            variables.append(nv)
    p["variables"] = variables

    clock = _dict(p.get("clock"))
    now_var = _s(clock.get("now_variable")).strip("{} `")
    if not now_var:
        for v in variables:
            if "now" == v["name"].lower() or re.search(r"current (date|time)", v["meaning"].lower()):
                now_var = v["name"]
                break
    p["clock"] = {**clock, "now_variable": now_var or "now", "timezone": _s(clock.get("timezone")),
                  "date_format": _s(clock.get("date_format"))}

    cal = _dict(p.get("calendar"))
    weekend = [w for w in (_int(x) for x in _list(cal.get("weekend"))) if w and 1 <= w <= 7] if "weekend" in cal else [6, 7]
    hol = sorted(set(h for h in (_iso(x) for x in _list(cal.get("holidays"))) if h))
    p["calendar"] = {**cal, "weekend": sorted(set(weekend)), "holidays": hol,
                     "roll_rule": _s(cal.get("roll_rule")) or "next_business_day"}

    op = _dict(p.get("opening"))
    p["opening"] = {**op, "text": _s(op.get("text")), "disclosures": _texts(op.get("disclosures")),
                    "forbidden_before_verification": _texts(op.get("forbidden_before_verification")),
                    "line": _s(op.get("line"))}

    tp = _dict(p.get("third_party"))
    cases = []
    for c in _list(tp.get("cases")):
        if isinstance(c, dict):
            cases.append({**c, "trigger": _s(c.get("trigger")), "say": _s(c.get("say")),
                          "action": _s(c.get("action")).lower() or "end", "outcome": _s(c.get("outcome")),
                          "reason": _s(c.get("reason"))})
    if not cases and (_s(tp.get("outcome")) or _s(tp.get("reason"))):
        cases.append({"trigger": "someone other than the customer answers", "say": "", "action": "end",
                      "outcome": _s(tp.get("outcome")), "reason": _s(tp.get("reason"))})
    p["third_party"] = {**tp, "rules": _texts(tp.get("rules")), "cases": cases}

    idn = _dict(p.get("identity"))
    qs = []
    for i, q in enumerate(_list(idn.get("questions"))):
        if not isinstance(q, dict):
            continue
        fmt = _s(q.get("answer_format")).lower() or "text"
        norm_text = _s(q.get("normalise"))
        norm = norm_kind(norm_text, fmt)
        tf = _s(q.get("tool_format"))
        qs.append({**q, "order": _int(q.get("order"), i + 1), "tool_field": _s(q.get("tool_field")),
                   "ask": _s(q.get("ask")), "answer_format": fmt, "normalise": norm, "tool_format": tf,
                   "date_format": date_pattern(tf) or (date_pattern(norm_text) if norm_text.lower() not in NORMALISERS else ""),
                   "text_ops": text_ops(tf + " " + (norm_text if norm_text.lower() not in NORMALISERS else ""))
                   if fmt == "text" else []})
    qs.sort(key=lambda q: q["order"])
    fail = _dict(idn.get("fail"))
    scope = _s(idn.get("max_wrong_scope")).lower().replace("-", "_").replace(" ", "_")
    p["identity"] = {**idn, "questions": qs, "max_wrong": _int(idn.get("max_wrong")),
                     "max_wrong_scope": "per_question" if scope.startswith("per") or "question" in scope else "total",
                     "wrong_counts_rules": _texts(idn.get("wrong_counts_rules")),
                     "fail": {**fail, "outcome": _s(fail.get("outcome")), "reason": _s(fail.get("reason")),
                              "say": _s(fail.get("say"))}}

    pr = _dict(p.get("products"))
    types = {}
    for k, t in _dict(pr.get("types")).items():
        t = _dict(t)
        types[_s(k)] = {**t, "speak_as": _s(t.get("speak_as")) or _s(k).replace("_", " "),
                        "floor_field": _s(t.get("floor_field")), "full_field": _s(t.get("full_field")),
                        "floor_expr": _s(t.get("floor_expr"))}
    id_field = _s(pr.get("id_field")) or find_tool_field(p, "create_promise", "product") or "product_id"
    p["products"] = {**pr, "max_count": _int(pr.get("max_count"), 1) or 1, "types": types,
                     "order": _s(pr.get("order")) or "as_returned", "id_field": id_field,
                     "type_field": _s(pr.get("type_field")) or "type",
                     "days_late_field": _s(pr.get("days_late_field")) or "days_late",
                     "list_field": _s(pr.get("list_field")) or "products"}

    bands = []
    for i, b in enumerate(_list(p.get("bands"))):
        if not isinstance(b, dict):
            continue
        dl = _dict(b.get("deadline"))
        atts = []
        for j, a in enumerate(_list(b.get("attempts"))):
            if isinstance(a, dict):
                atts.append({**a, "n": _int(a.get("n"), j + 1), "amount": _s(a.get("amount")),
                             "date": _s(a.get("date")), "text": _s(a.get("text"))})
        atts.sort(key=lambda a: a["n"])
        bands.append({**b, "name": _s(b.get("name")) or "band %d" % (i + 1),
                      "min_days_late": _int(b.get("min_days_late")), "max_days_late": _int(b.get("max_days_late")),
                      "deadline": {**dl, "kind": _s(dl.get("kind")) or "nth_business_day", "n": _int(dl.get("n"), 0),
                                   "counted_from": COUNTED_FROM_ALIASES.get(_s(dl.get("counted_from")).lower(),
                                                                            _s(dl.get("counted_from")).lower() or "today")},
                      "attempts": atts})
    bands.sort(key=lambda b: (b["min_days_late"] if b["min_days_late"] is not None else -10 ** 9))
    p["bands"] = bands

    am = _dict(p.get("amounts"))
    p["amounts"] = {**am, "min": _s(am.get("min")) or "floor", "max": _s(am.get("max")) or "full",
                    "below_min": _s(am.get("below_min")) or "reject",
                    "ask_open_amount": _bool(am.get("ask_open_amount"), False), "rules": _texts(am.get("rules"))}

    dt = _dict(p.get("dates"))
    nw = _s(dt.get("next_weekday"))
    p["dates"] = {**dt, "customer_date_after_deadline": _s(dt.get("customer_date_after_deadline")),
                  "vague_date": _s(dt.get("vague_date")), "rules": _texts(dt.get("rules")),
                  "next_weekday": nw if nw in NEXT_WEEKDAY else ""}

    pm = _dict(p.get("promise"))
    p["promise"] = {**pm, "readback": _s(pm.get("readback")), "confirm_required": _bool(pm.get("confirm_required"), True),
                    "retry_on_failure": _int(pm.get("retry_on_failure"), 1),
                    "success_say": _s(pm.get("success_say")), "failure_say": _s(pm.get("failure_say")),
                    "per_product": _bool(pm.get("per_product"), True)}

    cb = _dict(p.get("callback"))
    p["callback"] = {**cb, "days": [_s(d) for d in _list(cb.get("days")) if _s(d)], "start": _hhmm(cb.get("start")),
                     "end": _hhmm(cb.get("end")), "end_inclusive": _bool(cb.get("end_inclusive"), True),
                     "must_be_after_now": _bool(cb.get("must_be_after_now"), True),
                     "max_asks": _int(cb.get("max_asks"), 2), "per_weekday": per_weekday(cb.get("per_weekday"))}

    trs = []
    for i, t in enumerate(_list(p.get("triggers"))):
        if isinstance(t, dict):
            trs.append({**t, "name": _s(t.get("name")) or "trigger %d" % (i + 1), "detect": _s(t.get("detect")),
                        "action": _s(t.get("action")).lower() or "say", "queue": _s(t.get("queue")),
                        "outcome": _s(t.get("outcome")), "reason": _s(t.get("reason")),
                        "note_reason": _s(t.get("note_reason")), "say": _s(t.get("say")),
                        "precedence": _int(t.get("precedence"), 100 + i), "condition": condition(t.get("condition"))})
    trs.sort(key=lambda t: t["precedence"])
    p["triggers"] = trs

    nt = _dict(p.get("notes"))
    reasons = [_s(r) for r in _list(nt.get("tool_reasons")) if _s(r)] or tool_enum(p, "add_note", "reason")
    enabled = nt.get("enabled")
    if enabled is None or enabled == "":
        when = _s(nt.get("when")).lower()
        # off only when the text says notes are never used; "no notes on a transfer" is an exception, not a ban
        enabled = bool(reasons) and bool(when) and not re.search(
            r"\bnever (use|uses|call|calls|write|writes|add|adds|need|needs)\b|\b(is|are) (never|not) used\b|\bnot used\b", when)
    # notes that apply only to customers with several products ("multi-product customers", "more than one product")
    mp = _int(nt.get("min_products"))
    if mp is None:
        add_note = _dict(_dict(p.get("tools")).get("add_note"))
        text = " ".join([_s(nt.get("when"))] + _texts(nt.get("notes")) + [_s(add_note.get("notes")), _s(add_note.get("summary"))]).lower()
        if re.search(r"\b(with|for|also with|applies to|even with) (one|a single|1) product", text):
            mp = 1
        elif re.search(r"multi-?product|more than one product|several products|two or more products|multiple products", text):
            mp = 2
        else:
            mp = 1
    p["notes"] = {**nt, "tool_reasons": reasons, "when": _s(nt.get("when")),
                  "forbidden_on_transfer": _bool(nt.get("forbidden_on_transfer"), False), "enabled": _bool(enabled),
                  "min_products": max(1, mp)}

    oc = _dict(p.get("outcomes"))
    table = []
    for r in _list(oc.get("table")):
        if not isinstance(r, dict) or not _s(r.get("outcome")):
            continue
        rs = split_reasons(r.get("reason"))
        if not rs:
            table.append({**r, "outcome": _s(r.get("outcome")), "reason": "", "when": _s(r.get("when"))})
        for x in rs:
            table.append({**r, "outcome": _s(r.get("outcome")), "reason": x, "when": _s(r.get("when"))})
    p["outcomes"] = {**oc, "table": table, "precedence": _texts(oc.get("precedence")),
                     "multi_product": _s(oc.get("multi_product"))}

    ph = _dict(p.get("phrases"))
    p["phrases"] = {_s(k): _s(v) for k, v in ph.items() if _s(k) and k != "notes" and not isinstance(v, (list, dict))}
    if ph.get("notes"):
        p["phrases"]["notes"] = _texts(ph.get("notes"))

    tools = {}
    base_url = ""
    for name, t in _dict(p.get("tools")).items():
        if name in ("base_url", "server", "servers"):
            base_url = _s(t if not isinstance(t, list) else (t[0] if t else ""))
            continue
        t = _dict(t)
        fields = {}
        for f, spec in _dict(t.get("fields")).items():
            spec = _dict(spec)
            fields[_s(f)] = {**spec, "type": _s(spec.get("type")) or "string",
                             "enum": [_s(e) for e in _list(spec.get("enum")) if _s(e)] or None,
                             "required": _bool(spec.get("required"), False)}
        resp = {}
        for f, spec in _dict(t.get("response") or t.get("response_fields")).items():
            spec = _dict(spec)
            resp[_s(f)] = {**spec, "type": _s(spec.get("type")),
                           "enum": [_s(e) for e in _list(spec.get("enum")) if _s(e)] or None}
        tools[_s(name)] = {**t, "fields": fields, "notes": _s(t.get("notes")), "path": _s(t.get("path")),
                           "url": _s(t.get("url")), "method": _s(t.get("method")).upper() or "POST", "response": resp}
    p["tools"] = tools
    api = _dict(p.get("api"))
    servers = _texts(p["source"].get("api_servers") or _dict(policy).get("servers"))
    p["api"] = {**api, "base_url": (_s(api.get("base_url")) or base_url or (servers[0] if servers else "")).rstrip("/")}

    _derive_product_fields(p)

    rules = []
    for i, r in enumerate(_list(p.get("rules"))):
        if isinstance(r, dict):
            if _s(r.get("text")):
                rules.append({**r, "id": _s(r.get("id")) or str(i + 1), "text": _s(r.get("text"))})
        elif _s(r):
            rules.append({"id": str(i + 1), "text": _s(r)})
    p["rules"] = rules

    p["language"] = _s(p.get("language")) or "English"
    p["closing"] = derive_closing(p)
    return p


def _derive_product_fields(p):
    """products.list_field / id_field / type_field / days_late_field from get_account's response schema, when the
    policy's values are not fields of the returned items."""
    pr = p["products"]
    acc = next((t for n, t in p["tools"].items() if n == "get_account"), None) or \
        next((t for n, t in p["tools"].items() if "account" in n.lower()), None) or {}
    resp = acc.get("response") or {}
    if not resp:
        return
    if pr["list_field"] not in resp or _dict(resp.get(pr["list_field"])).get("type") != "array":
        arrays = [f for f, sp in resp.items() if _dict(sp).get("type") == "array"]
        if arrays:
            pr["list_field"] = arrays[0]
    items = _dict(_dict(_dict(resp.get(pr["list_field"])).get("items")).get("properties"))
    if not items:
        return
    calls = set(v["name"] for v in p["variables"])
    promise_fields = _dict(_dict(p["tools"].get("create_promise")).get("fields"))
    if pr["id_field"] not in items:
        shared = [f for f in items if f in promise_fields and f not in calls]
        ids = [f for f in items if f.lower().endswith("id")]
        pr["id_field"] = (shared or ids or [pr["id_field"]])[0]
    if pr["type_field"] not in items:
        enums = [f for f, sp in items.items() if _dict(sp).get("enum")]
        if enums:
            pr["type_field"] = enums[0]
    if pr["days_late_field"] not in items:
        days = [f for f, sp in items.items() if ("day" in f.lower() or "late" in f.lower())
                and _dict(sp).get("type") in ("integer", "number")]
        if days:
            pr["days_late_field"] = days[0]


def _pair(outcome, reason=""):
    return {"outcome": _s(outcome), "reason": _s(reason)}


def derive_closing(p):
    """Fill policy['closing'] (see module doc) from explicit values, then from the outcome table, triggers,
    identity and third-party cases. Works on a normalised policy."""
    given = _dict(p.get("closing"))
    table = p["outcomes"]["table"]
    reasons_of = {}
    for r in table:
        reasons_of.setdefault(r["outcome"], [])
        if r["reason"]:
            reasons_of[r["outcome"]].append(r["reason"])
    no_reason = [o for o, rs in reasons_of.items() if not rs]
    id_fail = _pair(p["identity"]["fail"].get("outcome"), p["identity"]["fail"].get("reason"))

    def explicit(key):
        v = _dict(given.get(key))
        return _pair(v.get("outcome"), v.get("reason")) if _s(v.get("outcome")) else None

    promise = explicit("promise")
    if not promise:
        cands = [r for r in table if not r["reason"]]
        # rows that talk about notes as well describe a mixed case; the plain promise row comes first
        cands.sort(key=lambda r: 1 if re.search(r"\bnoted?\b", r["when"].lower()) else 0)
        pick = None
        for r in cands:
            if "promise" in r["outcome"].lower() and not r["outcome"].lower().startswith("no"):
                pick = r
                break
        if not pick:
            for r in cands:
                if "promise" in r["when"].lower() and "record" in r["when"].lower():
                    pick = r
                    break
        if not pick:
            pe = tool_enum(p, "close_call", "outcome")
            for o in pe:
                if "promise" in o.lower() and not o.lower().startswith("no"):
                    pick = {"outcome": o}
                    break
        promise = _pair(pick["outcome"]) if pick else _pair("")

    def by_reason_word(*words):
        for r in table:
            if r["reason"] and any(w in r["reason"].lower() for w in words) and r["outcome"] != id_fail["outcome"]:
                return _pair(r["outcome"], r["reason"])
        return None

    refused = explicit("refused")
    if not refused:
        for t in p["triggers"]:
            if "refus" in (t["name"] + " " + t["reason"]).lower() and t["outcome"] and t["outcome"] != id_fail["outcome"]:
                refused = _pair(t["outcome"], t["reason"])
                break
    refused = refused or by_reason_word("refus", "declin") or _pair("")
    service_error = explicit("service_error") or by_reason_word("error", "fail", "technical") or _pair("")

    callback = explicit("callback")
    if not callback:
        for c in p["third_party"]["cases"]:
            if c["action"] == "callback" and c["outcome"]:
                callback = _pair(c["outcome"], c["reason"])
                break
    callback = callback or _pair("")

    no_callback = explicit("no_callback")
    if not no_callback:
        for c in p["third_party"]["cases"]:
            if c["outcome"] and c["action"] != "callback" and (
                    re.search(r"outside|window|again|no.?callback", (c["trigger"] + " " + c["action"] + " " + c["reason"]).lower())):
                no_callback = _pair(c["outcome"], c["reason"])
                break
    no_callback = no_callback or _pair("")

    transfer = {}
    for q, v in _dict(given.get("transfer")).items():
        if isinstance(v, dict) and _s(v.get("outcome")):
            transfer[_s(q)] = _pair(v.get("outcome"), v.get("reason"))
    for t in p["triggers"]:
        if t["action"] == "transfer" and t["queue"] and t["outcome"] and t["queue"] not in transfer:
            transfer[t["queue"]] = _pair(t["outcome"], t["reason"])

    note_map = {}
    for r, v in _dict(given.get("note_map")).items():
        if isinstance(v, dict) and _s(v.get("outcome")):
            note_map[_s(r)] = _pair(v.get("outcome"), v.get("reason"))
    transfer_outcomes = set(v["outcome"] for v in transfer.values())
    # explicit mappings in the text: "note `refused` matches `declined`", "service_error maps to system_error"
    all_reasons = set(reasons_of.get(refused["outcome"], [])) | set(r["reason"] for r in table if r["reason"])
    texts = [r["text"] for r in p["rules"]] + p["outcomes"]["precedence"] + [p["outcomes"]["multi_product"], p["notes"]["when"]]
    for t in texts:
        for m in re.finditer(r"`?([a-z_]+)`?\s+(?:matches|maps to|corresponds to|becomes|->|→)\s+`?([a-z_]+)`?", t.lower()):
            a, b = m.group(1), m.group(2)
            if a in p["notes"]["tool_reasons"] and b in all_reasons and a not in note_map:
                row = next((x for x in table if x["reason"] == b and x["outcome"] not in transfer_outcomes), None)
                if row:
                    note_map[a] = _pair(row["outcome"], b)
    for r in p["notes"]["tool_reasons"]:
        if r in note_map:
            continue
        rows = [x for x in table if x["reason"] == r and x["outcome"] != id_fail["outcome"]]
        # a note means the product ended without a promise: prefer the no-promise row over a transfer row
        rows.sort(key=lambda x: (0 if x["outcome"] == refused["outcome"] else 1 if x["outcome"] not in transfer_outcomes else 2))
        row = rows[0] if rows else None
        # a reason-less row whose condition names this note ("every product noted x") and is not about a
        # recorded promise ("at least one promise ... other than x" is the mixed-promise row)
        whole = next((x for x in table if not x["reason"] and x["outcome"] != promise["outcome"]
                      and not (re.search(r"\bpromises?\b", x["when"].lower()) and not re.search(r"\bno promises?\b", x["when"].lower()))
                      and re.search(r"(^|[^a-z_])" + re.escape(r.lower()) + r"([^a-z_]|$)", x["when"].lower())), None)
        if row:
            note_map[r] = _pair(row["outcome"], r)
        elif whole:
            note_map[r] = _pair(whole["outcome"], "")
        elif r in reasons_of:
            note_map[r] = _pair(r, "")
        else:
            trg = next((t for t in p["triggers"] if t["note_reason"] == r and t["outcome"] and t["action"] != "transfer"), None)
            if trg:
                note_map[r] = _pair(trg["outcome"], trg["reason"])
            elif refused["outcome"]:
                note_map[r] = _pair(refused["outcome"], r)

    # a promise outcome for calls where some product was also noted ("at least one promise and at least one product
    # noted ..."), with the note reasons that do not count ("other than X")
    mixed = explicit("promise_mixed")
    mixed_except = [_s(x) for x in _list(_dict(given.get("promise_mixed")).get("except_notes"))]
    if not mixed:
        for r in table:
            w = r["when"].lower()
            if (not r["reason"] and r["outcome"] != promise["outcome"] and "promise" in w and re.search(r"\bnoted?\b", w)
                    and not re.search(r"\bno promise\b", w)):
                mixed = _pair(r["outcome"])
                tail = re.split(r"other than|except|besides|apart from", w, 1)
                if len(tail) > 1:
                    mixed_except = [x for x in p["notes"]["tool_reasons"] if x.lower() in tail[1]]
                break
    mixed = mixed or _pair("")

    # severity order of note reasons for a no-promise close ("the most serious ... in this order: a, then b, ...")
    order = [_s(x) for x in _list(given.get("note_severity")) if _s(x)]
    if not order:
        texts = p["outcomes"]["precedence"] + [p["outcomes"]["multi_product"]] + [r["when"] for r in table] \
            + [r["text"] for r in p["rules"]] + [_s(x) for x in _list(p["outcomes"].get("notes"))]
        for t in texts:
            low = t.lower()
            # a severity list needs a severity word; "in get_account order" alone is not one
            if not re.search(r"\bserious|\bseverity|\bpriority|\brank|\bin this order\b", low):
                continue
            # the list follows one of these words ("most serious ..., ignoring x, in this order: a, then b"); try them
            # from the last one back and take the first that is followed by two or more note reasons
            hits = [m.end() for m in re.finditer(r"\border\b|\bserious\w*|\bseverity|\bpriority", low)]
            for pos in reversed(hits):
                seg = low[pos:]
                found = sorted((seg.find(x.lower()), x) for x in p["notes"]["tool_reasons"] if x.lower() in seg)
                if len(found) >= 2:
                    order = [x for _i, x in found]
                    break
            if order:
                break

    nro = given.get("no_reason_outcomes")
    return {**given, "promise": promise, "id_fail": explicit("id_fail") or id_fail, "callback": callback,
            "no_callback": no_callback, "promise_mixed": mixed, "promise_mixed_except": mixed_except,
            "note_severity": order, "refused": refused, "service_error": service_error, "transfer": transfer, "note_map": note_map,
            "transfer_overrides_promise": _bool(given.get("transfer_overrides_promise"), True),
            "no_reason_outcomes": [_s(x) for x in _list(nro)] if nro is not None else no_reason}


# ----------------------------------------------------------------------------------------------- validate

def validate(policy):
    """List of problems that make the policy unusable or ambiguous for the generator. [] means OK."""
    errors, _warnings = check(policy)
    return errors


def check(policy):
    """(errors, warnings). Errors block generation; warnings point at fields the generator had to guess."""
    errors, warnings = [], []
    if not isinstance(policy, dict):
        return ["policy is not a JSON object"], []
    for k in REQUIRED_KEYS:
        if k not in policy:
            errors.append("missing top-level key: %s" % k)
    for k in policy:
        if k not in REQUIRED_KEYS and k not in ("language", "api", "closing", "notes"):
            warnings.append("unknown top-level key kept as is: %s" % k)
    try:
        p = normalise(policy)
    except Exception as e:  # normalise must not fail; report instead of raising
        return errors + ["normalise failed: %r" % (e,)], warnings

    raw_cal = _dict(policy.get("calendar"))
    for h in _list(raw_cal.get("holidays")):
        if not ISO_DATE.match(_s(h)):
            errors.append("calendar.holidays: not YYYY-MM-DD: %r" % (h,))
    for w in _list(raw_cal.get("weekend")):
        if _int(w) is None or not 1 <= _int(w) <= 7:
            errors.append("calendar.weekend: not an ISO weekday 1-7: %r" % (w,))
    if p["calendar"]["roll_rule"] not in ROLL_RULES:
        errors.append("calendar.roll_rule must be one of %s" % ROLL_RULES)

    if not p["clock"]["now_variable"]:
        errors.append("clock.now_variable is empty")
    elif p["variables"] and p["clock"]["now_variable"] not in [v["name"] for v in p["variables"]]:
        warnings.append("clock.now_variable %r is not listed in variables" % p["clock"]["now_variable"])

    verify_fields = tool_enum(p, "verify_identity", "field")
    if not p["identity"]["questions"]:
        errors.append("identity.questions is empty")
    for q in p["identity"]["questions"]:
        if not q["tool_field"]:
            errors.append("identity question %s has no tool_field" % q["order"])
        elif verify_fields and q["tool_field"] not in verify_fields:
            errors.append("identity question tool_field %r not in verify_identity field enum %s" % (q["tool_field"], verify_fields))
        if q["answer_format"] not in ANSWER_FORMATS:
            warnings.append("identity question %s: unknown answer_format %r" % (q["order"], q["answer_format"]))
        if q["normalise"] not in NORMALISERS:
            errors.append("identity question %s: normalise must be one of %s" % (q["order"], NORMALISERS))
    if p["identity"]["max_wrong"] is None:
        warnings.append("identity.max_wrong missing; the tool's own counter is used")

    outcome_enum = tool_enum(p, "close_call", "outcome")

    def check_outcome(where, o):
        if o and outcome_enum and o not in outcome_enum:
            errors.append("%s: outcome %r not in close_call outcome enum %s" % (where, o, outcome_enum))

    check_outcome("identity.fail", p["identity"]["fail"]["outcome"])
    if not p["identity"]["fail"]["outcome"]:
        errors.append("identity.fail.outcome is empty")

    if not p["products"]["types"]:
        warnings.append("products.types is empty; product names fall back to the type value")
    for k, t in p["products"]["types"].items():
        if not t["full_field"]:
            errors.append("products.types.%s.full_field is empty" % k)
        if not t["floor_field"] and not t["floor_expr"]:
            warnings.append("products.types.%s.floor_field is empty; floor = full amount" % k)

    if not p["bands"]:
        errors.append("bands is empty")
    prev_hi = None
    for b in p["bands"]:
        if b["min_days_late"] is None or b["max_days_late"] is None:
            warnings.append("band %s: open days_late range" % b["name"])
        elif b["min_days_late"] > b["max_days_late"]:
            errors.append("band %s: min_days_late > max_days_late" % b["name"])
        if prev_hi is not None and b["min_days_late"] is not None and b["min_days_late"] <= prev_hi:
            errors.append("band %s overlaps the previous band" % b["name"])
        prev_hi = b["max_days_late"] if b["max_days_late"] is not None else prev_hi
        dl = b["deadline"]
        if dl["kind"] not in DEADLINE_KINDS:
            errors.append("band %s: deadline.kind must be one of %s" % (b["name"], DEADLINE_KINDS))
        if dl["kind"] in ("nth_business_day", "calendar_days") and not dl["n"]:
            errors.append("band %s: deadline.n missing" % b["name"])
        if dl["kind"] == "fixed_date" and not ISO_DATE.match(_s(dl.get("date"))):
            errors.append("band %s: deadline.date must be YYYY-MM-DD" % b["name"])
        if dl["counted_from"] not in COUNTED_FROM:
            errors.append("band %s: deadline.counted_from must be one of %s" % (b["name"], COUNTED_FROM))
        if not b["attempts"]:
            warnings.append("band %s has no persuasion attempts" % b["name"])

    cb = p["callback"]
    if cb["days"]:
        for k in ("start", "end"):
            if not HHMM.match(cb[k]):
                errors.append("callback.%s must be HH:MM" % k)
        for d in cb["days"]:
            if d not in ("today", "tomorrow", "next_business_day") and not re.match(r"^business_days:\d+$", d) \
                    and not ISO_DATE.match(d):
                errors.append("callback.days: unsupported value %r" % d)
    elif "schedule_callback" in p["tools"]:
        warnings.append("callback.days is empty; callbacks can never be scheduled")
    for wd, w in cb["per_weekday"].items():
        if w and not (HHMM.match(w["start"]) and HHMM.match(w["end"])):
            errors.append("callback.per_weekday.%s: start/end must be HH:MM" % wd)

    queues = tool_enum(p, "transfer_to_agent", "queue")
    reasons = tool_enum(p, "add_note", "reason")
    for t in p["triggers"]:
        if t["action"] not in TRIGGER_ACTIONS:
            warnings.append("trigger %s: unknown action %r" % (t["name"], t["action"]))
        if t["action"] == "transfer" and not t["queue"]:
            errors.append("trigger %s: transfer without queue" % t["name"])
        if t["queue"] and queues and t["queue"] not in queues:
            errors.append("trigger %s: queue %r not in transfer_to_agent enum %s" % (t["name"], t["queue"], queues))
        if t["note_reason"] and reasons and t["note_reason"] not in reasons:
            errors.append("trigger %s: note_reason %r not in add_note enum %s" % (t["name"], t["note_reason"], reasons))
        check_outcome("trigger %s" % t["name"], t["outcome"])
        if t["condition"] and (not t["condition"]["field"] and "count" not in t["condition"]["over"]
                               or t["condition"]["value"] is None or t["condition"]["op"] not in CONDITION_OPS):
            errors.append("trigger %s: condition needs field, op (one of %s) and a numeric value" % (t["name"], CONDITION_OPS))
    for c in p["third_party"]["cases"]:
        check_outcome("third_party case %r" % c["trigger"], c["outcome"])
    for r in p["outcomes"]["table"]:
        check_outcome("outcomes.table", r["outcome"])
    if not p["outcomes"]["table"]:
        errors.append("outcomes.table is empty")

    for name in ("verify_identity", "get_account", "create_promise", "close_call"):
        if name not in p["tools"]:
            errors.append("tools.%s missing" % name)
    if not p["api"]["base_url"] and not all(_s(t.get("url")) for t in p["tools"].values()):
        warnings.append("no api.base_url; generate_workflow needs the openapi file to know the tool URLs")

    cl = p["closing"]
    for k in ("promise", "refused"):
        if not cl[k]["outcome"]:
            warnings.append("closing.%s could not be derived from the outcome table" % k)
    if not p["rules"]:
        warnings.append("rules is empty")
    return errors, warnings
