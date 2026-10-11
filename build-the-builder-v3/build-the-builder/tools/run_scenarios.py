"""Run a scenario file against an agent: one persona per scenario, its own call variables, pass/fail from the judge.

    python tools/run_scenarios.py <agent_id> --cases <file.json> [--only id1,id2] [--reps 1] [--language tr|auto|none]
        [--out results/x.json] [--tx-dir results/tx-x/] [--force-cancel-others] [--no-empty-retry] [--raw-persona] [--dry]

A scenario file is a JSON list of objects:
    {"id": "S-01", "family": "Refusal", "persona": "<instructions for the simulated caller, any language>",
     "criteria": ["<success criterion>", ...], "variables": {"customer_id": "c_1"}, "customerNumber": "+90...", "now": "..."}
Every other top-level key with a scalar value (customerNumber, now, first_name ...) is a call variable as well; on a clash
the value inside "variables" wins. Values are sent as strings (numbers as written, booleans true/false, objects as JSON).

How the variables reach the agent: every scenario gets its own persona, and the simulation is started with
customerVariablesMap = {persona_id: call variables of that scenario} (POST /api/v2/simulations, docs/api.md). The agent
reads them as {{name}} in prompts and vars.get("name") in code. After the call the runner compares them with the call's
variable table and lists in vars_missing any name it did not see there.

Language: --language tr adds "Speak only Turkish." to the persona instructions; the default "auto" does that when the
persona text is Turkish; "none" never adds a language line.

Verdict: the simulation's judge model checks the scenario's criteria; a call passes when its run outcome is "pass" (the
call's own analysis is the fallback when the run has no outcome). No bank ledger, no score.py. A call with no agent and
no user turn (or no call at all) is infra, not a verdict: those ids are re-run once in a new simulation (--no-empty-retry).
Retries/backoff, the one-simulation-per-workspace wait and the cancel-on-abort come from run_cases.py and freya.py.
--force-cancel-others cancels every running simulation of the workspace before each simulation this run starts.
Needs FREYA_API_KEY (source env.sh), except with --dry, which only prints personas and variables.
"""
import argparse, collections, concurrent.futures as cf, hashlib, json, pathlib, re, sys, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import freya                      # noqa: E402
import run_cases as rc            # noqa: E402  (importing it wraps freya.api with retry/backoff)
import turn_latency               # noqa: E402

RESERVED = ("id", "family", "persona", "criteria", "variables")       # every other top-level scalar key is a call variable
LANGUAGES = {"tr": "Turkish", "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "nl": "Dutch", "ar": "Arabic"}
_TURKISH = re.compile(r"[ğĞşŞıİ]")
STORE = ROOT / "results" / ".personas-scenarios.json"                  # results/ is git-ignored

HEAD = "OUTPUT RULE: Write only the words you say on the phone. No stage directions, no brackets. One or two short sentences per turn, then wait for the other side."
RULES = """RULES:
- Answer what you are asked. Do not volunteer information nobody asked for.
- When you give a number or a date, say all of it in one turn unless the text above says otherwise.
- Do not invent facts that are not written above. If asked something that is not covered, give a short neutral answer.
- When the other side says goodbye, say goodbye and say nothing more."""


# ---- scenario -> call variables, persona -------------------------------------------------------------------------

def _scalar(v): return isinstance(v, (str, int, float, bool))

def as_var(v):
    """A call variable value as a string."""
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, str): return v
    if isinstance(v, (int, float)): return str(v)
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))

def call_variables(s):
    """Call variables of one scenario: top-level scalar keys outside RESERVED, then "variables" (wins on a clash)."""
    out = {str(k): as_var(v) for k, v in s.items() if k not in RESERVED and _scalar(v)}
    for k, v in (s.get("variables") or {}).items():
        if v is not None: out[str(k)] = as_var(v)
    return out

def criteria_list(s):
    c = s.get("criteria")
    if isinstance(c, str): c = [c]
    return [" ".join(str(x).split()) for x in (c or []) if str(x).strip()]

def language_of(text, language="auto"):
    """Language code the simulated caller must speak: --language as given, or for "auto" tr when the persona text is Turkish."""
    lang = str(language or "auto").strip().lower()
    if lang in ("none", "off", "no", ""): return None
    if lang == "auto": return "tr" if _TURKISH.search(text or "") else None
    return lang

def language_line(lang):
    return f"LANGUAGE: Speak only {LANGUAGES.get(lang, lang)}." if lang else ""

def persona_spec(s, language="auto", raw=False):
    """The persona to create for a scenario. The name carries the scenario id, so two scenarios never share a persona
    (customerVariablesMap is keyed by persona id: a shared persona would mix up their variables)."""
    text = str(s.get("persona") or "").strip()
    lang = language_of(text, language)
    parts = [text, language_line(lang)] if raw else [HEAD, language_line(lang), "WHO YOU ARE AND WHAT HAPPENS IN THIS CALL:\n" + text, RULES]
    name = str(s["id"]) + (f" {s['family']}" if s.get("family") else "")
    return {"name": name[:120], "prompt": "\n\n".join(p for p in parts if p),
            "successCriteria": "All of these are true:\n- " + "\n- ".join(criteria_list(s)),
            "tags": ["scenario"] + ([f"lang-{lang}"] if lang else [])}

def validate(scenarios):
    """Problems of the selected scenarios, one line each (empty list = fine)."""
    if not isinstance(scenarios, list): return ["the scenario file must hold a JSON list of objects"]
    errs, seen = [], set()
    for k, s in enumerate(scenarios):
        if not isinstance(s, dict): errs.append(f"item {k}: not an object"); continue
        sid = s.get("id")
        if sid in (None, "") or not isinstance(sid, (str, int)): errs.append(f"item {k}: no id"); continue
        sid = str(sid)
        if sid in seen: errs.append(f"{sid}: duplicate id")
        seen.add(sid)
        if not str(s.get("persona") or "").strip(): errs.append(f"{sid}: no persona text")
        if not criteria_list(s): errs.append(f"{sid}: no criteria")
        if s.get("variables") is not None and not isinstance(s["variables"], dict): errs.append(f"{sid}: variables is not an object")
    return errs

def select(data, only=None):
    """The scenarios to run (file order), ids as strings. Raises ValueError listing every problem of the selection."""
    if isinstance(data, dict): data = data.get("scenarios") or data.get("cases") or data.get("items") or data
    if only and isinstance(data, list):
        want = [x.strip() for x in only.split(",") if x.strip()]
        have = {str(s.get("id")) for s in data if isinstance(s, dict)}
        unknown = [w for w in want if w not in have]
        if unknown: print("-- unknown ids in --only (skipped):", ", ".join(unknown), flush=True)
        data = [s for s in data if isinstance(s, dict) and str(s.get("id")) in want]
    errs = validate(data)
    if errs: raise ValueError("scenario file problems:\n  " + "\n  ".join(errs))
    return [dict(s, id=str(s["id"])) for s in data]

def spec_key(sid, spec): return sid + ":" + hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]

def persona_ids(scenarios, specs, store=STORE, create=None):
    """{scenario id: persona id}. A persona is created (in batches of 100) only when its text is new or changed."""
    store = pathlib.Path(store); ids = json.loads(store.read_text()) if store.exists() else {}
    key = {s["id"]: spec_key(s["id"], specs[s["id"]]) for s in scenarios}
    new = [s for s in scenarios if key[s["id"]] not in ids]
    if new:
        made = rc.create_personas_batched([specs[s["id"]] for s in new], create=create)
        fresh = json.loads(store.read_text()) if store.exists() else {}      # keep what another run wrote meanwhile
        fresh.update(ids); fresh.update({key[s["id"]]: p["id"] for s, p in zip(new, made)}); ids = fresh
        store.parent.mkdir(parents=True, exist_ok=True); store.write_text(json.dumps(ids, indent=1))
    return {s["id"]: ids[key[s["id"]]] for s in scenarios}


# ---- reading a finished call ----------------------------------------------------------------------------------------

def _flat(text, n=600): return " ".join(str(text).split())[:n]

def verdict(run, call=None):
    """(passed, outcome, reason) from the simulation run of the call; the call's own analysis when the run has no outcome."""
    run, call = run or {}, call or {}
    ev = (call.get("analysis") or {}).get("successEvaluation") or {}
    scr = call.get("successCriteriaResult") or {}
    outcome = str(run.get("outcome") or "").strip().lower() or None
    if outcome not in ("pass", "fail", "inconclusive", "error"):
        ok = ev.get("success")
        if not isinstance(ok, bool): ok = scr.get("success")
        if isinstance(ok, bool): outcome = "pass" if ok else "fail"
    reason = run.get("reason") or ev.get("reason") or scr.get("reason") or ("no verdict" if not outcome else outcome)
    return outcome == "pass", outcome or "none", _flat(reason)

def seen_variables(call):
    """Names in the call's variable tables (assistant workflowSnapshot, route_evaluated events)."""
    names = set()
    for m in (call or {}).get("messages") or []:
        vt = (m.get("workflowSnapshot") or {}).get("variableTable")
        if isinstance(vt, dict): names |= set(vt)
        if m.get("role") == "system" and "variable_table" in str(m.get("message") or ""):
            try: e = json.loads(m.get("message") or "")
            except Exception: continue
            if isinstance(e, dict) and isinstance(e.get("variable_table"), dict): names |= set(e["variable_table"])
    return names

def missing_variables(call, variables):
    """Passed variables the call's variable table does not show; None when the call has no variable table at all."""
    seen = seen_variables(call)
    return sorted(k for k in variables if k not in seen) if seen else None

def slowest_turn(call):
    """Longest user message -> agent finished, in seconds (tools/turn_latency.py)."""
    try: return round(max([t["total"] for t in turn_latency.analyse(call)] or [0.0]), 1)
    except Exception: return None

def make_row(s, run, call, variables, fetch_error=None):
    passed, outcome, reason = verdict(run, call)
    call_id = (run or {}).get("callId")
    row = {"id": s["id"], "family": s.get("family"), "pass": passed, "passed": passed, "outcome": outcome, "reason": reason,
           "failed": [] if passed else ["judge: " + reason], "call_id": call_id, "empty": not call_id, "variables": variables}
    if call is not None:
        row["empty"] = rc.is_empty_call(call); row["slowest_turn_s"] = slowest_turn(call); row["ended"] = call.get("endedReason")
        row["turns"] = sum(1 for m in call.get("messages") or [] if m.get("role") in ("assistant", "user"))
        row["vars_missing"] = missing_variables(call, variables)
    if fetch_error: row["call_error"] = fetch_error[:200]
    return row

def transcript(s, row, call):
    """Readable transcript: verdict, criteria, variables, then speech, nodes, tool calls and results."""
    lines = [f"#### {s['id']}  family={s.get('family')}  {'PASS' if row['pass'] else 'FAIL'} ({row['outcome']})  call {row.get('call_id')}"
             f"  ended={(call or {}).get('endedReason')}  slowest_turn={row.get('slowest_turn_s')} s",
             "judge: " + row["reason"], "criteria:"] + ["  - " + c for c in criteria_list(s)]
    lines.append("variables: " + json.dumps(row.get("variables") or {}, ensure_ascii=False))
    if row.get("vars_missing"): lines.append("variables NOT seen in the call: " + ", ".join(row["vars_missing"]))
    lines.append("")
    if call is None: lines.append("(no call)" + (f" {row['call_error']}" if row.get("call_error") else ""))
    for m in (call or {}).get("messages") or []:
        role, t = m.get("role"), m.get("message") or ""
        if role in ("assistant", "user"): lines.append(f"{'A' if role == 'assistant' else 'U'}: {t}"); continue
        try: e = json.loads(t)
        except Exception: continue
        if not isinstance(e, dict): continue
        ev = e.get("event")
        if ev == "node_enter": lines.append(f"   [node {e.get('node')}]")
        elif ev == "function_call_start":
            a = dict(e.get("args") or {}); a.pop("code", None); a.pop("timeout_ms", None)
            p = a.get("params") if isinstance(a.get("params"), dict) else a
            lines.append(f"   >> {e.get('function')} {json.dumps(p, ensure_ascii=False, default=str)[:300]}")
        elif ev in ("function_call_complete", "function_call_error"):
            res = e.get("result") if ev == "function_call_complete" else e.get("error", e.get("result"))
            data = res.get("data", res) if isinstance(res, dict) else res
            lines.append(f"   << {e.get('function')}{' ERROR' if ev == 'function_call_error' else ''} {json.dumps(data, ensure_ascii=False, default=str)[:400]}")
    return "\n".join(lines) + "\n"

def print_row(r):
    extra = (["EMPTY"] if r.get("empty") else []) + ([f"slowest turn {r['slowest_turn_s']:.0f} s"] if r.get("slowest_turn_s") is not None else [])
    if r.get("vars_missing"): extra.append("variables not seen in the call: " + ",".join(r["vars_missing"]))
    print("PASS" if r["pass"] else "FAIL", r["id"], f"{str(r.get('family') or '-'):<14}", "|", "; ".join(extra) or "-", flush=True)
    if not r["pass"]: print("       -", r["reason"][:260], flush=True)


# ---- running ----------------------------------------------------------------------------------------------------------

def fetch_calls(call_ids, workers=8):
    """{scenario id: (call or None, error or None)}, read in parallel."""
    def one(cid):
        if not cid: return None, None
        try: return freya.get_call(cid), None
        except Exception as e: return None, str(e)[:200]
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {sid: ex.submit(one, cid) for sid, cid in call_ids.items()}
        return {sid: f.result() for sid, f in futs.items()}

def run_once(agent_id, scenarios, ids, variables, force_cancel=False, max_seconds=420, tx_dir=None, suffix=""):
    """One simulation, every scenario once. Returns (rows, simulation id, iteration id, status, seconds)."""
    if force_cancel: rc.cancel_others()
    before, t0 = freya.LAST_SIM, time.time()
    try: sim_id, it_id, status = freya.run_simulation(agent_id, f"scenarios {time.strftime('%H:%M:%S')}", {ids[s["id"]]: variables[s["id"]] for s in scenarios}, max_seconds=max_seconds)
    except BaseException:                                  # our simulation is orphaned (error, Ctrl-C): free the workspace
        if freya.LAST_SIM and freya.LAST_SIM != before: print(f"-- run aborted: cancelling our simulation {freya.LAST_SIM}", flush=True); freya.cancel_simulation(freya.LAST_SIM)
        raise
    took = time.time() - t0
    judge = {r.get("personaId"): r for r in (freya.get_iteration(sim_id, it_id).get("runs") or [])}
    runs = {s["id"]: judge.get(ids[s["id"]]) for s in scenarios}
    calls = fetch_calls({sid: (r or {}).get("callId") for sid, r in runs.items()})
    rows = []
    for s in scenarios:
        call, err = calls.get(s["id"], (None, None))
        row = make_row(s, runs[s["id"]], call, variables[s["id"]], err); rows.append(row); print_row(row)
        if tx_dir:
            pathlib.Path(tx_dir).mkdir(parents=True, exist_ok=True)
            (pathlib.Path(tx_dir) / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', s['id'])}{suffix}.txt").write_text(transcript(s, row, call))
    return rows, sim_id, it_id, status, took

def final_rows(scenarios, reps):
    """Per scenario over all runs: it passes only when every run passed."""
    by = [{r["id"]: r for r in rows} for rows in reps]
    out = []
    for s in scenarios:
        n = sum(bool(b.get(s["id"], {}).get("pass")) for b in by)
        out.append({"id": s["id"], "family": s.get("family"), "passes": n, "runs": len(reps), "pass": n == len(reps), "passed": n == len(reps)})
    return out

def by_family(final):
    fam = collections.OrderedDict()
    for f in final:
        x = fam.setdefault(f.get("family") or "-", {"passed": 0, "total": 0}); x["passed"] += bool(f["pass"]); x["total"] += 1
    return fam

def summary(final, reps):
    """Compact summary lines: N/M, per family, failed ids with a one-line reason (from their last failed run)."""
    n = sum(f["pass"] for f in final)
    out = [f"{n} / {len(final)} passed" + (f" (a scenario counts when all {len(reps)} runs pass)" if len(reps) > 1 else ""),
           "per family: " + ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in by_family(final).items())]
    failed = [f for f in final if not f["pass"]]
    if failed: out.append("failed:")
    for f in failed:
        r = next((r for rows in reversed(reps) for r in rows if r["id"] == f["id"] and not r["pass"]), {})
        tag = f" {f['passes']}/{f['runs']}" if f["runs"] > 1 else ""
        out.append(f"  {f['id']}{tag} [{f.get('family') or '-'}] {'EMPTY ' if r.get('empty') else ''}{(r.get('reason') or '')[:180]}")
    return out

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Run a scenario file (persona, criteria, call variables) against an agent; judge verdicts only.")
    ap.add_argument("agent_id"); ap.add_argument("--cases", required=True, help="scenario JSON file (a list)")
    ap.add_argument("--only", help="comma-separated scenario ids"); ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--language", default="auto", help="tr, en, ... adds 'Speak only <language>.'; auto (default): tr when the persona text is Turkish; none")
    ap.add_argument("--out", help="results JSON (default results/scenarios-<time>.json)"); ap.add_argument("--tx-dir", help="write one transcript per call here")
    ap.add_argument("--force-cancel-others", action="store_true"); ap.add_argument("--no-empty-retry", action="store_true")
    ap.add_argument("--raw-persona", action="store_true", help="send the persona text as it is (plus the language line), without the output rules")
    ap.add_argument("--max-call-seconds", type=int, default=420); ap.add_argument("--dry", action="store_true", help="print personas and variables, no Freya calls")
    a = ap.parse_args(argv)
    if a.reps < 1: ap.error("--reps must be at least 1")
    return a

def main(argv=None):
    a = parse_args(argv)
    try: scenarios = select(json.loads(pathlib.Path(a.cases).read_text()), a.only)
    except ValueError as e: raise SystemExit(str(e))
    if not scenarios: raise SystemExit("no scenarios selected")
    specs = {s["id"]: persona_spec(s, a.language, a.raw_persona) for s in scenarios}
    variables = {s["id"]: call_variables(s) for s in scenarios}
    if a.dry:
        for s in scenarios:
            print(s["id"], f"[{s.get('family') or '-'}]", f"criteria={len(criteria_list(s))}", "tags=" + ",".join(specs[s["id"]]["tags"]), "variables=" + json.dumps(variables[s["id"]], ensure_ascii=False))
        first = specs[scenarios[0]["id"]]; print("\n--- persona of", scenarios[0]["id"], "---\n" + first["prompt"] + "\n\n--- success criteria ---\n" + first["successCriteria"]); return
    ids = persona_ids(scenarios, specs)
    reps, total_s, sim_id, it_id, status = [], 0, None, None, None
    for rep in range(a.reps):
        suffix = f".r{rep + 1}" if a.reps > 1 else ""
        rows, sim_id, it_id, status, took = run_once(a.agent_id, scenarios, ids, variables, a.force_cancel_others, a.max_call_seconds, a.tx_dir, suffix)
        empty = [r["id"] for r in rows if r.get("empty")]
        retry = []
        if empty and not a.no_empty_retry:                  # infra, not a verdict: re-run those ids once in a new simulation
            print(f"-- {len(empty)} empty call(s) (no agent or user turn): re-running once: {', '.join(empty)}", flush=True)
            retry, sim_id, it_id, status, took2 = run_once(a.agent_id, [s for s in scenarios if s["id"] in empty], ids, variables, a.force_cancel_others, a.max_call_seconds, a.tx_dir, suffix)
            took += took2; print(f"-- retried_empty: {len(empty)}; still empty: {sum(1 for r in retry if r.get('empty'))}", flush=True)
        rows = rc.merge_retried(rows, retry); reps.append(rows); total_s += took
        if a.reps > 1: print(f"-- run {rep + 1} of {a.reps}: {sum(r['pass'] for r in rows)} / {len(rows)} passed, {took:.0f} s\n", flush=True)
    final = final_rows(scenarios, reps)
    n = sum(f["pass"] for f in final)
    print("\n" + "\n".join(summary(final, reps)))
    waits = sorted(r["slowest_turn_s"] for r in reps[-1] if r.get("slowest_turn_s") is not None)
    if waits:
        med = waits[len(waits) // 2]
        print(f"slowest agent turn per call: median {med:.0f} s, worst {waits[-1]:.0f} s.")
        if med > 10: print("WARNING: the phone agent was slow in this run; the model may have been busy.")
    missing = {r["id"]: r["vars_missing"] for r in reps[-1] if r.get("vars_missing")}
    if missing: print("WARNING: variables not seen in the call's variable table:", "; ".join(f"{k}: {','.join(v)}" for k, v in missing.items()))
    out = pathlib.Path(a.out) if a.out else ROOT / "results" / f"scenarios-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"agent_id": a.agent_id, "cases": a.cases, "language": a.language, "simulation_id": sim_id, "iteration_id": it_id,
                               "status": status, "seconds": round(total_s), "passed": n, "total": len(final), "runs": a.reps, "by_family": by_family(final),
                               "failed": [f["id"] for f in final if not f["pass"]], "final": final, "rows": reps[-1], "all_rows": reps},
                              indent=1, ensure_ascii=False))
    print(f"Last simulation {sim_id}, status {status}, {total_s:.0f} s. Results: {out}" + (f", transcripts: {a.tx_dir}" if a.tx_dir else ""))

if __name__ == "__main__": main()
