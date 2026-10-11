"""FX5 / EDGE-20: a third party's callback time goes to schedule_callback, whose hooks judge the window.

Live, the model judged the window itself (from the 'outside the window' rule and the window list in greet_verify) and
never called schedule_callback, so the max_asks count of the hooks never ran. With guard + enforce_callback (default),
greet_verify carries one line that hands the window check to the tool, no window list, and the 'outside the window'
rule marked as the tool's job (its text stays: every rule reaches the model).

    cd build-the-builder && python3 -m unittest tests.test_fx5_callback -v
"""
import json
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
from builder import policy_schema as S  # noqa: E402
import test_generate as TG  # noqa: E402

BRIEFS = ("brief-a", "brief-b", "brief-a-live")
SPOLS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
OLD_CASE = "never judge it yourself"
WINDOWS = "{{w_callback_windows}}"


def greet(raw, **opts):
    return G.generate_workflow(raw, options=opts or None)["nodes"]["greet_verify"]["task_message"]


def spol(name):
    return json.loads((KIT / "policies" / (name + ".policy.json")).read_text())


class DefaultPrompt(unittest.TestCase):
    def test_greet_hands_the_window_to_the_tool(self):
        for name in BRIEFS:
            t = greet(TG.raw(name))
            self.assertEqual(t.count(G.CALLBACK_LINE), 1, name)
            self.assertNotIn(WINDOWS, t, name)
            self.assertNotIn(OLD_CASE, t, name)
            self.assertNotIn("ask for another time only", t, name)

    def test_outside_rule_is_tagged_window_rule_stays(self):
        for name in BRIEFS:
            p = S.normalise(TG.raw(name))
            t = greet(TG.raw(name))
            ids = G.callback_rule_ids(p, "greet")
            self.assertEqual(len(ids), 1, name)
            r = next(x for x in p["rules"] if x["id"] in ids)
            self.assertIn("outside that window", r["text"])
            self.assertIn(G.CB_TAG + G._rule_text(r["text"]), t, name)
            # the window definition is not a judging rule: shown untagged
            self.assertIn("] A callback is for today or the next business day", t, name)

    def test_shorter_than_the_old_prompt(self):
        for name in BRIEFS:
            self.assertLess(len(greet(TG.raw(name))), len(greet(TG.raw(name), enforce_callback=False)), name)

    def test_policies_generic(self):
        """Each S policy has its own 'outside the window' rule: it is found, tagged, and the window rule is not."""
        for name in SPOLS:
            raw = spol(name)
            p = S.normalise(raw)
            ids = G.callback_rule_ids(p, "greet")
            self.assertTrue(ids, name)
            t = greet(raw)
            self.assertIn(G.CALLBACK_LINE, t, name)
            self.assertNotIn(WINDOWS, t, name)
            for r in p["rules"]:
                if r["id"] in ids:
                    self.assertIn(G.CB_TAG + G._brace_vars(G._rule_text(r["text"]), p), t, (name, r["id"]))
        self.assertNotIn("02-policy-extract.5.3.2", G.callback_rule_ids(S.normalise(spol("S6")), "greet"))


class OldPromptWhenNotEnforced(unittest.TestCase):
    def test_enforce_callback_off(self):
        for name in BRIEFS:
            t = greet(TG.raw(name), enforce_callback=False)
            self.assertNotIn(G.CALLBACK_LINE, t, name)
            self.assertNotIn(G.CB_TAG, t, name)
            self.assertIn(WINDOWS, t, name)
            self.assertIn(OLD_CASE, t, name)

    def test_guard_off(self):
        """Without the guard the pre-hook does not check the window: the model keeps the list."""
        t = greet(TG.raw("brief-a"), guard=False)
        self.assertNotIn(G.CALLBACK_LINE, t)
        self.assertIn(WINDOWS, t)

    def test_negotiate_unchanged(self):
        for name in BRIEFS:
            on = G.generate_workflow(TG.raw(name))["nodes"]["negotiate"]["task_message"]
            off = G.generate_workflow(TG.raw(name), options={"enforce_callback": False})["nodes"]["negotiate"]["task_message"]
            self.assertEqual(on, off, name)
            self.assertNotIn(G.CALLBACK_LINE, on, name)


class HooksEnforceTheWindow(unittest.TestCase):
    """What the prompt now leaves to the tool: an outside time is stopped with the allowed windows, the second closes."""

    def test_outside_time_is_stopped_with_the_windows(self):
        for name in ("brief-a", "brief-b"):
            s = TG.Sim(G.generate_workflow(TG.raw(name)), now="2026-10-14T11:00", customer_id="c1")
            cfg, _r, msg = s.call("greet_verify", "schedule_callback", {"date": "2026-10-14", "time": "21:00"})
            self.assertIn(".invalid", cfg["url"], name)
            self.assertIn("is outside the allowed window", msg, name)
            self.assertIn("Allowed:", msg, name)
            self.assertIn("Ask once for another", msg, name)
            _c, _r, _m = s.call("greet_verify", "schedule_callback", {"date": "2026-10-14", "time": "22:00"})
            self.assertEqual(s.vs["w_cb_over"], "yes", name)

    def test_inside_time_goes_out_and_says_how_to_close(self):
        s = TG.Sim(G.generate_workflow(TG.raw("brief-a")), now="2026-10-14T11:00", customer_id="c1")
        cfg, _r, msg = s.call("greet_verify", "schedule_callback", {"date": "2026-10-14", "time": "15:00"})
        self.assertNotIn(".invalid", cfg["url"])
        self.assertIn("The callback is scheduled", msg)
        self.assertIn("not_reached", msg)


if __name__ == "__main__":
    unittest.main()
