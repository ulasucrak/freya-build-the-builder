import pathlib, sys, unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import run_cases as rc


class Fx9(unittest.TestCase):
    def test_batches_of_100(self):
        sizes = []
        def create(b): sizes.append(len(b)); return {"personas": [{"id": x} for x in b]}
        logs = []
        out = rc.create_personas_batched(list(range(250)), create=create, log=logs.append)
        self.assertEqual(sizes, [100, 100, 50]); self.assertEqual([p["id"] for p in out], list(range(250))); self.assertEqual(len(logs), 3)

    def test_empty_detection(self):
        setup = {"messages": [{"role": "system", "message": "{}"}]}
        self.assertTrue(rc.is_empty_call(setup)); self.assertTrue(rc.is_empty_call({}))
        self.assertFalse(rc.is_empty_call({"messages": [{"role": "assistant"}]}))
        self.assertFalse(rc.is_empty_call({"messages": [{"role": "user"}]}))

    def test_merge(self):
        rows = [{"id": "a", "pass": False}, {"id": "b", "pass": True}]
        m = rc.merge_retried(rows, [{"id": "a", "pass": True}])
        self.assertEqual(m, [{"id": "a", "pass": True, "retried_empty": True}, {"id": "b", "pass": True, "retried_empty": False}])


if __name__ == "__main__": unittest.main()
