"""Offline tests for builder.trace (trace format, budget logic with a fake clock).

    cd build-the-builder && python -m unittest tests.test_trace -v
"""
import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest

KIT = pathlib.Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(KIT))

from builder.trace import Budget, Trace, one_line  # noqa: E402


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeNow:
    def __init__(self, clock):
        self.clock = clock
        self.base = datetime.datetime(2026, 10, 18, 9, 0, 0, tzinfo=datetime.timezone.utc)

    def __call__(self):
        return self.base + datetime.timedelta(seconds=self.clock.t - 1000.0)


class BudgetTests(unittest.TestCase):
    def test_remaining_and_can_start(self):
        c = FakeClock()
        b = Budget(600, clock=c)
        self.assertEqual(b.remaining(), 600)
        c.advance(400)
        self.assertEqual(b.elapsed(), 400)
        self.assertTrue(b.can_start(150, reserve_s=50))
        self.assertFalse(b.can_start(151, reserve_s=50))
        c.advance(1000)
        self.assertEqual(b.remaining(), 0)
        self.assertFalse(b.can_start(1))

    def test_sub_budget_capped_and_minimum(self):
        c = FakeClock()
        b = Budget(300, clock=c)
        self.assertEqual(b.sub(120, reserve_s=100), 120)
        c.advance(150)
        self.assertEqual(b.sub(120, reserve_s=100), 50)
        self.assertEqual(b.sub(120, reserve_s=100, minimum_s=60), 0)
        c.advance(200)
        self.assertEqual(b.sub(120), 0)


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.trace = Trace("dev-T06 test/slot", self.tmp.name, now=FakeNow(self.clock), clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_names(self):
        self.assertEqual(self.trace.path.name, "dev-T06-test-slot-20261018T090000Z.txt")
        self.assertEqual(self.trace.json_path.name, "dev-T06-test-slot-20261018T090000Z.json")
        self.assertTrue(self.trace.path.exists())          # written at once, before anything happens

    def test_full_trace_content(self):
        t = self.trace
        self.clock.advance(5)
        t.model_call({"model": "claude-x", "purpose": "extract policy part 1\n(replicate 1)", "backend": "sdk",
                      "duration_s": 42.5, "ok": True, "prompt_chars": 10, "response_chars": 20})
        t.model_call({"model": "claude-x", "purpose": "write node prompts", "duration_s": 3, "ok": False, "error": "timeout"})
        self.clock.advance(60)
        t.freya("saved phone agent (v1)", slot="s", workflow_id="wf_1", agent_id="ag_1", version=3)
        t.freya("exported", workflow_id="wf_1", path="out/s.workflow.json")
        t.test_run("self-test v1", cases=["SELF-01", "SELF-02"], passed=1, total=2, duration_s=300, detail="failed: SELF-02")
        t.stage("extract", "ok", 42.0)
        self.clock.advance(35)
        t.finish("ok", workflow_id="wf_1", selftest_score="1/2")
        txt = t.path.read_text()
        self.assertIn("start (UTC): 2026-10-18T09:00:00Z", txt)
        self.assertIn("end (UTC): 2026-10-18T09:01:40Z", txt)
        self.assertIn("duration: 100 s", txt)
        self.assertIn("model call: claude-x | extract policy part 1 (replicate 1) | 42.5 s | ok", txt)
        self.assertIn("FAILED (timeout)", txt)
        self.assertIn("MODEL CALLS: 2", txt)
        self.assertIn("workflow_id=wf_1", txt)
        self.assertIn("path=out/s.workflow.json", txt)
        self.assertIn("test run: self-test v1 cases [SELF-01, SELF-02] score 1/2 in 300 s", txt)
        self.assertIn("FINAL RESULT: status=ok", txt)
        self.assertIn("[   65.0 s]", txt)
        js = json.loads(t.json_path.read_text())
        self.assertEqual(js["result"]["status"], "ok")
        self.assertEqual(len(js["model_calls"]), 2)
        self.assertEqual(js["freya"][0]["workflow_id"], "wf_1")
        self.assertEqual(js["test_runs"][0]["passed"], 1)
        self.assertEqual(js["duration_s"], 100.0)

    def test_unfinished_trace_is_readable(self):
        self.trace.note("something happened")
        txt = self.trace.path.read_text()
        self.assertIn("(still running)", txt)
        self.assertIn("FINAL RESULT: (not finished)", txt)

    def test_one_line(self):
        self.assertEqual(one_line("a\n  b\tc"), "a b c")
        self.assertTrue(one_line("x" * 500, 50).endswith("..."))
        self.assertEqual(len(one_line("x" * 500, 50)), 50)


if __name__ == "__main__":
    unittest.main()
