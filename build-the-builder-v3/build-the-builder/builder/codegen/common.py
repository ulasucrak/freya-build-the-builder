# Runtime helpers shared by the generated code nodes and tool hooks.
#
# This file is NOT imported. generate.py copies the functions a snippet needs (together with the oracle
# functions) into the body of the generated `freya_main(vs, extra)` function. Inside it these names exist:
#   vs     the workflow variable table (dict-like; a plain dict when the platform gives none)
#   extra  the hook object: `config` for a pre-hook, `result` for a post-hook, None in a code node
#   RT     the runtime policy (dict) generated from policy.json, with RT["names"] for tool/field names
# Same restrictions as the oracle's embedded region: no double braces, no leading underscores, never raise.


def vget(vs, key, default=""):
    try:
        v = vs.get(key)
    except Exception:
        v = None
    if v is None:
        return default
    return v


def vset(vs, key, value):
    try:
        vs[key] = value
    except Exception:
        pass


def nm(key, default=""):
    return (RT.get("names") or dict()).get(key) or default


def num(value, default=0.0):
    n = to_number(value)
    return default if n is None else n


def cnt(vs, key):
    """A counter variable as an int (0 when unset or unreadable); no number parsing pulled in."""
    try:
        return int(float(vget(vs, key, 0) or 0))
    except Exception:
        return 0


def csv_list(value):
    return [x for x in str(value or "").split(",") if x]


def resp_data(result):
    """The API's JSON body inside a tool result envelope, or None."""
    try:
        d = result.get("data")
        if isinstance(d, dict):
            return d
        if d is None and ("ok" in result or "error" in result) and "status_code" not in result:
            return result
    except Exception:
        pass
    return None


def call_ok(result, ok_field):
    """The one success rule: HTTP 2xx, a JSON body, no error in it, and its ok flag (when it has one) true."""
    try:
        d = resp_data(result)
        return str(result.get("status") or "").lower() in ("", "success", "ok", "200") \
            and str(result.get("status_code") or 2)[:1] == "2" and not d.get("error") \
            and all(truthy(d[k]) for k in (ok_field, "ok") if k in d)
    except Exception:
        return False


def var_ok(vs, tool, f):
    """call_ok on the engine's copy of the last response (<tool>_response.data.*): the ok flag when set, else no error."""
    p = tool + "_response.data."
    o = vget(vs, p + f, "")
    return truthy(o) if str(o) else not vget(vs, p + "error")


def err_kind(result):
    """An answer that is not a service failure: 'done' (already recorded/exists) or 'failed' (locked, no tries left)."""
    e = api_error(result).lower()
    if re.search(r"locked|too many", e) or str((resp_data(result) or dict()).get(nm("verify_left", "wrong_answers_left"))).strip() in ("0", "0.0"):
        return "failed"
    return "done" if re.search(r"already|duplicate|(?<!not )exists", e) else ""


def fails(vs, tool, add=0):
    """Failed answers of `tool` in this call (w_fail_<tool>), plus `add`, stored when `add` is given."""
    n = cnt(vs, "w_fail_" + tool) + add
    if add:
        vset(vs, "w_fail_" + tool, str(n))
    return n


SVC = "Do not call it again; say it cannot be done now and close the call as the rules say for a service error."


def svc_close():
    """SVC with the closing table's service-error outcome (post-hooks; pre-hooks keep RT small and use SVC)."""
    se = pair((RT.get("closing") or dict()).get("service_error"))
    return ("Do not call it again: tell the customer it cannot be done right now. Then " + close_instruction(se["outcome"], se["reason"])) if se["outcome"] else SVC


def api_error(result):
    try:
        return str((resp_data(result) or dict()).get("error") or result.get("message") or "")
    except Exception:
        return ""


def set_next(result, text):
    """Add the instruction for the model to the tool result (what_to_do_next) and drop response headers."""
    try:
        if isinstance(result, dict):
            result.pop("header", None)
            d = result.get("data")
            if not isinstance(d, dict):
                d = dict()
                result["data"] = d
            d["what_to_do_next"] = text
    except Exception:
        pass


def body_of(config):
    try:
        b = config.get("body")
        if isinstance(b, str):
            b = json.loads(b)
            config["body"] = b
        if isinstance(b, dict):
            return b
    except Exception:
        pass
    return dict()


def block_request(config, vs, flag, info, tool=""):
    """Stop a request before it reaches the bank: the URL cannot resolve and its host name says why. The reason
    also goes to w_last_event (every prompt shows it): no post-hook runs for a request without an HTTP response."""
    g = info.get
    config["url"] = "https://not-sent-" + (re.sub(r"[^a-z0-9]+", "-", str(g("code") or "rule").lower()).strip("-")[:30] or "rule") + "-nothing-recorded.invalid/"
    vset(vs, flag, json.dumps(info))
    vset(vs, "w_last_event", "Your last " + (tool or "tool") + " request was NOT sent to the bank and nothing was recorded. " + str(g("message") or "") + " " + str(g("next") or ""))


def request_sent(vs):
    vset(vs, "w_last_event", "none")


# Call state that tools write is kept in one variable per product (or per queue): several tool calls of one model
# message run in parallel on copies of the variable table and are merged key by key afterwards, so a shared list
# variable would keep only the last call's write.

def key_of(prefix, ident):
    return prefix + re.sub(r"[^A-Za-z0-9]", "_", str(ident))


def per_key(vs, prefix):
    """{product id: value} for the products in the account, from variables named prefix + product id."""
    out = dict()
    for p in products_of(vs):
        pid = p.get("id")
        v = vget(vs, key_of(prefix, pid), "")
        if v not in ("", None):
            out[str(pid)] = str(v)
    return out


def mark(vs, prefix, ident, value):
    vset(vs, key_of(prefix, ident), value)


def note_intents(vs):
    n = load_json(vget(vs, "w_note_intents"), dict())
    n = n if isinstance(n, dict) else dict()
    n.update(per_key(vs, "w_intent_"))
    return n


def notes_apply(vs):
    """Notes are used in this call: enabled by the policy and enough products (some banks note only multi-product calls)."""
    nt = RT.get("notes") or dict()
    if not nt.get("enabled"):
        return False
    return len(products_of(vs)) >= int(num(nt.get("min_products"), 1))


def missing_notes(vs, facts):
    """Products with no recorded promise and no note yet, as 'id (name, reason ...)' texts; [] when notes do not apply."""
    if facts.get("transfers") or not facts.get("verified") or not notes_apply(vs) or fails(vs, nm("note_tool", "add_note")) >= 2:
        return []
    out = []
    intents = note_intents(vs)
    notes = notes_of(vs)
    for p in products_of(vs):
        pid = p.get("id")
        if pid not in (facts.get("promises") or []) and pid not in notes:
            out.append(str(pid) + " (" + str(p.get("name")) + (", reason '" + str(intents[pid]) + "'" if pid in intents else "") + ")")
    return out


def notes_instruction(vs):
    """What to do about notes when every product is handled; '' when no note is needed."""
    if not notes_apply(vs) or transfers_of(vs):
        return ""
    intents = note_intents(vs)
    sent = notes_of(vs)
    todo = [p.get("id") for p in products_of(vs) if p.get("id") not in promised_of(vs) and p.get("id") not in sent]
    if not todo:
        return ""
    return ("Now send the notes, one add_note call per message, before closing: " + "; ".join(
        ["product " + str(pid) + (" with reason '" + str(intents[pid]) + "'" if pid in intents else " with its reason") for pid in todo])
        + ". Then close the call.")


def today_of(vs):
    return to_date(vget(vs, "w_today")) or parse_now(vget(vs, nm("now_var", "now")))[0]


def now_time_of(vs):
    return str(vget(vs, "w_now_time") or "") or parse_now(vget(vs, nm("now_var", "now")))[1]


def products_of(vs):
    return [p for p in load_json(vget(vs, "w_products_json"), []) if isinstance(p, dict)]


def find_product(products, pid):
    low = str(pid or "").strip().lower()
    for f in ("id", "name", "type"):
        for p in products:
            if low and low == str(p.get(f) or "").lower():
                return p
    return products[0] if len(products) == 1 and not low else None


def sent_ok(vs, tool, prefix, okf):
    """{product id: value} of requests a pre-hook marked as sent ('0': known failed), when the engine counted every
    request of `tool` as a success, none as a failure, and the last answer is ok (var_ok). Parallel tool calls can
    leave a post-hook unsure which product it answers; per-product marks and the counters survive. Else {}."""
    s = dict((k, v) for k, v in per_key(vs, prefix).items() if v != "0")
    return s if s and cnt(vs, tool + "_failure_count") == 0 and cnt(vs, tool + "_success_count") >= len(s) and var_ok(vs, tool, okf) else dict()


def in_flight(vs, prefix, done):
    """Products a pre-hook marked as sent (prefix) and no post-hook credited yet (done prefix)."""
    d = per_key(vs, done)
    return [k for k, v in per_key(vs, prefix).items() if v != "0" and k not in d]


def promised_of(vs):
    out = csv_list(vget(vs, "w_promised"))
    return out + [k for k in dict(per_key(vs, "w_promised_"), **sent_ok(vs, nm("promise_tool", "create_promise"), "w_cp_sent_", nm("promise_ok", "ok"))) if k not in out]


def notes_of(vs):
    n = load_json(vget(vs, "w_notes"), dict())
    n = n if isinstance(n, dict) else dict()
    n.update(sent_ok(vs, nm("note_tool", "add_note"), "w_note_sent_", nm("note_ok", "ok")))
    n.update(per_key(vs, "w_note_"))
    return n


def transfers_of(vs):
    out = csv_list(vget(vs, "w_transfers"))
    try:
        out += [str(vget(vs, k)) for k in list(vs.keys()) if str(k).startswith("w_transfer_q_") and vget(vs, k)]
    except Exception:
        pass
    t = nm("transfer_tool", "transfer_to_agent")
    q = vget(vs, t + "_args." + nm("queue_field", "queue"))
    return out or ([str(q)] if q and cnt(vs, t + "_success_count") and truthy(vget(vs, t + "_response.data." + nm("transfer_ok", "ok"), "false")) else [])


def id_state_of(vs):
    st = str(vget(vs, "w_id_state") or "")
    tool = nm("verify_tool", "verify_identity")
    if st != "verified" and truthy(vget(vs, tool + "_response.data." + nm("verify_all", "all_verified"))):
        st = "verified"
    if st not in ("verified", "failed"):
        left = vget(vs, tool + "_response.data." + nm("verify_left", "wrong_answers_left"), None)
        if left is not None and str(left).strip() in ("0", "0.0"):
            st = "failed"
    return st


def collect_facts(vs):
    products = products_of(vs)
    promises = promised_of(vs)
    if not promises:
        tool = nm("promise_tool", "create_promise")
        if num(vget(vs, tool + "_success_count", 0)) > 0 and truthy(vget(vs, tool + "_response.data." + nm("promise_ok", "ok"))) and not vget(vs, tool + "_response.data.error"):
            promises = ["unknown"]
    st = id_state_of(vs)
    cb_done = str(vget(vs, "w_callback_done")) == "yes"
    notes = dict(note_intents(vs))
    notes.update(notes_of(vs))
    return dict(verified=st == "verified", id_failed=st == "failed", transfers=transfers_of(vs),
                promises=promises, notes=notes, products=[p.get("id") for p in products],
                callback_done=cb_done)


def remaining_products(vs):
    """Products with neither a recorded promise nor a note (sent or decided), in get_account order."""
    done = set(promised_of(vs)) | set(notes_of(vs).keys()) | set(note_intents(vs).keys())
    return [p for p in products_of(vs) if p.get("id") not in done]


def close_instruction(outcome, reason):
    t = "thank the customer, say goodbye and call close_call with outcome '" + str(outcome) + "'"
    if reason:
        t += " and reason '" + str(reason) + "'"
    return t + ", all in the same message."


# w_next_action: one line, "<what just happened or the state> - next: <the single next required action>", written by
# the post-hooks and code nodes from the recorded call state (generic: product ids, counts, outcome names only; never
# what the customer said). The conversation prompts show it as {{w_next_action}}; '' when nothing specific applies.

def next_action(vs):
    """(state, action) from the recorded call state; ('', '') when nothing specific applies. Reads no RT section
    (each one named here would be embedded in every hook that calls it)."""
    facts = collect_facts(vs)
    if str(vget(vs, "w_close_done")) == "yes":
        return "The call is closed", "say nothing more"
    if facts.get("transfers"):
        return "Transfer done", "tell the customer a colleague will take over, record nothing else and close_call as the rules say"
    if facts.get("id_failed"):
        return "Identity check failed", "say nothing about the account and close_call as the rules say"
    if facts.get("callback_done") and not facts.get("verified"):
        return "Callback scheduled", "say when the customer will be called, without saying why, and close_call"
    if not facts.get("verified"):
        wrong = int(num(vget(vs, "w_id_wrong", 0)))
        return ("Identity not verified yet (" + str(len(csv_list(vget(vs, "w_id_done")))) + " answer(s) correct, " + str(wrong) + " wrong)",
                "ask the next security question as the rules say and call verify_identity with the answer")
    acc = str(vget(vs, "w_account_state") or "")
    if acc != "ok":
        return ("Identity verified", "the account is loaded automatically; then state the debt as in the account facts") if acc == "pending" else ("", "")
    rest = [str(p.get("id")) for p in remaining_products(vs)]
    n = len(facts.get("products") or [])
    if rest:
        return (str(n - len(rest)) + " of " + str(n) + " products handled; open: " + ", ".join(rest),
                "for each open product get a clear yes to a read-back and call create_promise, or decide its note reason, as the rules say")
    todo = [str(p) for p in facts.get("products") or [] if p not in facts.get("promises") and p not in notes_of(vs)] if notes_apply(vs) else []
    if todo:
        return "All products handled", "add_note for " + ", ".join(todo) + " (one per message), then close_call"
    return "All products handled", "ask whether there is anything else, then close_call with the outcome the rules give"


def set_action(vs, event="", action=""):
    """Write w_next_action: `event` replaces the state part, `action` the derived next action."""
    try:
        st, act = next_action(vs)
    except Exception:
        st, act = "", ""
    st = event or st
    act = action or act
    vset(vs, "w_next_action", (st + " - next: " + act + ".") if act else "")


NOTE_TEXT_FIELDS = ("text", "note", "notes", "comment", "comments", "description", "details", "detail", "message",
                    "remark", "remarks", "summary", "memo", "body", "content")


def safe_note_text(reason, reasons):
    """The only free text a note may carry: a fixed phrase with the reason code when it is one of the tool's reasons."""
    r = str(reason or "").strip()
    return ("No promise. Reason: " + r + ".") if r and r in reasons else "No promise."


def match_product(prods, said):
    """The product whose name, type or id shares the most words with what was said ('the card', 'car loan'); None
    when nothing or several match equally."""
    stop = set(["the", "my", "a", "an", "for", "of", "on", "ending", "one", "product", "account", "and", "that", "this"])
    words = set(re.findall(r"[a-z0-9]+", str(said or "").lower())) - stop
    best, score, tie = None, 0, False
    for p in prods if words else []:
        pw = set(re.findall(r"[a-z0-9]+", (str(p.get("name") or "") + " " + str(p.get("type") or "").replace("_", " ")
                                            + " " + str(p.get("id") or "")).lower()))
        s = len(words & pw)
        if s > score:
            best, score, tie = p, s, False
        elif s == score and s > 0:
            tie = True
    return None if tie or score == 0 else best
