"""Extraction accuracy on the holdout banks: extract policy.json, score it against fixtures/<bank>/facts.json.

    python tools/eval_extract.py                       # extract S1..S4 (2 in parallel) and score
    python tools/eval_extract.py S2 S4 --parallel 2
    python tools/eval_extract.py --score-only          # score the saved policies/<bank>.policy.json again

Policies are saved to policies/<bank>.policy.json, a score report to policies/<bank>.eval.json.
Scoring runs on policy_schema.normalise(policy) (what the generator sees) and uses builder.oracle to compute,
for every facts.json deadline_examples entry, the band, deadline, attempt dates and floor from the extracted
policy (the product itself is looked up in cases.json by case id + days_late).
"""
import argparse
import concurrent.futures as cf
import json
import pathlib
import re
import sys
import time

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import extract, oracle, policy_schema  # noqa: E402

FIX = KIT / "fixtures"
OUT = KIT / "policies"


# ------------------------------------------------------------------ helpers

def _norm_text(s):
    s = str(s or "").lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()


def _counted_semantics(cf_value):
    """facts.json: 'tomorrow' = today never counts, 'today' = today is day 1.
    policy (oracle): 'today'/'tomorrow' = strictly after today, 'including_today' = today is day 1."""
    return "today_is_day_1" if cf_value == "including_today" else "after_today"


def _gold_counted(cf_value):
    return "today_is_day_1" if cf_value == "today" else "after_today"


def _day_kw(d):
    """Callback day as its keyword: 'today (only if a business day)' -> 'today'."""
    return re.sub(r"\s*\(.*$", "", str(d or "")).strip().lower().replace(" ", "_")


def _gold_field(fld):
    """'arrears (no lower amount; ...)' -> 'arrears'; a real expression -> None."""
    m = re.match(r"^(\w+)(\s+\(.*\))?$", str(fld or "").strip())
    return m.group(1) if m else None


def _tokens_in(phrase, entries):
    """Every word of `phrase` (minus filler) appears in one of `entries`."""
    words = [w for w in re.findall(r"[a-z0-9']+", _norm_text(phrase)) if w not in ("the", "a", "an", "any", "of")]
    for e in entries:
        ew = set(re.findall(r"[a-z0-9']+", _norm_text(e)))
        if words and all(w in ew or (w.endswith("s") and w[:-1] in ew) or w + "s" in ew for w in words):
            return True
    return False


class _WeekStart:
    """Context manager: make the oracle's week helpers start the week on ISO weekday `ws` (1 = Monday).
    The oracle only knows Monday weeks; this measures whether the EXTRACTED week start gives the gold dates."""

    def __init__(self, ws):
        self.ws = ws if isinstance(ws, int) and 1 <= ws <= 7 else 1
        self.saved = None

    def __enter__(self):
        self.saved = oracle.week_monday
        ws = self.ws
        oracle.week_monday = lambda d: d - oracle.datetime.timedelta(days=(d.isoweekday() - ws) % 7)
        return self

    def __exit__(self, *a):
        oracle.week_monday = self.saved


def _pairs_from_close_reasons(cr):
    out = set()
    for o, rs in (cr or {}).items():
        if not rs:
            out.add((o, ""))
        for r in rs or []:
            out.add((o, r or ""))
    return out


class Scorer:
    def __init__(self, bank, facts, policy, cases):
        self.bank = bank
        self.f = facts
        self.raw = policy
        self.p = policy_schema.normalise(policy)
        self.cases = {c["id"]: c for c in cases}
        self.rows = []      # (section, name, ok, gold, got)
        self.text = _norm_text(json.dumps(policy, ensure_ascii=False))

    def chk(self, section, name, gold, got, ok=None):
        if ok is None:
            ok = gold == got
        self.rows.append((section, name, bool(ok), gold, got))

    def run(self):
        for step in (self.variables, self.clock, self.calendar, self.opening, self.identity, self.products,
                     self.bands, self.examples, self.callback, self.queues, self.notes, self.outcomes,
                     self.closing, self.phrases, self.overrides, self.next_weekday, self.high_balance):
            try:
                step()
            except Exception as e:  # a broken scorer step is a finding, not a crash
                self.chk(step.__name__, "scorer error", "", "%s: %s" % (type(e).__name__, e), False)
        return self.rows

    # sections ----------------------------------------------------
    def variables(self):
        names = {v["name"] for v in self.p["variables"]}
        for v in self.f.get("variables") or []:
            self.chk("variables", v, True, v in names)

    def clock(self):
        c = self.f.get("clock") or {}
        self.chk("clock", "now_variable", c.get("now_variable"), self.p["clock"]["now_variable"])
        if c.get("date_format"):
            got = self.p["clock"]["date_format"]
            self.chk("clock", "date_format", c["date_format"], got,
                     _norm_text(c["date_format"]).replace(" ", "") in _norm_text(got).replace(" ", ""))

    def calendar(self):
        g = self.f["calendar"]
        cal = self.p["calendar"]
        self.chk("calendar", "weekend", sorted(g["weekend"]), sorted(cal["weekend"]))
        self.chk("calendar", "holidays", sorted(g["holidays"]), sorted(cal["holidays"]))
        for d in (g.get("not_holidays") or []) + (g.get("non_holidays") or []):
            self.chk("calendar", "not holiday %s" % d, False, d in cal["holidays"])
        if g.get("week_starts") is not None:
            self.chk("calendar", "week_starts", g["week_starts"], cal.get("week_starts"))

    def opening(self):
        g = self.f.get("opening") or {}
        forb = _norm_text(" | ".join(self.p["opening"]["forbidden_before_verification"]))
        entries = self.p["opening"]["forbidden_before_verification"]
        for w in g.get("forbidden_before_verification") or []:
            self.chk("opening", "forbidden %r" % w, True, _norm_text(w) in forb or _tokens_in(w, entries))
        if g.get("confirm_question"):
            self.chk("opening", "confirm_question", g["confirm_question"], None,
                     _norm_text(g["confirm_question"]).rstrip("?") in self.text)

    def identity(self):
        g = self.f["identity"]
        idn = self.p["identity"]
        self.chk("identity", "question order", [q["tool_field"] for q in g["questions"]],
                 [q["tool_field"] for q in idn["questions"]])
        byf = {q["tool_field"]: q for q in idn["questions"]}
        for q in g["questions"]:
            got = byf.get(q["tool_field"], {})
            self.chk("identity", "%s answer_format" % q["tool_field"], q["answer_format"], got.get("answer_format"))
            if q.get("normalise") in policy_schema.NORMALISERS:
                self.chk("identity", "%s normalise" % q["tool_field"], q["normalise"], got.get("normalise"))
            elif q.get("tool_format"):
                blob = _norm_text(json.dumps(got))
                self.chk("identity", "%s tool format" % q["tool_field"], q["tool_format"],
                         got.get("normalise"), _norm_text(q["tool_format"]) in blob)
        self.chk("identity", "max_wrong", g["max_wrong"], idn["max_wrong"])
        per_q = "per question" in (g.get("max_wrong_scope") or "")
        scope = str(idn.get("max_wrong_scope") or "total")
        self.chk("identity", "max_wrong_scope", "per_question" if per_q else "total", scope)
        fail = (g.get("fail") or {})
        self.chk("identity", "fail", (fail.get("outcome"), fail.get("reason") or ""),
                 (idn["fail"]["outcome"], idn["fail"]["reason"] or ""))
        dec = g.get("declined") or {}
        if dec:
            pairs = {(r["outcome"], r["reason"]) for r in self.p["outcomes"]["table"]}
            pairs |= {(t["outcome"], t["reason"]) for t in self.p["triggers"] if t["outcome"]}
            self.chk("identity", "declined pair", (dec.get("outcome"), dec.get("reason") or ""),
                     None, (dec.get("outcome"), dec.get("reason") or "") in pairs)

    def products(self):
        g = self.f["products"]
        pr = self.p["products"]
        self.chk("products", "max_count", g["max_count"], pr["max_count"])
        for k in ("list_field", "type_field", "days_late_field", "id_field"):
            if g.get(k):
                self.chk("products", k, g[k], pr.get(k))
        if g.get("days_field"):
            self.chk("products", "days_late_field", g["days_field"], pr.get("days_late_field"))
        for t, fld in (g.get("floor_field") or {}).items():
            got = (pr["types"].get(t) or {})
            if _gold_field(fld):
                f = _gold_field(fld)
                self.chk("products", "floor %s" % t, f, got.get("floor_field") or got.get("floor_expr"))
            else:
                self.chk("products", "floor %s (expr)" % t, fld, got.get("floor_expr") or got.get("floor_field"),
                         bool(got.get("floor_expr")))
        full_gold = g.get("past_due_field") or g.get("full_field")
        if full_gold:
            for t, info in pr["types"].items():
                self.chk("products", "full %s" % t, full_gold, info.get("full_field"))
        for t, s in (g.get("speak_as") or {}).items():
            got = (pr["types"].get(t) or {}).get("speak_as", "")
            self.chk("products", "speak_as %s" % t, s, got, _norm_text(s).replace("your ", "") in _norm_text(got)
                     or _norm_text(got) in _norm_text(s))

    def bands(self):
        gb = sorted(self.f["bands"], key=lambda b: b["min_days_late"])
        pb = self.p["bands"]
        self.chk("bands", "count", len(gb), len(pb))
        for g, b in zip(gb, pb):
            nm = g["name"]
            self.chk("bands", "%s range" % nm, (g["min_days_late"], g["max_days_late"]),
                     (b["min_days_late"], b["max_days_late"]))
            self.chk("bands", "%s deadline n" % nm, g["deadline"]["n"], b["deadline"]["n"])
            self.chk("bands", "%s counted_from" % nm, _gold_counted(g["deadline"]["counted_from"]),
                     _counted_semantics(b["deadline"]["counted_from"]))
            self.chk("bands", "%s attempts" % nm, g["attempts"], len(b["attempts"]))

    def _days_late(self, ex):
        gf = (self.f.get("products") or {}).get("days_field")
        for k in ("days_late", gf, "days_in_arrears", "days_overdue"):
            if k and ex.get(k) is not None:
                return ex[k]
        return None

    def _raw_product(self, ex):
        case = self.cases.get(ex["case"]) or {}
        dl_field = self.p["products"]["days_late_field"]
        dl = self._days_late(ex)
        prods = case.get("products") or []
        m = re.match(r"^p(\d+)$", str(ex.get("product") or ""))
        if m and 0 < int(m.group(1)) <= len(prods):
            return prods[int(m.group(1)) - 1]
        for raw in prods:
            if ex.get("product") is not None and ex.get("product") in raw.values():
                return raw
        for raw in prods:
            for k in (dl_field, "days_late", "days_overdue", "days_in_arrears"):
                if raw.get(k) == dl:
                    return raw
        return {dl_field: dl}

    def examples(self):
        with _WeekStart(self.p["calendar"].get("week_starts")):
            self._examples()

    def _examples(self):
        gbands = {}
        for b in self.f["bands"]:   # band names may repeat (same name, different ranges)
            gbands.setdefault(b["name"], []).append(b)
        cal = oracle.make_calendar(self.p)
        for ex in self.f.get("deadline_examples") or []:
            ex = dict(ex, days_late=self._days_late(ex))
            tag = "%s dl=%s" % (ex["case"], ex["days_late"])
            today, _t = oracle.parse_now(ex["now"])
            raw = self._raw_product(ex)
            prod = oracle.build_product(raw, 0, self.p, today, cal)
            cands = gbands.get(ex["band"]) or [{}]
            gb = next((b for b in cands if b.get("min_days_late", 0) <= ex["days_late"] <= b.get("max_days_late", 0)),
                      cands[0])
            pband = oracle.band_for(ex["days_late"], self.p) or {}
            self.chk("examples", tag + " band", (gb.get("min_days_late"), gb.get("max_days_late")),
                     (pband.get("min_days_late"), pband.get("max_days_late")))
            self.chk("examples", tag + " deadline", ex["deadline"], prod["deadline"])
            offers = {o["n"]: o for o in prod["offers"]}
            self.chk("examples", tag + " attempt 1", ex["attempt_1_date"], (offers.get(1) or {}).get("date"))
            if "attempt_3_date" in ex:
                self.chk("examples", tag + " attempt 3", ex["attempt_3_date"] or None,
                         (offers.get(3) or {}).get("date") or None)
            if ex.get("attempts") is not None:
                self.chk("examples", tag + " attempts", ex["attempts"], len(prod["offers"]))
            if "full" in ex:
                self.chk("examples", tag + " full", ex["full"], prod["full"],
                         prod["full"] is not None and abs(float(prod["full"]) - float(ex["full"])) < 0.005)
            if "floor" in ex:
                floor = prod["floor"]
                tinfo = (self.p["products"]["types"].get(prod["type"]) or {})
                if tinfo.get("floor_expr"):
                    # oracle reads floor_field only; the extracted rule is evaluated with the extractor helper
                    floor = extract.eval_amount_expr(tinfo["floor_expr"], raw)
                self.chk("examples", tag + " floor", ex["floor"], floor,
                         floor is not None and abs(float(floor) - float(ex["floor"])) < 0.005)

    def callback(self):
        g = self.f["callback"]
        cb = self.p["callback"]
        for k in ("start", "end", "end_inclusive", "must_be_after_now"):
            self.chk("callback", k, g[k], cb.get(k))
        self.chk("callback", "days", sorted(_day_kw(d) for d in g["days"]), sorted(_day_kw(d) for d in cb["days"]))
        for wd, win in (g.get("per_weekday") or {}).items():
            got = (cb.get("per_weekday") or {}).get(str(wd)) or (cb.get("per_weekday") or {}).get(int(wd)) or {}
            self.chk("callback", "weekday %s window" % wd, (win["start"], win["end"]),
                     (got.get("start"), got.get("end")))
            if "end_inclusive" in win:
                self.chk("callback", "weekday %s end_inclusive" % wd, win["end_inclusive"],
                         got.get("end_inclusive", cb.get("end_inclusive")))
        for day, win in (g.get("per_date") or {}).items():
            got = (cb.get("per_date") or {}).get(day) or {}
            self.chk("callback", "date %s window" % day, (win["start"], win["end"]),
                     (got.get("start"), got.get("end")))
        if "retry_asks" in g:
            pass  # free-text in notes; not scored
        for key in ("ok", "fail"):
            if g.get(key):
                want = (g[key][0], g[key][1] or "")
                got = self.p["closing"]["callback" if key == "ok" else "no_callback"]
                self.chk("callback", "close " + key, want, (got["outcome"], got["reason"]))

    def queues(self):
        qs = {t["queue"] for t in self.p["triggers"] if t["queue"]} | set(self.p["closing"]["transfer"])
        for k, q in (self.f.get("queues") or {}).items():
            self.chk("queues", k, q, sorted(qs), q in qs)

    def notes(self):
        if self.f.get("note_reasons"):
            self.chk("notes", "tool_reasons", sorted(self.f["note_reasons"]), sorted(self.p["notes"]["tool_reasons"]))
        nt = self.f.get("notes_on_transfer")
        if isinstance(nt, bool):
            self.chk("notes", "forbidden_on_transfer", not nt, self.p["notes"]["forbidden_on_transfer"])

    def outcomes(self):
        gold = _pairs_from_close_reasons(self.f.get("close_reasons"))
        got = {(r["outcome"], r["reason"]) for r in self.p["outcomes"]["table"]}
        self.chk("outcomes", "missing pairs", [], sorted(gold - got), not (gold - got))
        self.chk("outcomes", "extra pairs", [], sorted(got - gold), not (got - gold))

    def closing(self):
        mp = self.f.get("multi_product_closing") or {}
        nm = self.p["closing"]["note_map"]
        rows = self.p["outcomes"]["table"]
        no_reason = {r["outcome"] for r in rows if not r["reason"]}
        for note, close in (mp.get("note_to_close_reason") or {}).items():
            got = nm.get(note) or {}
            ok = got.get("reason") == close or (close in no_reason and got.get("outcome") == close)
            self.chk("closing", "note %s ->" % note, close, (got.get("outcome"), got.get("reason")), ok)
        g = self.f["identity"].get("fail") or {}
        c = self.p["closing"]["id_fail"]
        self.chk("closing", "id_fail", (g.get("outcome"), g.get("reason") or ""), (c["outcome"], c["reason"]))

    def phrases(self):
        for k, s in (self.f.get("phrases") or {}).items():
            self.chk("phrases", k, s, None, _norm_text(s).rstrip(".") in self.text)

    def overrides(self):
        g = self.f.get("overrides") or []
        if g:
            self.chk("overrides", "count", len(g), len(self.p["source"]["overrides_applied"]),
                     len(self.p["source"]["overrides_applied"]) >= len(g))

    def next_weekday(self):
        r = self.f.get("next_weekday_rule") or ""
        if not r:
            return
        want = "following_week" if "following" in r else "next_occurrence"
        self.chk("dates", "next_weekday", want, self.p["dates"].get("next_weekday"))

    def high_balance(self):
        h = self.f.get("high_balance_rule")
        if not h:
            return
        trg = [t for t in self.p["triggers"] if t["queue"] == h["queue"]]
        self.chk("high_balance", "trigger queue", h["queue"], [t["name"] for t in trg], bool(trg))
        if trg:
            self.chk("high_balance", "close", (h["outcome"], h["reason"]), (trg[0]["outcome"], trg[0]["reason"]))
            cond = trg[0].get("condition") or {}
            self.chk("high_balance", "condition", (h["threshold"], "all_products_sum", ">"),
                     (cond.get("value"), cond.get("over"), cond.get("op")))


# ------------------------------------------------------------------ driver

def bank_inputs(bank):
    d = FIX / bank
    facts = json.loads((d / "facts.json").read_text())
    briefs = [d / f for f in facts["brief_files"]]
    return facts, briefs, d / facts.get("openapi", "openapi.json"), json.loads((d / "cases.json").read_text())


def run_bank(bank, do_extract=True):
    facts, briefs, openapi, cases = bank_inputs(bank)
    out = OUT / ("%s.policy.json" % bank)
    secs = None
    warnings = []
    if do_extract:
        t0 = time.time()
        policy = extract.extract_policy([str(b) for b in briefs], str(openapi), warnings=warnings)
        secs = time.time() - t0
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(policy, indent=1, ensure_ascii=False) + "\n")
    else:
        policy = json.loads(out.read_text())
    rows = Scorer(bank, facts, policy, cases).run()
    report = {"bank": bank, "seconds": secs, "warnings": warnings,
              "passed": sum(1 for r in rows if r[2]), "total": len(rows),
              "rows": [{"section": s, "check": n, "ok": ok, "gold": g, "got": v} for s, n, ok, g, v in rows]}
    (OUT / ("%s.eval.json" % bank)).write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n")
    return report


def print_reports(reports):
    sections = []
    for r in reports:
        for row in r["rows"]:
            if row["section"] not in sections:
                sections.append(row["section"])
    head = "%-13s" % "section" + "".join("%12s" % r["bank"] for r in reports)
    print(head)
    for s in sections:
        line = "%-13s" % s
        for r in reports:
            rows = [x for x in r["rows"] if x["section"] == s]
            line += "%12s" % ("%d/%d" % (sum(x["ok"] for x in rows), len(rows)) if rows else "-")
        print(line)
    print("%-13s" % "TOTAL" + "".join("%12s" % ("%d/%d" % (r["passed"], r["total"])) for r in reports))
    print("%-13s" % "seconds" + "".join("%12s" % ("%.0f" % r["seconds"] if r["seconds"] else "-") for r in reports))
    for r in reports:
        bad = [x for x in r["rows"] if not x["ok"]]
        if bad:
            print("\n%s mismatches (%d):" % (r["bank"], len(bad)))
            for x in bad:
                print("  [%s] %s: gold=%s got=%s" % (x["section"], x["check"], json.dumps(x["gold"], default=str)[:150],
                                                     json.dumps(x["got"], default=str)[:200]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("banks", nargs="*", default=["S1", "S2", "S3", "S4"])
    ap.add_argument("--score-only", action="store_true", help="score saved policies, no model calls")
    ap.add_argument("--parallel", type=int, default=2)
    args = ap.parse_args(argv)
    if args.score_only:
        reports = [run_bank(b, False) for b in args.banks]
    else:
        with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
            reports = list(pool.map(lambda b: run_bank(b, True), args.banks))
    print_reports(reports)
    return 0


if __name__ == "__main__":
    sys.exit(main())
