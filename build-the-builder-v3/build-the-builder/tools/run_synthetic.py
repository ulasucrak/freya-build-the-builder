"""Run cases of any bank (synthetic fixtures or the kit's own) against a workflow in TEST MODE: the bank tools are
replaced by a fake bank that runs inside Freya (builder/testmode.py), so cases with their own identity, products,
enums and faults work end to end on the real engine and the real AGENT_LLM.

    python tools/run_synthetic.py <workflow.json> --cases fixtures/<bank>/cases.json --openapi fixtures/<bank>/openapi.json \
        [--slot dev-T10-x] [--only PUB-01,PUB-03] [--reps N] [--style talkative] [--out results/x.json] [--facts facts.json]

Wrap it in the simulation lock: python3 ~/.claude/orchestra/orch.py with-lock db -- python tools/run_synthetic.py ...

Needs FREYA_API_KEY. With FREYA_BANK_TEAM_KEY set, end_of_call lifecycle bank tools (whose results are invisible
afterwards) are pointed at a throw-away practice session of the kit's bank mock and read back from its ledger;
without it (or with --no-sink) they run the fake bank too and are reported as not observable.
"""
import argparse, hashlib, json, os, pathlib, re, sys, time, uuid
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import freya                                     # noqa: E402
from score import HOST, http, score_ledger       # noqa: E402
from run_cases import STYLES, persona_for        # noqa: E402
from builder import testmode as tm               # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent / "fakebank"
_ENTRY = re.compile(r"_fb_tool='([A-Za-z0-9_]+)'|return _fb_entry\('([A-Za-z0-9_]+)'")


def load_workflow(path):
    w = json.load(open(path))
    return w["workflow"] if isinstance(w, dict) and isinstance(w.get("workflow"), dict) and "nodes" in w["workflow"] else w


def find_facts(cases_path, facts_path=None):
    p = pathlib.Path(facts_path) if facts_path else pathlib.Path(cases_path).parent / "facts.json"
    if not p.exists(): return {}
    try: return json.load(open(p))
    except Exception: return {}


def _deep_get(d, keys):
    """First value found under any of `keys`, searched depth first through dicts."""
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None: return d[k]
        for v in d.values():
            r = _deep_get(v, keys)
            if r is not None: return r
    return None


def scenario_extras(cases, facts, max_wrong):
    """max_wrong and the outcome->reasons table: from the facts file if it has them, else from the cases."""
    mw = _deep_get(facts, ("max_wrong", "max_wrong_answers"))
    table = _deep_get(facts, ("close_reasons", "outcome_reasons", "reasons_by_outcome"))
    if not isinstance(table, dict):
        seen = {}
        for c in cases:
            e = c.get("expected") or {}
            seen.setdefault(e.get("outcome"), set()).add(e.get("reason"))
        table = {o: [] for o, rs in seen.items() if o and rs == {None}}       # outcomes that never carry a reason
    return {"max_wrong": int(mw) if mw is not None else max_wrong, "close_reasons": table}


def personas(cases, style):
    store = HERE / ".personas.json"; ids = json.load(open(store)) if store.exists() else {}
    spec = {c["id"]: persona_for(c, {"identity": c["identity"]}, style) for c in cases}
    for p in spec.values(): p["tags"] = list(p.get("tags") or []) + ["synthetic"]
    key = {i: i + ":" + hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10] for i, p in spec.items()}
    new = [c for c in cases if key[c["id"]] not in ids]
    if new:
        made = freya.create_personas([spec[c["id"]] for c in new])["personas"]
        ids.update({key[c["id"]]: p["id"] for c, p in zip(new, made)})
        HERE.mkdir(exist_ok=True); json.dump(ids, open(store, "w"), indent=1)
    return {c["id"]: ids[key[c["id"]]] for c in cases}


def _events(call):
    for m in call.get("messages") or []:
        if m.get("role") == "system":
            try: e = json.loads(m.get("message") or "")
            except Exception: continue
            if isinstance(e, dict): yield m, e


def _state_candidates(call, state_var):
    for m in call.get("messages") or []:
        snap = m.get("workflowSnapshot") or {}
        vt = snap.get("variableTable") or {}
        v = vt.get(state_var) if isinstance(vt, dict) else None
        if isinstance(v, dict): v = v.get("value")
        if v: yield v
    for _, e in _events(call):
        vt = e.get("variable_table")
        if isinstance(vt, dict) and vt.get(state_var): yield vt[state_var]


def state_from_call(call, state_var=tm.STATE_VAR):
    """The fake bank's last state seen in the call (the one with the most recorded calls)."""
    best = None
    for v in _state_candidates(call, state_var):
        try: st = json.loads(v) if isinstance(v, str) else v
        except Exception: continue
        if not isinstance(st, dict): continue
        n = len(st.get("calls") or st.get("L") or [])             # working form or compact stored form
        if best is None or n >= best[0]: best = (n, st)
    return best[1] if best else None


def tool_events(call):
    """[(tool, params)] of the fake-bank calls in order, from function_call_start events."""
    out = []
    for _, e in _events(call):
        if e.get("event") != "function_call_start": continue
        a = e.get("args") or {}
        m = _ENTRY.search(str(a.get("code") or ""))
        if m: out.append((m.group(1) or m.group(2), a.get("params") or {}))
    return out


def write_log_from_call(call):
    """Per-call write records (`fakebank_w_*` variables) seen anywhere in the call, and the completion order of
    the fake-bank tools (function_call_complete / _error events)."""
    entries = {}
    for m in (call or {}).get("messages") or []:
        vt = (m.get("workflowSnapshot") or {}).get("variableTable") or {}
        for k, v in (vt.items() if isinstance(vt, dict) else []):
            if k.startswith(tm.WLOG_PREFIX): entries[k] = v.get("value") if isinstance(v, dict) else v
    order = []
    for _, e in _events(call or {}):
        vt = e.get("variable_table")
        if isinstance(vt, dict):
            for k, v in vt.items():
                if k.startswith(tm.WLOG_PREFIX): entries[k] = v
        if e.get("event") in ("function_call_complete", "function_call_error"): order.append(e.get("function"))
    out = []
    for v in entries.values():
        try: out.append(json.loads(v) if isinstance(v, str) else v)
        except Exception: pass
    return out, order


def ledger_for(call, scenario, spec, sink_led=None):
    """Rebuild the bank ledger: fake-bank state from the call (or a replay of its tool events), plus writes of
    parallel tool calls that a sibling call overwrote in the state, then the end_of_call tool calls read from the
    sink session, replayed on top. Returns (ledger, source, restored_entries)."""
    st = state_from_call(call, spec["state_var"]) if call else None
    v = {spec["scenario_var"]: json.dumps(scenario)}
    source, restored = "state", []
    if st is None:
        source = "replay"
        for tool, params in tool_events(call or {}): tm.fakebank_call(tool, params, v, spec)
    else:
        entries, order = write_log_from_call(call)
        order = [t for t in order if t in spec["tools"]]
        led, restored = tm.merge_write_log(st, entries, order)
        if restored:
            source = "state+parallel"
            seen = {}
            for c in led["calls"]: seen[c["tool"]] = seen.get(c["tool"], 0) + 1
            st = dict(led, closed=any(c["tool"] == "close_call" and c["result"].get("ok") for c in led["calls"]), seen=seen)
        v[spec["state_var"]] = json.dumps(st)
    for c in (sink_led or {}).get("calls") or []:
        tm.fakebank_call(c["tool"], c.get("args") or {}, v, spec)
    return tm.ledger_from_state(v.get(spec["state_var"]) or "{}"), source, restored


def slowest_turn(call):
    last_user, worst = None, 0.0
    for m in call.get("messages", []):
        t = m.get("secondsFromStart")
        if t is None: continue
        if m.get("role") == "user": last_user = t
        elif last_user is not None:
            worst = max(worst, t - last_user); last_user = None if m.get("role") == "assistant" else last_user
    return worst


def run_once(agent_id, cases, ids, scenarios, spec, sink_case):
    variables, sinks = {}, {}
    for c in cases:
        v = {k: str(x) for k, x in (c.get("variables") or {}).items()}
        v.setdefault("customer_id", "c_syn_" + uuid.uuid4().hex[:10])
        v[spec["scenario_var"]] = json.dumps(scenarios[c["id"]])
        if sink_case:
            sinks[c["id"]] = http("/v1/practice/sessions", {"case": sink_case})["variables"]["customer_id"]
            v[tm.SINK_VAR] = sinks[c["id"]]
        variables[c["id"]] = v
    t0 = time.time()
    sim_id, it_id, status = freya.run_simulation(agent_id, f"synthetic {time.strftime('%H:%M:%S')}", {ids[c["id"]]: variables[c["id"]] for c in cases})
    took = time.time() - t0
    judge = {r["personaId"]: r for r in freya.get_iteration(sim_id, it_id)["runs"]}
    rows, waits = [], []
    for c in cases:
        j = judge.get(ids[c["id"]], {})
        call = None
        if j.get("callId"):
            try: call = freya.get_call(j["callId"])
            except Exception as e: print("  could not read call", j.get("callId"), e)
        sink_led = http("/v1/practice/ledger/" + sinks[c["id"]]) if c["id"] in sinks else None
        led, source, restored = ledger_for(call, scenarios[c["id"]], spec, sink_led)
        r = score_ledger(c, led)
        if not sink_case and not any(x["tool"] == "close_call" for x in led["calls"]):
            r["failed"].append("close_call not observable without a sink (end_of_call lifecycle tool?)")
        if j.get("outcome") != "pass": r["failed"].append("conversation: " + " ".join(str(j.get("reason") or "no verdict").split())[:400])
        row = {"id": c["id"], "level": c.get("level"), "family": c.get("family"), "pass": not r["failed"], "failed": r["failed"],
               "call_id": j.get("callId"), "tools": [x["tool"] for x in led["calls"]], "ledger_source": source, "parallel_restored": [e[0] for e in restored],
               "customer_id": variables[c["id"]].get("customer_id"), "sink_id": sinks.get(c["id"]), "ledger": led}
        if call:
            row["slowest_turn_s"] = round(slowest_turn(call), 1); row["ended"] = call.get("endedReason"); waits.append(row["slowest_turn_s"])
        rows.append(row)
        print(("PASS" if row["pass"] else "FAIL"), c["id"], f"{str(c.get('level')):<6}", f"{str(c.get('family')):<14}", "|",
              " > ".join(x["tool"].replace("verify_identity", "verify").replace("create_promise", "promise") for x in led["calls"]) or "(no tool calls)", flush=True)
        if restored: print("       ! parallel tool calls in one turn: restored", ", ".join(e[0] for e in restored), "from the per-call write log")
        for f in r["failed"]: print("       -", f[:260])
    med = 0
    if waits:
        waits.sort(); med = waits[len(waits) // 2]; hung = sum(r.get("ended") == "customer-ended-call" and not r["pass"] for r in rows)
        print(f"\nslowest agent turn per call: median {med:.0f} s, worst {waits[-1]:.0f} s. Failed calls where the customer hung up: {hung}.")
        if med > 10: print("WARNING: the phone agent was slow in this run. If the same agent is fast in another run, the model was busy and this score is too low.")
    return rows, sim_id, it_id, status, took, med


SCORE_LOG = os.environ.get("SCORE_LOG", "/Users/ulas/Desktop/Freya/build-the-builder-v3/build-the-builder/results/log.jsonl")


def append_score_log(a, bank, agent_id, reps, final, total_s):
    """One JSON line per run for the shared scoreboard (tools/scoreboard.py)."""
    import subprocess
    try: sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception: sha = None
    waits = sorted(r["slowest_turn_s"] for rows in reps for r in rows if r.get("slowest_turn_s") is not None)
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "agent_id": agent_id, "brief": bank, "runs": a.reps,
           "passed": sum(f["pass"] for f in final), "total": len(final), "seconds": round(total_s),
           "failed": [f["id"] for f in final if not f["pass"]], "source": "synthetic", "bank": bank, "slot": a.slot,
           "label": os.environ.get("RUN_LABEL", "ours"), "git_sha": sha, "reps": a.reps, "style": a.style,
           "pass_all_reps": all(f["pass"] for f in final), "median_turn_s": waits[len(waits) // 2] if waits else None,
           "worst_turn_s": waits[-1] if waits else None,
           "per_case": {f["id"]: (("PASS" if f["pass"] else "FAIL") if a.reps == 1 else f"{f['passes']}/{f['runs']}") for f in final}}
    try:
        pathlib.Path(SCORE_LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(SCORE_LOG, "a") as f: f.write(json.dumps(rec) + "\n")
    except Exception as e: print("could not append to", SCORE_LOG, e)


def bank_name(a, cases):
    if a.bank: return a.bank
    briefs = sorted({str(c.get("brief")) for c in cases if c.get("brief")})
    if len(briefs) == 1: return briefs[0]
    p = pathlib.Path(a.cases).resolve()
    return p.parent.name if p.parent != ROOT else "+".join(briefs) or "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow"); ap.add_argument("--cases", required=True); ap.add_argument("--openapi", required=True)
    ap.add_argument("--slot", default="dev-synthetic"); ap.add_argument("--only"); ap.add_argument("--brief")
    ap.add_argument("--reps", type=int, default=1); ap.add_argument("--style", choices=sorted(STYLES)); ap.add_argument("--out")
    ap.add_argument("--facts"); ap.add_argument("--max-wrong", type=int, default=3, help="used when the facts file does not say")
    ap.add_argument("--state-file", default=os.environ.get("FREYA_STATE_FILE", str(ROOT / ".freya-agents.json")))
    ap.add_argument("--no-sink", action="store_true"); ap.add_argument("--sink-case", default="PUB-01", help="stock case id used for the throw-away sink session")
    ap.add_argument("--bank", help="bank id for the score log (default: the cases' brief or the fixture dir name)")
    ap.add_argument("--dry", action="store_true", help="transform and print stats, no Freya calls")
    a = ap.parse_args()
    all_cases = json.load(open(a.cases))
    cases = [c for c in all_cases if (not a.brief or c.get("brief") == a.brief) and (not a.only or c["id"] in a.only.split(","))]
    openapi = json.load(open(a.openapi))
    extras = scenario_extras(all_cases, find_facts(a.cases, a.facts), a.max_wrong)
    scenarios = {c["id"]: tm.scenario_for_case(c, extras) for c in cases}
    use_sink = not a.no_sink and bool(os.environ.get("FREYA_BANK_TEAM_KEY"))
    sink = {"base_url": HOST.rstrip("/") + "/v1", "var": tm.SINK_VAR} if use_sink else None
    wf = tm.to_test_mode(load_workflow(a.workflow), openapi, sink=sink)
    spec = tm.spec_from_openapi(openapi)
    print(f"test mode: {tm.LAST_STATS.get('converted', 0)} bank tools faked, {tm.LAST_STATS.get('sink', 0)} end_of_call tools on the sink. "
          f"{len(cases)} cases. max_wrong={extras['max_wrong']}, reason table for {sorted(extras['close_reasons'])}", flush=True)
    if a.dry:
        print(json.dumps(wf)[:2000]); return
    info = freya.save_workflow(a.slot, wf, state_file=a.state_file)
    print(f"saved slot {a.slot}: workflow {info['workflow_id']} agent {info['agent_id']} version {info.get('version')}", flush=True)
    ids = personas(cases, a.style)
    reps, total_s = [], 0
    for rep in range(a.reps):
        rows, sim_id, it_id, status, took, med = run_once(info["agent_id"], cases, ids, scenarios, spec, a.sink_case if use_sink else None)
        reps.append(rows); total_s += took
        if a.reps > 1: print(f"-- run {rep + 1} of {a.reps}: {sum(r['pass'] for r in rows)} / {len(rows)} passed, {took:.0f} s\n")
    final = [{"id": c["id"], "family": c.get("family"), "passes": sum(r[k]["pass"] for r in reps), "runs": a.reps,
              "pass": all(r[k]["pass"] for r in reps)} for k, c in enumerate(cases)]
    n = sum(f["pass"] for f in final)
    if a.reps > 1:
        for f in final: print(("PASS" if f["pass"] else "FAIL"), f["id"], f"{f['passes']}/{f['runs']}")
    print(f"\n{n} / {len(final)} passed (fake-bank ledger and conversation checks). Last simulation {sim_id}, status {status}, {total_s:.0f} s.")
    append_score_log(a, bank_name(a, cases), info["agent_id"], reps, final, total_s)
    if a.out:
        pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        json.dump({"agent_id": info["agent_id"], "workflow_id": info["workflow_id"], "simulation_id": sim_id, "iteration_id": it_id,
                   "seconds": round(total_s), "passed": n, "total": len(final), "runs": a.reps, "final": final, "rows": reps[-1], "all_rows": reps},
                  open(a.out, "w"), indent=1)


if __name__ == "__main__": main()
