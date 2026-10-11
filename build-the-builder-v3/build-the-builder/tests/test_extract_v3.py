"""T06: extraction contract for the fixed opening line and the meaning of "[overrides <id>]" marks (offline)."""
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import extract  # noqa: E402
from builder import policy_schema as S  # noqa: E402


class ExtractV3(unittest.TestCase):
    def test_opening_line_in_contract(self):
        c = extract.CONTRACT["opening"]
        self.assertIn('"line"', c)
        self.assertIn("word for word", c)
        self.assertIn("null when the opening is described rather than quoted", c)
        p = S.normalise({"opening": {"text": "Hi.", "line": "Hi. Am I speaking with {title} {last_name}?"}})
        self.assertEqual(p["opening"]["line"], "Hi. Am I speaking with {title} {last_name}?")

    def test_rules_prompt_override_marks(self):
        txt = extract.rules_prompt("DOCS")
        self.assertIn("[overrides <id>] ", txt)
        self.assertIn("left out of the agent's prompt", txt)
        self.assertIn("not when it merely adds", txt)
        self.assertTrue(txt.rstrip().endswith("DOCS"))


if __name__ == "__main__":
    unittest.main()
