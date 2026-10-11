"""freya.run_simulation: a simulation whose analysis hangs is cancelled instead of blocking the workspace forever."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import freya  # noqa: E402


class FakeFreya:
    """Fake API on a fake clock. `progress(t)` -> (status, done, analyzing) at time t."""

    def __init__(self, progress):
        self.t, self.progress, self.calls = 0.0, progress, []

    def api(self, method, path, body=None, **params):
        self.calls.append((method, path))
        st, done, an = self.progress(self.t)
        if method == "POST" and path == "/api/v2/simulations":
            return {"simulation": {"id": "s1"}}
        if path.endswith("/cancel"):
            return {"success": True}
        if path.endswith("/iterations"):
            return {"iterations": [{"id": "i1", "passedRuns": done, "failedRuns": 0, "analyzingRuns": an}]}
        return {"simulation": {"status": st}}

    def sleep(self, s):
        self.t += s

    def time(self):
        return self.t


def run(progress, n=99):
    f = FakeFreya(progress)
    with mock.patch.object(freya, "api", f.api), mock.patch.object(freya.time, "sleep", f.sleep), \
            mock.patch.object(freya.time, "time", f.time), mock.patch("builtins.print"):
        out = freya.run_simulation("a1", "t", {"p%d" % i: {} for i in range(n)})
    return out, f


class RunSimulation(unittest.TestCase):
    def test_completes_normally(self):
        (sid, iid, status), f = run(lambda t: ("completed", 99, 0) if t >= 600 else ("running", int(t // 10), 1))
        self.assertEqual((sid, iid, status), ("s1", "i1", "completed"))
        self.assertFalse(any(p.endswith("/cancel") for _m, p in f.calls))

    def test_hung_analysis_is_cancelled(self):
        # the run seen on 11 Oct: 97 calls done, 2 stuck in "analyzing", status "running" for ever
        (sid, _iid, status), f = run(lambda t: ("running", min(97, int(t // 10)), 2))
        self.assertEqual(status, "cancelled_stalled")
        self.assertIn(("POST", "/api/v2/simulations/s1/cancel"), f.calls)
        self.assertLess(f.t, 970 + freya.STALL_ANALYSIS_S + 30)    # cancelled soon after the last call ended
        self.assertEqual(freya.LAST_SIM, "s1")

    def test_slow_but_moving_is_not_cancelled(self):
        # one call finishes every 5 minutes: slow, but never idle long enough to be called stalled
        # (calls still talking are counted nowhere; a finished call is analysed within a minute)
        (_sid, _iid, status), f = run(lambda t: ("completed", 10, 0) if t >= 3000 else ("running", int(t // 300), 1 if t % 300 < 60 else 0), n=10)
        self.assertEqual(status, "completed")
        self.assertFalse(any(p.endswith("/cancel") for _m, p in f.calls))


if __name__ == "__main__":
    unittest.main()
