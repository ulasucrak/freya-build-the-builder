"""Build trace (required by the competition rules) and the wall-clock budget of one builder run.

    trace = Trace(slot, trace_dir)              # writes traces/<slot>-<UTCts>.txt and a .json twin
    llm.on_call(trace.model_call)               # every model call: model, one-line purpose, duration
    trace.freya("saved", workflow_id=..., agent_id=..., version=...)
    trace.test_run("self-test round 1", cases=10, passed=7, duration_s=412)
    trace.finish("ok", score="7/10")

    budget = Budget(28 * 60)                    # hard wall clock for the whole run
    budget.remaining(); budget.can_start(est_s, reserve_s); budget.sub(planned_s, reserve_s)

The text file is rewritten after every event, so a crash still leaves a usable trace.
Standard library only, Python 3.9 compatible.
"""
import datetime
import json
import os
import pathlib
import re
import threading
import time


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text, limit=160):
    """Collapse whitespace; cut long text."""
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[:limit - 3] + "..."


class CallTimeout(RuntimeError):
    pass


def run_with_timeout(fn, timeout_s, *args, **kwargs):
    """fn(*args, **kwargs) in a daemon thread; CallTimeout when it does not return within timeout_s (the thread is
    abandoned). Exceptions of fn are re-raised. Used to put a hard deadline on blocking network calls."""
    box = {}

    def work():
        try:
            box["out"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(max(0.0, float(timeout_s)))
    if th.is_alive():
        raise CallTimeout("%s did not return within %.0f s" % (getattr(fn, "__name__", "call"), timeout_s))
    if "err" in box:
        raise box["err"]
    return box.get("out")


class Budget:
    """Wall-clock budget. `clock` is injectable (tests use a fake clock)."""

    def __init__(self, total_s, clock=None):
        self.clock = clock or time.monotonic
        self.total_s = float(total_s)
        self.t0 = self.clock()

    def elapsed(self):
        return self.clock() - self.t0

    def remaining(self):
        return max(0.0, self.total_s - self.elapsed())

    def can_start(self, est_s, reserve_s=0.0):
        """True when a step estimated at est_s seconds can finish with reserve_s left over."""
        return self.remaining() - reserve_s >= est_s

    def sub(self, planned_s, reserve_s=0.0, minimum_s=0.0):
        """Seconds a stage may use: its plan, capped by what is left after the reserve. 0 when below minimum_s."""
        s = min(float(planned_s), self.remaining() - reserve_s)
        return s if s >= minimum_s and s > 0 else 0.0

    def deadline_ts(self, seconds):
        """Absolute time.time() value `seconds` from now (for APIs that take a wall-clock deadline)."""
        return time.time() + max(0.0, seconds)


class Trace:
    """Collects events and writes them as plain text (for the jury) and JSON (for us)."""

    def __init__(self, slot, trace_dir, now=None, clock=None):
        self.now = now or utc_now
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.slot = slot
        self.started = self.now()
        self.t0 = self.clock()
        self.events = []
        self.model_calls = []
        self.freya_items = []
        self.test_runs = []
        self.stages = []
        self.result = None
        self.info = {}
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(slot)).strip("-") or "build"
        stamp = self.started.strftime("%Y%m%dT%H%M%SZ")
        d = pathlib.Path(trace_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / ("%s-%s.txt" % (safe, stamp))
        self.json_path = d / ("%s-%s.json" % (safe, stamp))
        self.ended = None
        self.flush()

    # ------------------------------------------------------------------ recording
    def _t(self):
        return round(self.clock() - self.t0, 1)

    def _add(self, kind, text, data=None):
        with self.lock:
            ev = {"t": self._t(), "at": iso_utc(self.now()), "kind": kind, "text": one_line(text, 400)}
            if data:
                ev["data"] = data
            self.events.append(ev)
        self.flush()
        return ev

    def set_info(self, **kw):
        self.info.update(kw)
        self.flush()

    def note(self, text, **data):
        return self._add("note", text, data or None)

    def stage(self, name, status, duration_s=None, detail=""):
        rec = {"stage": name, "status": status, "duration_s": None if duration_s is None else round(duration_s, 1),
               "detail": one_line(detail, 300)}
        self.stages.append(rec)
        dur = "" if duration_s is None else " (%.1f s)" % duration_s
        return self._add("stage", "%s: %s%s%s" % (name, status, dur, (" - " + rec["detail"]) if rec["detail"] else ""), rec)

    def model_call(self, rec):
        """llm.on_call hook. rec: model, purpose, backend, start, duration_s, ok, error, prompt/response chars."""
        r = {"model": rec.get("model"), "purpose": one_line(rec.get("purpose"), 120), "backend": rec.get("backend"),
             "start": rec.get("start"), "duration_s": rec.get("duration_s"), "ok": rec.get("ok"),
             "error": one_line(rec.get("error"), 200) if rec.get("error") else None,
             "prompt_chars": rec.get("prompt_chars"), "response_chars": rec.get("response_chars")}
        self.model_calls.append(r)
        status = "ok" if r["ok"] else "FAILED (%s)" % (r["error"] or "error")
        dur = r["duration_s"] if r["duration_s"] is not None else 0
        return self._add("model", "model call: %s | %s | %.1f s | %s" % (r["model"], r["purpose"], float(dur), status), r)

    def freya(self, action, **data):
        """Something created or updated on Freya (or exported from it)."""
        rec = dict(action=action, **data)
        self.freya_items.append(rec)
        parts = ", ".join("%s=%s" % (k, v) for k, v in data.items() if v not in (None, ""))
        return self._add("freya", "Freya %s: %s" % (action, parts), rec)

    def test_run(self, what, cases=None, passed=None, total=None, duration_s=None, detail="", **data):
        rec = dict(what=what, cases=cases, passed=passed, total=total,
                   duration_s=None if duration_s is None else round(duration_s, 1), detail=one_line(detail, 300), **data)
        self.test_runs.append(rec)
        score = "" if passed is None else " score %s/%s" % (passed, total if total is not None else "?")
        cs = ""
        if isinstance(cases, (list, tuple)):
            cs = " cases [%s]" % ", ".join(str(c) for c in cases)
        elif cases is not None:
            cs = " cases %s" % cases
        dur = "" if duration_s is None else " in %.0f s" % duration_s
        return self._add("test", "test run: %s%s%s%s%s" % (what, cs, score, dur, (" - " + rec["detail"]) if rec["detail"] else ""), rec)

    def finish(self, status, **data):
        self.ended = self.now()
        self.result = dict(status=status, **data)
        parts = ", ".join("%s=%s" % (k, v) for k, v in data.items() if v not in (None, ""))
        self._add("result", "final result: %s%s" % (status, (" (" + parts + ")") if parts else ""), self.result)

    # ------------------------------------------------------------------ output
    def duration_s(self):
        end = self.ended or self.now()
        return (end - self.started).total_seconds()

    def text(self):
        lines = ["BUILD TRACE", "slot: %s" % self.slot, "start (UTC): %s" % iso_utc(self.started)]
        lines.append("end (UTC): %s" % (iso_utc(self.ended) if self.ended else "(still running)"))
        lines.append("duration: %.0f s" % self.duration_s())
        for k, v in self.info.items():
            lines.append("%s: %s" % (k, one_line(v if not isinstance(v, (list, dict)) else json.dumps(v), 300)))
        lines.append("")
        lines.append("EVENTS (t = seconds since start)")
        for ev in self.events:
            lines.append("[%7.1f s] %s %s" % (ev["t"], ev["at"], ev["text"]))
        lines.append("")
        lines.append("MODEL CALLS: %d" % len(self.model_calls))
        for i, c in enumerate(self.model_calls, 1):
            lines.append("  %d. %s | %s | %.1f s | %s" % (i, c["model"], c["purpose"], float(c["duration_s"] or 0),
                                                         "ok" if c["ok"] else "failed"))
        lines.append("")
        lines.append("CREATED / UPDATED ON FREYA: %d" % len(self.freya_items))
        for f in self.freya_items:
            lines.append("  - " + ", ".join("%s=%s" % (k, v) for k, v in f.items() if v not in (None, "")))
        lines.append("")
        lines.append("TEST RUNS ON OWN AGENT: %d" % len(self.test_runs))
        for r in self.test_runs:
            lines.append("  - %s: cases=%s passed=%s/%s duration=%s s %s" % (
                r["what"], r["cases"] if not isinstance(r["cases"], (list, tuple)) else len(r["cases"]),
                r["passed"], r["total"], r["duration_s"], r.get("detail") or ""))
        lines.append("")
        if self.result:
            lines.append("FINAL RESULT: " + ", ".join("%s=%s" % (k, v) for k, v in self.result.items() if v not in (None, "")))
        else:
            lines.append("FINAL RESULT: (not finished)")
        return "\n".join(lines) + "\n"

    def as_json(self):
        return {"slot": self.slot, "start": iso_utc(self.started), "end": iso_utc(self.ended) if self.ended else None,
                "duration_s": round(self.duration_s(), 1), "info": self.info, "stages": self.stages,
                "model_calls": self.model_calls, "freya": self.freya_items, "test_runs": self.test_runs,
                "events": self.events, "result": self.result}

    def flush(self):
        try:
            with self.lock:
                txt = self.text()
                js = json.dumps(self.as_json(), indent=1, default=str)
            tmp = str(self.path) + ".tmp"
            with open(tmp, "w") as f:
                f.write(txt)
            os.replace(tmp, str(self.path))
            tmp = str(self.json_path) + ".tmp"
            with open(tmp, "w") as f:
                f.write(js)
            os.replace(tmp, str(self.json_path))
        except Exception:
            pass
