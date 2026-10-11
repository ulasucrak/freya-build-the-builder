"""SELF-TEST: run synthetic cases against a workflow in TEST MODE (fake bank inside Freya, tools/run_synthetic.py
and builder/testmode.py) and collect PASS/FAIL per case with reasons and transcript excerpts for the repair step.

    st = SelfTester(openapi_path, test_slot, state_file, trace=trace, policy=policy)
    res = st.run(workflow, cases, label="self-test 1", timeout_s=600)
    res -> {"ok", "passed", "total", "rows", "duration_s", "error", "simulation_id"}

The test-mode copy is saved to its own slot (`<slot>-selftest`), never over the real phone agent.
One simulation runs at a time per workspace. During development, ORCH_SIM_LOCK=1 takes the orchestra db lock
around each simulation only (SimLock); never wrap the whole build in the lock. The final run uses no lock.
"""
import json
import math
import os
import pathlib
import sys
import threading
import time

from builder.trace import run_with_timeout

KIT = pathlib.Path(os.path.abspath(__file__)).parent.parent
SIM_OVERHEAD_S = 60          # personas, save, start-up, scoring reads
SIM_WAVE_S = 220             # one wave of concurrent calls (live: 10 cases in 2 waves took ~410 s)


def sim_concurrency():
    try:
        return max(1, int(os.environ.get("FREYA_SIM_CONCURRENCY", "5")))
    except ValueError:
        return 5


def estimate_sim_s(n_cases, concurrency=None):
    """Conservative wall time of one simulation of n cases."""
    if n_cases <= 0:
        return 0.0
    c = concurrency or sim_concurrency()
    return SIM_OVERHEAD_S + math.ceil(n_cases / float(c)) * SIM_WAVE_S


def max_cases_within(seconds, concurrency=None):
    """Largest case count whose estimated simulation fits in `seconds` (0 when none)."""
    c = concurrency or sim_concurrency()
    waves = int((seconds - SIM_OVERHEAD_S) // SIM_WAVE_S)
    return max(0, waves * c)


def close_reasons(policy):
    """{outcome: [reasons]} from the policy's outcome table (for the fake bank's close_call check)."""
    out = {}
    for r in ((policy or {}).get("outcomes") or {}).get("table") or []:
        if isinstance(r, dict) and r.get("outcome"):
            out.setdefault(str(r["outcome"]), [])
            if r.get("reason"):
                for x in str(r["reason"]).replace("/", ",").split(","):
                    x = x.strip().strip("`")
                    if x and x not in out[str(r["outcome"])]:
                        out[str(r["outcome"])].append(x)
    return out


def _import_runner():
    for p in (str(KIT), str(KIT / "tools")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import run_synthetic          # noqa: E402  (tools/run_synthetic.py)
    from builder import testmode  # noqa: E402
    import freya                  # noqa: E402
    return run_synthetic, testmode, freya


class WorkspaceBusy(RuntimeError):
    pass


def busy_wait_s():
    try:
        return float(os.environ.get("SIM_BUSY_WAIT_S", "120"))
    except ValueError:
        return 120.0


def guard_busy(freya, max_wait_s):
    """Wrap freya.api so a workspace that keeps another simulation running makes run_simulation fail after
    max_wait_s instead of waiting forever. Returns a function that restores the original."""
    orig = freya.api
    until = time.time() + max_wait_s

    def api(method, path, body=None, **params):
        try:
            return orig(method, path, body, **params)
        except RuntimeError as e:
            if "concurrent simulations" in str(e) and time.time() > until:
                raise WorkspaceBusy("another simulation kept the workspace busy for %.0f s" % max_wait_s) from None
            raise
    freya.api = api

    def restore():
        freya.api = orig
    return restore


def http_up(url, headers=None, timeout_s=15):
    """(up, detail). Up = any HTTP answer below 500 (4xx means the service answers). Cloudflare outages such as
    error 1101 come back as 5xx or connection errors."""
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return True, "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        return e.code < 500, "HTTP %s" % e.code
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:150])


def health_check(freya, bank_host=None, timeout_s=15, probe=None):
    """Are Freya (and the bank mock, when the sink uses it) answering right now? -> (ok, detail)."""
    probe = probe or http_up
    ok, d = probe(freya.URL.rstrip("/") + "/api/v2/workspaces", getattr(freya, "HEADERS", {}), timeout_s)
    details = ["freya " + d]
    if ok and bank_host:
        ok2, d2 = probe(bank_host.rstrip("/") + "/", {"user-agent": "freya-bank-scorer/0.1"}, timeout_s)
        details.append("bank mock " + d2)
        ok = ok2
    return ok, "; ".join(details)


class SimLock:
    """Per-simulation lock through the orchestra tool, only when ORCH_SIM_LOCK=1 (off for the real final run).

    Holds `orch.py with-lock db -- <python waiting on stdin>` open while the simulation runs; closing stdin
    releases it. ORCH_CMD overrides the orchestra command, ORCH_LOCK_CWD its working directory (a git checkout)."""

    def __init__(self, wait_s, cwd=None, log=None):
        self.enabled = os.environ.get("ORCH_SIM_LOCK", "") == "1"
        self.wait_s = wait_s
        self.cwd = str(cwd or os.environ.get("ORCH_LOCK_CWD") or KIT)
        self.proc = None
        self.log = log or (lambda m: None)
        self.acquired = not self.enabled

    def __enter__(self):
        if not self.enabled:
            return self
        import shlex
        import subprocess
        cmd = shlex.split(os.environ.get("ORCH_CMD") or "python3 " + os.path.expanduser("~/.claude/orchestra/orch.py"))
        holder = [sys.executable, "-c", "import sys; print('SIMLOCK-ACQUIRED', flush=True); sys.stdin.read()"]
        try:
            self.proc = subprocess.Popen(cmd + ["with-lock", "db", "--"] + holder, cwd=self.cwd, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except Exception as e:  # noqa: BLE001
            self.log("sim lock unavailable (%s); running without it" % e)
            self.acquired = True
            return self
        box = {}

        def reader():
            for line in self.proc.stdout:
                if "SIMLOCK-ACQUIRED" in line:
                    box["ok"] = True
                    return
        th = threading.Thread(target=reader, daemon=True)
        th.start()
        th.join(self.wait_s)
        if box.get("ok"):
            self.acquired = True
        elif self.proc.poll() is not None:
            self.log("sim lock command exited (code %s); running without it" % self.proc.returncode)
            self.acquired = True
        else:
            self.log("sim lock not acquired within %.0f s" % self.wait_s)
            self._release()
        return self

    def _release(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            self.proc = None

    def __exit__(self, *exc):
        self._release()
        return False


def cache_calls(freya):
    """Wrap freya.get_call so every call run_once reads is kept: ({call_id: call}, restore)."""
    calls = {}
    orig = getattr(freya, "get_call", None)
    if orig is None:
        return calls, (lambda: None)

    def get_call(call_id):
        c = orig(call_id)
        calls[call_id] = c
        return c
    freya.get_call = get_call

    def restore():
        freya.get_call = orig
    return calls, restore


def transcript_excerpt(call, limit_chars=2500, last=40):
    """Readable tail of a call: spoken turns and tool calls."""
    lines = []
    for m in (call or {}).get("messages") or []:
        role = m.get("role") or "?"
        msg = m.get("message")
        if role == "system":
            try:
                e = json.loads(msg or "")
            except Exception:
                continue
            if isinstance(e, dict) and e.get("event") in ("function_call_start", "function_call_end", "function_call_result"):
                name = e.get("name") or e.get("function") or (e.get("args") or {}).get("name") or ""
                body = e.get("result") if "result" in e else (e.get("args") or {}).get("params") or e.get("args")
                lines.append("[%s %s] %s" % (e.get("event"), name, json.dumps(body, default=str)[:300]))
            continue
        if msg:
            lines.append("%s: %s" % (role, " ".join(str(msg).split())[:300]))
        for tc in m.get("toolCalls") or m.get("tool_calls") or []:
            lines.append("[tool call] %s" % json.dumps(tc, default=str)[:300])
    text = "\n".join(lines[-last:])
    return text[-limit_chars:]


def persona_cases(cases):
    """Copies of the cases whose variables carry first_name/last_name, which the kit's persona text needs, taken
    from whatever name variables the bank uses. Only used to write personas; calls get the original variables."""
    out = []
    for c in cases:
        v = dict(c.get("variables") or {})
        if "first_name" not in v or "last_name" not in v:
            first = last = ""
            for k, x in v.items():
                kl = k.lower()
                if not first and ("first" in kl or "given" in kl or "fore" in kl):
                    first = str(x)
                elif not last and ("last" in kl or "surname" in kl or "family" in kl):
                    last = str(x)
            if not first and not last:
                full = next((str(x) for k, x in v.items() if "name" in k.lower()), "")
                first, _, last = full.partition(" ")
            title = next((str(x) for k, x in v.items() if any(w in k.lower() for w in ("title", "honorific", "salutation"))), "")
            v.setdefault("first_name", first or title or "the")
            v.setdefault("last_name", last or "customer")
        out.append(dict(c, variables=v))
    return out


class SelfTester:
    def __init__(self, openapi_path, test_slot, state_file, trace=None, policy=None, runner=None, log=print):
        self.openapi_path = str(openapi_path)
        with open(self.openapi_path) as fh:
            self.openapi = json.load(fh)
        self.test_slot = test_slot
        self.state_file = str(state_file)
        self.trace = trace
        self.policy = policy or {}
        self.runner = runner          # (run_synthetic, testmode, freya) for tests
        self.log = log
        self.info = None
        self.probe = None             # health probe override for tests

    def _mods(self):
        if self.runner is None:
            self.runner = _import_runner()
        return self.runner

    def run(self, workflow, cases, label="self-test", timeout_s=None, min_run_s=None):
        """Save the test-mode copy, run one simulation of `cases`, score. Never raises.

        One deadline (t0 + timeout_s) covers everything: health check, test-copy save, personas, lock wait, the
        simulation and the transcript reads. Transcripts come from the calls run_once already read.
        min_run_s: time the simulation itself needs; the lock wait stops early enough to leave it."""
        t0 = time.time()
        deadline = t0 + timeout_s if timeout_s and timeout_s > 0 else None
        res = {"ok": False, "label": label, "passed": 0, "total": len(cases), "rows": [], "duration_s": 0.0,
               "error": "", "simulation_id": None, "cases": [c["id"] for c in cases]}

        def left(cap=None):
            v = (deadline - time.time()) if deadline else (cap or 3600.0)
            return max(0.0, min(v, cap) if cap else v)
        try:
            rs, tm, freya = self._mods()
            use_sink = bool(os.environ.get("FREYA_BANK_TEAM_KEY"))
            ok, detail = health_check(freya, getattr(rs, "HOST", None) if use_sink else None,
                                      timeout_s=max(1.0, left(15)), probe=self.probe)
            if self.trace:
                self.trace.note("health check before %s: %s" % (label, detail))
            if not ok:
                res["error"] = "service down, test skipped (%s)" % detail
                return res
            ident = (self.policy.get("identity") or {})
            facts = {"max_wrong": ident.get("max_wrong"), "close_reasons": close_reasons(self.policy)}
            if facts["max_wrong"] is None:
                facts.pop("max_wrong")
            if not facts["close_reasons"]:
                facts.pop("close_reasons")
            extras = rs.scenario_extras(cases, facts, 3)
            scenarios = {c["id"]: tm.scenario_for_case(c, extras) for c in cases}
            sink = {"base_url": rs.HOST.rstrip("/") + "/v1", "var": tm.SINK_VAR} if use_sink else None
            twf = tm.to_test_mode(workflow, self.openapi, sink=sink)
            spec = tm.spec_from_openapi(self.openapi)
            info = run_with_timeout(freya.save_workflow, left(240), self.test_slot, twf, state_file=self.state_file)
            self.info = info
            if self.trace:
                self.trace.freya("saved test-mode copy", slot=self.test_slot, workflow_id=info.get("workflow_id"),
                                 agent_id=info.get("agent_id"), version=info.get("version"))
            ids = run_with_timeout(rs.personas, left(120), persona_cases(cases), None)
            if self.trace:
                self.trace.freya("personas ready", count=len(ids), note="created only when new or changed")
            box = {}

            def work():
                try:
                    box["out"] = rs.run_once(info["agent_id"], cases, ids, scenarios, spec, "PUB-01" if use_sink else None)
                except Exception as e:  # noqa: BLE001
                    box["err"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            t_lock = time.time()
            with SimLock(wait_s=max(1.0, left() - (min_run_s or 60)), log=self.log) as lock:
                waited = time.time() - t_lock
                if lock.enabled and self.trace:
                    self.trace.note("simulation lock %s after %.0f s" % ("acquired" if lock.acquired else "NOT acquired", waited))
                if not lock.acquired:
                    res["error"] = "simulation lock not free in time, test skipped"
                    return res
                limit = left() if deadline else None
                if deadline and limit <= 1:
                    res["error"] = "no time left after waiting for the simulation lock, test skipped"
                    return res
                restore_busy = guard_busy(freya, busy_wait_s()) if hasattr(freya, "api") else (lambda: None)
                calls, restore_calls = cache_calls(freya)
                th = threading.Thread(target=work, daemon=True)
                th.start()
                th.join(limit)
                restore_busy()
                restore_calls()
                if th.is_alive():
                    # an abandoned simulation keeps running on Freya and blocks the workspace for the next round
                    if getattr(freya, "LAST_SIM", None) and hasattr(freya, "cancel_simulation"):
                        freya.cancel_simulation(freya.LAST_SIM)
                    res["error"] = "simulation did not finish within %.0f s (abandoned and cancelled, result ignored)" % (limit or 0)
                    return res
            if "err" in box:
                res["error"] = box["err"]
                return res
            rows, sim_id, _it, status, took, med = box["out"]
            res["simulation_id"] = sim_id
            res["status"] = status
            res["median_slowest_turn_s"] = med
            for r in rows:
                if not r.get("pass") and r.get("call_id"):
                    call = calls.get(r["call_id"])
                    r["transcript"] = transcript_excerpt(call) if call else "(transcript not read during the run)"
            res["rows"] = rows
            res["passed"] = sum(1 for r in rows if r.get("pass"))
            res["ok"] = True
            return res
        except Exception as e:  # noqa: BLE001
            res["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            return res
        finally:
            res["duration_s"] = round(time.time() - t0, 1)
