import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import scoreboard as sb  # noqa: E402


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.log = os.path.join(self.d, "log.jsonl")
        lines = [
            {"at": "2026-10-10 10:00:00", "agent_id": "0178a85a-x", "brief": "A", "runs": 1,
             "passed": 9, "total": 10, "seconds": 5, "failed": ["PUB-06"]},
            "not json{",
            {"at": "2026-10-10 11:00:00", "slot": "s1", "label": "ours", "bank": "A", "source": "synthetic",
             "style": "x", "reps": 3, "passed": 6, "total": 10, "per_case": {"C1": "FAIL", "C2": "PASS"}},
            {"at": "2026-10-10 12:00:00", "slot": "s1", "label": "ours", "bank": "A", "source": "synthetic",
             "style": "x", "reps": 3, "passed": 8, "total": 10, "median_turn_s": 2.5},
            {"junk": 1},
        ]
        with open(self.log, "w") as f:
            for l in lines:
                f.write((l if isinstance(l, str) else json.dumps(l)) + "\n")

    def test_load_tolerates_bad_lines(self):
        self.assertEqual(len(sb.load_rows(self.log)), 4)
        self.assertEqual(sb.load_rows("/nonexistent/x"), [])

    def test_summary_trend(self):
        recs = [sb.norm(d, {}) for d in sb.load_rows(self.log)]
        row = [r for r in sb.summary(recs) if r[0] == "A"][0]
        self.assertEqual(row[1], "80%")
        self.assertTrue(row[2].endswith("^"))
        self.assertEqual(row[3], "90%")
        self.assertEqual(recs[0]["label"], "baseline")
        self.assertEqual(recs[1]["failed"], ["C1"])

    def test_outputs(self):
        self.assertEqual(sb.main(["--log", self.log, "--md", "--html"]), 0)
        self.assertIn("refresh", open(os.path.join(self.d, "scoreboard.html")).read())
        self.assertIn("| bank |", open(os.path.join(self.d, "SCOREBOARD.md")).read())


if __name__ == "__main__":
    unittest.main()
