"""Synthetic self-test cases from a policy, in public-test-set format.

Python plans every case (customer data, products, call time, what the customer does) and computes the expected
ledger with builder.oracle (deadlines, offers, closing outcome). The LLM only writes the customer's script, the
conversation checks and plausible values for the call variables; it never decides an expected value.

    cases, warnings = generate_cases(policy, ask_json=llm.ask_json, n_max=12, deadline=..., seed=7)

Case kinds (each only when the policy has what it needs): happy path, second attempt, date after the deadline,
identity failure, third party (callback or end), one per actionable trigger (transfer / note / close: hardship,
dispute, already paid, human request ...), tool failure, multi-product (max_count > 1), a customer who talks in words.
Nothing here knows a brief: every value comes from the policy at runtime.
"""
import copy
import datetime
import json
import random
import re

try:
    from builder import oracle as _oracle
except Exception:  # pragma: no cover - resolved lazily when the module exists
    _oracle = None
try:
    from builder import policy_schema as _schema
except Exception:  # pragma: no cover
    _schema = None


def _orc():
    global _oracle
    if _oracle is None:
        from builder import oracle as o
        _oracle = o
    return _oracle


def _normalise(policy):
    global _schema
    if _schema is None:
        try:
            from builder import policy_schema as s
            _schema = s
        except Exception:
            return copy.deepcopy(policy)
    try:
        return _schema.normalise(policy)
    except Exception:
        return copy.deepcopy(policy)


def _none(x):
    return x if x not in ("", None) else None


# ------------------------------------------------------------------------------------------- formats

def format_now(dt, fmt):
    """datetime -> text in the policy's clock.date_format ('YYYY-MM-DDTHH:MM', 'DD/MM/YYYY HH:MM' ...).
    Falls back to ISO 'YYYY-MM-DDTHH:MM' when the format is unknown or would not parse back."""
    iso = dt.strftime("%Y-%m-%dT%H:%M")
    m = re.search(r"[YMDHhmsdyT:./\- ]*(?:YYYY|yyyy)[YMDHhmsdyT:./\- ]*", str(fmt or ""))
    f = m.group(0).strip() if m else ""
    if not f:
        return iso
    s = re.sub(r"(HH|hh)([:.h])(MM|mm)", lambda m: dt.strftime("%H") + m.group(2) + dt.strftime("%M"), f)
    s = re.sub(r"YYYY|yyyy", dt.strftime("%Y"), s)
    s = re.sub(r"MM", dt.strftime("%m"), s)
    s = re.sub(r"DD|dd", dt.strftime("%d"), s)
    s = re.sub(r"HH|hh", dt.strftime("%H"), s)
    s = re.sub(r"mm", dt.strftime("%M"), s)
    if re.search(r"[A-Za-z]", s.replace("T", "")):
        return iso
    try:
        d, t = _orc().parse_now(s)
        if d == dt.date() and (not t or t == dt.strftime("%H:%M")):
            return s
    except Exception:
        pass
    return iso


def _date_format_hint(policy, field):
    """'DD-MM-YYYY'-like hint for an identity date field, from the tool API description, else ''."""
    vi = ((policy.get("tools") or {}).get("verify_identity") or {})
    texts = [json.dumps(vi)]
    for q in (policy.get("identity") or {}).get("questions") or []:
        if q.get("tool_field") == field:
            texts.append(json.dumps(q))
    m = re.search(r"\b(DD|YYYY|MM)([-./])(MM|DD)\2(YYYY|DD)\b", " ".join(texts))
    return m.group(0) if m else ""


def identity_value(q, policy, rng):
    """A correct answer for one identity question, in the format the tool expects."""
    fmt = (q.get("answer_format") or "text").lower()
    if fmt == "date":
        d = datetime.date(rng.randint(1955, 2000), rng.randint(1, 12), rng.randint(1, 28))
        hint = _date_format_hint(policy, q.get("tool_field"))
        if hint and not hint.startswith("YYYY"):
            sep = hint[2]
            parts = hint.split(sep)
            vals = {"DD": "%02d" % d.day, "MM": "%02d" % d.month, "YYYY": "%04d" % d.year}
            return sep.join(vals.get(p, "") for p in parts)
        return d.isoformat()
    if fmt == "digits" and "year" in (str(q.get("tool_field") or "") + " " + str(q.get("ask") or "")).lower():
        return str(rng.randint(1950, 2000))         # a year of birth is a year, not four random digits (3573)
    if fmt == "digits":
        n = 4
        m = re.search(r"(\d+)", str(q.get("tool_field") or ""))
        if m and 2 <= int(m.group(1)) <= 12:
            n = int(m.group(1))
        return "".join(str(rng.randint(0, 9)) for _ in range(n))
    return ""   # text answers: the LLM proposes one, else a neutral code below


def wrong_value(value, fmt):
    v = str(value)
    if fmt == "date":
        out = re.sub(r"\d{4}", lambda m: str(int(m.group(0)) + 1), v, count=1)
        return out if out != v else v + "1"
    if fmt == "digits" and re.fullmatch(r"(19|20)\d\d", v):
        return str(int(v) + 7)                      # a believable wrong year
    if fmt == "digits":
        return "".join(str((int(c) + 3) % 10) if c.isdigit() else c for c in v)
    return v[::-1] if len(v) > 1 else v + "X"


def money_text(x):
    try:
        x = float(x)
    except Exception:
        return str(x)
    return ("%d" % x) if abs(x - round(x)) < 0.005 else ("%.2f" % x)


def speak_date(iso):
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
        return d.strftime("%A ") + str(d.day) + d.strftime(" %B %Y")
    except Exception:
        return str(iso)


# ------------------------------------------------------------------------------------------- planning

class Planner:
    def __init__(self, policy, seed=7, base_date=None):
        self.raw = policy
        self.p = _normalise(policy)
        self.o = _orc()
        self.cal = self.o.make_calendar(self.p)
        self.rng = random.Random(seed)
        self.base = base_date or datetime.date.today()
        self.warnings = []
        self.day_cursor = 0

    # --- data helpers
    def next_now(self, hour=None):
        """A business-day call time, walking forward so cases fall on different weekdays."""
        d = self.base + datetime.timedelta(days=3 + self.day_cursor)
        self.day_cursor += self.rng.choice([1, 2, 3])
        while not self.o.is_business_day(d, self.cal):
            d += datetime.timedelta(days=1)
            self.day_cursor += 1
        h = hour if hour is not None else self.rng.choice([9, 10, 11, 13, 14])
        return datetime.datetime(d.year, d.month, d.day, h, self.rng.choice([0, 15, 30]))

    def product_types(self):
        types = (self.p.get("products") or {}).get("types") or {}
        return [(k, v) for k, v in types.items() if v.get("full_field")]

    def bands(self):
        return [b for b in self.p.get("bands") or [] if b.get("min_days_late") is not None]

    def raw_product(self, band, type_idx=0):
        pr = self.p["products"]
        types = self.product_types()
        if not types:
            return None
        tname, t = types[type_idx % len(types)]
        lo = band.get("min_days_late") or 1
        hi = band.get("max_days_late") if band.get("max_days_late") is not None else lo + 20
        days = self.rng.randint(int(lo), int(max(lo, hi)))
        full = float(self.rng.randrange(180, 1400, 5))
        raw = {pr.get("type_field") or "type": tname, pr.get("days_late_field") or "days_late": days, t["full_field"]: full}
        if t.get("floor_field") and t["floor_field"] != t["full_field"]:
            raw[t["floor_field"]] = float(int(full * self.rng.choice([0.25, 0.3, 0.35])))
        for ph in re.findall(r"\{(\w+)\}", t.get("speak_as") or ""):
            if ph not in raw:
                raw[ph] = "".join(str(self.rng.randint(0, 9)) for _ in range(4))
        return raw

    def built(self, raws, now):
        prods = self.o.products_for_case(self.p, raws, now.strftime("%Y-%m-%dT%H:%M"))
        for i, b in enumerate(prods):
            b["pid"] = "p%d" % (i + 1)
        return prods

    def closing(self, **facts):
        facts.setdefault("verified", True)
        r = self.o.compute_closing(facts, self.p)
        return _none(r.get("outcome")), _none(r.get("reason"))

    def identity(self):
        out, fmts = {}, {}
        for q in (self.p.get("identity") or {}).get("questions") or []:
            f = q.get("tool_field")
            if f:
                out[f] = identity_value(q, self.p, self.rng)
                fmts[f] = q.get("answer_format")
        return out, fmts

    def case(self, kind, family, level, title, now, raws, plan, expected, faults=None, third_party=False, style=None):
        ident, fmts = self.identity()
        c = {"id": "", "brief": "SELF", "family": family, "level": level, "title": title, "kind": kind,
             "now": now, "identity": ident, "identity_formats": fmts, "products": raws or [], "plan": plan,
             "expected": expected}
        if faults:
            c["faults"] = faults
        if third_party:
            c["third_party"] = True
        if style:
            c["style"] = style
        return c

    @staticmethod
    def ledger(outcome, reason, promises=(), notes=(), callbacks=(), transfers=()):
        return {"outcome": outcome, "reason": reason, "promises": list(promises), "notes": list(notes),
                "callbacks": list(callbacks), "transfers": list(transfers)}

    def offer(self, prod, n, now=None):
        """Attempt n of a built product with a concrete amount and date, or None. When the oracle could not read
        the expression, its leading keyword is tried (e.g. 'floor (if ...)' -> 'floor')."""
        offs = prod.get("offers") or []
        if len(offs) < n:
            return None
        o = dict(offs[n - 1])
        today = now.date() if now else None
        if o.get("amount") is None:
            m = re.match(r"\s*([A-Za-z_]+|\d+(?:\.\d+)?%?)", o.get("amount_expr") or "")
            if m:
                o["amount"] = self.o.resolve_offer_amount(m.group(1), prod.get("full"), prod.get("floor"))
        if not o.get("date") and today is not None:
            m = re.match(r"\s*([A-Za-z_:+0-9-]+)", o.get("date_expr") or "")
            dl = datetime.date.fromisoformat(prod["deadline"]) if prod.get("deadline") else None
            if m:
                d = self.o.resolve_offer_date(m.group(1), today, dl, self.cal)
                o["date"] = d.isoformat() if d else ""
        if o.get("amount") is not None and o.get("date"):
            return o
        return None

    # --- kinds
    def plan_promise_case(self, kind, band, attempt=1, style=None):
        now = self.next_now()
        raw = self.raw_product(band)
        if not raw:
            return None
        prod = self.built([raw], now)[0]
        off = self.offer(prod, attempt, now)
        if attempt > 1 and not off:
            return None
        outcome, reason = self.closing(promises=["p1"], products=["p1"])
        if not outcome:
            self.warnings.append("%s: no closing outcome for a promise" % kind)
            return None
        if attempt == 1:
            if prod.get("full") is None:
                return None
            today = now.date().isoformat()
            promise = {"product_id": "p1", "amount": prod["full"], "date": today}
            plan = ("Is the customer. Answers the security questions correctly. Says they can pay the full amount (%s) "
                    "today, and confirms at the read-back." % money_text(prod["full"]))
            fam, lvl, title = "Happy path", "easy", "pays the full amount today"
        else:
            first = self.offer(prod, 1, now) or {}
            promise = {"product_id": "p1", "amount": off["amount"], "date": off["date"]}
            plan = ("Is the customer. Answers the security questions correctly. Cannot pay today. Says no to the first "
                    "proposal%s. Accepts the next offer: %s on %s, and confirms at the read-back."
                    % ((" (%s on %s)" % (money_text(first.get("amount")), speak_date(first.get("date")))) if first else "",
                       money_text(off["amount"]), speak_date(off["date"])))
            fam, lvl, title = "Amounts", "medium", "declines once, accepts attempt %d" % attempt
        return self.case(kind, fam, lvl, title, now, [raw], plan,
                         self.ledger(outcome, reason, [promise]), style=style)

    def plan_deadline_case(self, band):
        now = self.next_now()
        raw = self.raw_product(band)
        if not raw:
            return None
        prod = self.built([raw], now)[0]
        if not prod.get("deadline") or prod.get("full") is None:
            return None
        dl = datetime.date.fromisoformat(prod["deadline"])
        late = dl + datetime.timedelta(days=9)
        outcome, reason = self.closing(promises=["p1"], products=["p1"])
        if not outcome:
            return None
        plan = ("Is the customer. Answers the security questions correctly. Cannot pay today. Wants to pay the full amount "
                "(%s) on %s. When told that date is too late, accepts the latest date the agent offers (%s) for the full "
                "amount, and confirms at the read-back." % (money_text(prod["full"]), speak_date(late.isoformat()), speak_date(prod["deadline"])))
        return self.case("deadline_cap", "Dates", "hard", "own date after the deadline", now, [raw], plan,
                         self.ledger(outcome, reason, [{"product_id": "p1", "amount": prod["full"], "date": prod["deadline"]}]))

    def plan_id_fail(self, band):
        now = self.next_now()
        raw = self.raw_product(band)
        fail = (self.p.get("identity") or {}).get("fail") or {}
        if not fail.get("outcome") or not (self.p.get("identity") or {}).get("questions"):
            return None
        c = self.case("id_fail", "Identity", "medium", "fails the security questions", now, [raw] if raw else [], "",
                      self.ledger(_none(fail.get("outcome")), _none(fail.get("reason"))))
        wrong = {f: wrong_value(v, c["identity_formats"].get(f)) if v else "" for f, v in c["identity"].items()}
        c["wrong_identity"] = wrong
        c["plan"] = ("Is the customer, confirms the name. Gives a WRONG answer every time a security question is asked "
                     "(wrong answers to use: %s). Does not correct them. Accepts the end of the call politely."
                     % ", ".join("%s: %s" % (k, v) for k, v in wrong.items() if v))
        return c

    def plan_third_party(self, band):
        now = self.next_now(hour=self.rng.choice([10, 11, 14]))
        raw = self.raw_product(band)
        cases = (self.p.get("third_party") or {}).get("cases") or []
        has_cb_tool = "schedule_callback" in (self.p.get("tools") or {})
        cbcase = next((c for c in cases if c.get("action") == "callback"), None)
        if cbcase and has_cb_tool:
            wins = self.o.callback_windows(now.date(), now.strftime("%H:%M"), self.p, self.cal)
            wins = [w for w in wins if w["date"] != now.date().isoformat()] or wins
            for w in wins:
                st = self.o.time_to_minutes(w["start"]) if w.get("start") else None
                en = self.o.time_to_minutes(w["end"]) if w.get("end") else None
                if st is None or en is None or en - st < 60:
                    continue
                t = st + 60 if st + 60 < en else st
                tt = "%02d:%02d" % (t // 60, t % 60)
                chk = self.o.check_callback(w["date"], tt, now.date(), now.strftime("%H:%M"), self.p, self.cal)
                if not chk.get("ok"):
                    continue
                outcome, reason = self.closing(verified=False, callback_done=True)
                if not outcome:
                    outcome, reason = _none(cbcase.get("outcome")), _none(cbcase.get("reason"))
                if not outcome:
                    break
                plan = ("Is NOT the customer: a family member answers the phone. Does not discuss the customer's affairs. "
                        "Says the customer can be reached on %s at %s and agrees to a callback then." % (speak_date(w["date"]), tt))
                return self.case("third_party_callback", "Third party", "medium", "someone else answers, callback",
                                 now, [raw] if raw else [], plan,
                                 self.ledger(outcome, reason, callbacks=[{"date": w["date"], "time": tt}]), third_party=True)
        end = next((c for c in cases if c.get("action") != "callback" and c.get("outcome")), None)
        if end:
            plan = ("Is NOT the customer. Situation: %s. Answers briefly and does not give any personal information."
                    % (end.get("trigger") or "someone else answers"))
            return self.case("third_party_end", "Third party", "medium", "not the customer", now, [raw] if raw else [], plan,
                             self.ledger(_none(end.get("outcome")), _none(end.get("reason"))), third_party=True)
        return None

    def post_verification_triggers(self):
        """Triggers a verified customer can raise (transfer, note, close), transfers and notes first. Triggers that
        end like a third-party case or an identity failure belong to the opening, not to the account talk."""
        pre = set()
        for c in (self.p.get("third_party") or {}).get("cases") or []:
            pre.add((c.get("outcome") or "", c.get("reason") or ""))
        fail = (self.p.get("identity") or {}).get("fail") or {}
        pre_outcomes = set([fail.get("outcome")] if fail.get("outcome") else [])
        out = []
        for t in self.p.get("triggers") or []:
            if t.get("action") not in ("transfer", "note", "close"):
                continue
            if (t.get("outcome") or "", t.get("reason") or "") in pre or t.get("outcome") in pre_outcomes:
                continue
            out.append(t)
        rank = {"transfer": 0, "note": 1, "close": 2}
        return sorted(out, key=lambda t: rank[t["action"]])

    def plan_trigger(self, trig, band):
        act = trig.get("action")
        now = self.next_now()
        raw = self.raw_product(band)
        if not raw:
            return None
        notes_cfg = self.p.get("notes") or {}
        notes, transfers, facts = [], [], {"products": ["p1"]}
        if act == "transfer":
            if not trig.get("queue"):
                return None
            transfers = [{"queue": trig["queue"]}]
            facts["transfers"] = [trig["queue"]]
            if trig.get("note_reason") and notes_cfg.get("enabled") and not notes_cfg.get("forbidden_on_transfer"):
                notes = [{"product_id": "p1", "reason": trig["note_reason"]}]
                facts["notes"] = {"p1": trig["note_reason"]}
        elif act == "note":
            if not trig.get("note_reason"):
                return None
            notes = [{"product_id": "p1", "reason": trig["note_reason"]}]
            facts["notes"] = {"p1": trig["note_reason"]}
        elif act != "close":
            return None
        if trig.get("outcome"):
            outcome, reason = _none(trig.get("outcome")), _none(trig.get("reason"))
            if act == "transfer":
                o2, r2 = self.closing(**facts)
                if o2:
                    outcome, reason = o2, r2
        else:
            outcome, reason = self.closing(**facts)
        if not outcome:
            return None
        plan = ("Is the customer. Answers the security questions correctly. When the overdue amount is discussed, the "
                "customer's situation is: %s. Stays consistent with that and does not agree to a payment."
                % (trig.get("detect") or trig.get("name")))
        if act == "transfer":       # the expected ledger has the transfer: the customer must take it when it is offered
            plan += (" If the agent first asks whether it can help, repeats the request once. Accepts when the agent "
                     "offers to connect them.")
        return self.case("trigger:" + trig.get("name", ""), str(trig.get("name") or "Trigger").replace("_", " ").capitalize(),
                         "medium", trig.get("name") or "trigger", now, [raw], plan,
                         self.ledger(outcome, reason, notes=notes, transfers=transfers))

    def plan_tool_failure(self, band):
        if "create_promise" not in (self.p.get("tools") or {}):
            return None
        now = self.next_now()
        raw = self.raw_product(band)
        if not raw:
            return None
        prod = self.built([raw], now)[0]
        off = self.offer(prod, 1, now)
        if not off:
            return None
        retry = int((self.p.get("promise") or {}).get("retry_on_failure") or 0)
        if retry >= 1:
            outcome, reason = self.closing(promises=["p1"], products=["p1"])
            faults = [{"tool": "create_promise", "fail_first_n": 1}]
            exp = self.ledger(outcome, reason, [{"product_id": "p1", "amount": off["amount"], "date": off["date"]}])
            extra = "The first attempt to record the promise fails on the bank's side; the customer waits patiently."
        else:
            se = ((self.p.get("closing") or {}).get("service_error") or {})
            outcome, reason = _none(se.get("outcome")), _none(se.get("reason"))
            faults = [{"tool": "create_promise", "fail_first_n": 9}]
            exp = self.ledger(outcome, reason)
            extra = "Recording the promise fails on the bank's side; the customer accepts what the agent says."
        if not outcome:
            return None
        plan = ("Is the customer. Answers the security questions correctly. Accepts the first payment offer: %s on %s, "
                "and confirms at the read-back. %s" % (money_text(off["amount"]), speak_date(off["date"]), extra))
        return self.case("tool_failure", "Tool failure", "hard", "promise tool fails", now, [raw], plan, exp, faults=faults)

    def plan_multi(self):
        maxc = int((self.p.get("products") or {}).get("max_count") or 1)
        bands = self.bands()
        if maxc < 2 or not bands:
            return None
        now = self.next_now()
        n = min(maxc, 3, max(2, len(bands)))
        raws = [self.raw_product(bands[i % len(bands)], type_idx=i) for i in range(n)]
        if any(r is None for r in raws):
            return None
        prods = self.built(raws, now)
        promises, says = [], []
        for pr in prods:
            off = self.offer(pr, 1, now)
            if not off:
                return None
            promises.append({"product_id": pr["pid"], "amount": off["amount"], "date": off["date"]})
            says.append("%s: %s on %s" % (pr.get("name") or pr["pid"], money_text(off["amount"]), speak_date(off["date"])))
        outcome, reason = self.closing(promises=[p["product_id"] for p in promises], products=[p["pid"] for p in prods])
        if not outcome:
            return None
        plan = ("Is the customer. Answers the security questions correctly. Has %d late products. For each one accepts the "
                "first offer (%s) and confirms the read-back." % (len(prods), "; ".join(says)))
        return self.case("multi_product", "Multi-product", "hard", "%d products, all promised" % len(prods), now, raws, plan,
                         self.ledger(outcome, reason, promises))

    def plan_all(self, n_max=12):
        bands = self.bands()
        out = []

        def add(c):
            if c and len(out) < n_max:
                out.append(c)
        if not bands:
            self.warnings.append("policy has no usable bands; only identity/third-party cases")
        b0 = bands[0] if bands else {}
        bmid = bands[len(bands) // 2] if bands else {}
        blast = bands[-1] if bands else {}
        if bands:
            add(self.plan_promise_case("happy", b0))
        add(self.plan_id_fail(b0))
        add(self.plan_third_party(b0))
        if bands:
            add(self.plan_deadline_case(bmid))
            multi_att = next((b for b in bands if len(b.get("attempts") or []) >= 2), None)
            if multi_att:
                add(self.plan_promise_case("second_attempt", multi_att, attempt=2))
            add(self.plan_multi())
            add(self.plan_tool_failure(blast))
            for t in self.post_verification_triggers()[:4]:
                add(self.plan_trigger(t, b0 if len(out) % 2 else blast))
            add(self.plan_promise_case("happy_in_words", blast, style="in_words"))
        for i, c in enumerate(out, 1):
            c["id"] = "SELF-%02d" % i
        return out


# ------------------------------------------------------------------------------------------- LLM part

SYSTEM = ("You write short scripts for simulated bank customers in a phone-agent test. You never change a number, "
          "date or fact given to you; you only phrase what the customer does, in the style of a test case.")


def llm_request(cases, policy):
    variables = [{"name": v.get("name"), "meaning": v.get("meaning")} for v in (policy.get("variables") or [])
                 if isinstance(v, dict)]
    now_var = ((policy.get("clock") or {}).get("now_variable"))
    items = []
    for c in cases:
        items.append({"id": c["id"], "family": c["family"], "plan": c["plan"],
                      "text_identity_fields": [f for f, v in c["identity"].items() if not v]})
    return "\n".join([
        "Write one entry per test case. Keep every number and date of the plan exactly (you may write dates as words).",
        "script: 1-4 short sentences in the third person, what the customer says and does in the call (like "
        "\"Confirms she is the customer. Gives date of birth 3 May 1980. Accepts 120 on Friday 16 October.\").",
        "checks: 2-4 short conversation checks a judge can verify from the transcript, derived from the plan only. "
        "Check what the agent must say, record or avoid; never prescribe how it orders or groups the talk (whether "
        "read-backs are combined or separate, whether it offers help before a transfer): the bank's rules decide that.",
        "variables: realistic values for these call variables (do NOT include %r, it is set by the test): %s"
        % (now_var, json.dumps(variables)),
        "identity_text: for each field in text_identity_fields a plausible correct answer value (e.g. a code or a word).",
        "", "CASES:", json.dumps(items, ensure_ascii=False, indent=1), "",
        'Answer JSON: {"cases": [{"id": "...", "script": "...", "checks": ["..."], "variables": {"name": "value"}, '
        '"identity_text": {"field": "value"}}]}'])


def fallback_variables(policy, rng):
    out = {}
    now_var = ((policy.get("clock") or {}).get("now_variable"))
    for v in policy.get("variables") or []:
        if not isinstance(v, dict) or not v.get("name") or v.get("name") == now_var:
            continue
        n = str(v["name"]).lower()
        meaning = str(v.get("meaning") or "").lower()
        if "title" in n or "honorific" in n or "salutation" in n:
            out[v["name"]] = rng.choice(["Mr", "Ms"])
        elif "first" in n or "given" in n:
            out[v["name"]] = rng.choice(["Alex", "Sam", "Robin", "Kim"])
        elif "last" in n or "surname" in n or "family" in n:
            out[v["name"]] = rng.choice(["Morgan", "Lindqvist", "Okafor", "Brandt"])
        elif re.search(r"(^|_)(id|ref)", n):
            continue                                # the test runner sets the customer id
        elif "name" in n:
            out[v["name"]] = "Alex Morgan"
    return out


def finalise(cases, policy, llm_out, rng):
    """Merge the LLM answer into the planned cases -> public-test-set format."""
    by_id = {}
    for e in (llm_out or {}).get("cases") or []:
        if isinstance(e, dict) and e.get("id"):
            by_id[str(e["id"])] = e
    now_var = ((policy.get("clock") or {}).get("now_variable")) or "now"
    date_fmt = (policy.get("clock") or {}).get("date_format")
    out = []
    for c in cases:
        e = by_id.get(c["id"], {})
        variables = fallback_variables(policy, rng)
        for k, v in (e.get("variables") or {}).items():
            if k in variables or any(isinstance(x, dict) and x.get("name") == k for x in policy.get("variables") or []):
                if k != now_var and isinstance(v, (str, int, float)) and str(v).strip():
                    variables[k] = str(v)
        variables[now_var] = format_now(c["now"], date_fmt)
        ident = dict(c["identity"])
        for f, v in ident.items():
            if not v:
                t = (e.get("identity_text") or {}).get(f)
                ident[f] = str(t) if t else "K" + "".join(str(rng.randint(0, 9)) for _ in range(4))
        script = e.get("script") if isinstance(e.get("script"), str) and e.get("script").strip() else c["plan"]
        if c.get("wrong_identity"):
            script += " Security answers to give (all wrong): " + ", ".join(
                "%s %s" % (k, v) for k, v in c["wrong_identity"].items() if v) + "."
        checks = [str(x) for x in (e.get("checks") or []) if str(x).strip()][:4] or [
            "the agent follows the bank's rules for this situation", "the agent never claims an action that failed"]
        case = {"id": c["id"], "brief": c["brief"], "family": c["family"], "level": c["level"], "title": c["title"],
                "variables": variables, "identity": ident, "products": c["products"], "script": script,
                "expected": c["expected"], "checks": checks, "kind": c["kind"], "plan": c["plan"]}
        for k in ("faults", "third_party", "style"):
            if c.get(k):
                case[k] = c[k]
        out.append(case)
    return out


def generate_cases(policy, ask_json=None, n_max=12, deadline=None, seed=7, base_date=None, model=None, timeout_s=180):
    """policy -> (cases in public-test-set format, warnings). ask_json=None skips the LLM (template scripts)."""
    pl = Planner(policy, seed=seed, base_date=base_date)
    planned = pl.plan_all(n_max=n_max)
    llm_out = None
    if ask_json and planned:
        try:
            kw = {"system": SYSTEM, "purpose": "write self-test customer scripts (%d cases)" % len(planned),
                  "timeout_s": timeout_s}
            if deadline:
                kw["deadline"] = deadline
            if model:
                kw["model"] = model
            llm_out = ask_json(llm_request(planned, pl.p), **kw)
        except Exception as e:
            pl.warnings.append("case scripts by template (LLM failed: %s)" % str(e)[:200])
    return finalise(planned, pl.p, llm_out, pl.rng), pl.warnings
