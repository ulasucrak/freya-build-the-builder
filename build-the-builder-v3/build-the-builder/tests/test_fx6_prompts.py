"""FX6: prompt/flow fixes from the 250new run (builder/generate.py).

    cd build-the-builder && python3 -m unittest tests.test_fx6_prompts -v
"""
import copy
import json
import pathlib
import re
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
from builder import policy_schema as S  # noqa: E402
import test_generate as TG  # noqa: E402

OPENAPI = json.loads((KIT / "tools" / "openapi.json").read_text())


def spol(name):
    return json.loads((KIT / "policies" / (name + ".policy.json")).read_text())


def wf_of(raw, **opts):
    return G.generate_workflow(raw, None, OPENAPI, opts or None)


def changes_policy():
    """brief-b with the new-style '[changes ...]' marks (as the v5 extraction writes them)."""
    q = copy.deepcopy(TG.raw("brief-b"))
    for r in q["rules"]:
        r["text"] = re.sub(r"\[overrides ", "[changes ", r["text"])
        if r["text"].startswith("[changes brief-a.28]"):
            r["text"] = r["text"].replace("[changes brief-a.28]", "[changes brief-a.28, brief-a.30]")
    return q


class RestatedRuleHidden(unittest.TestCase):
    """1. '[28] First ask: "Can you make this payment today?"' next to '[b.4] (changes 28) First ask whether ... all of
    them today ... Do not ask about today again': the model followed [28] in a third of the B calls."""

    def test_restated_rule_is_not_shown(self):
        for raw in (TG.raw("brief-b"), changes_policy(), spol("brief-b")):
            p = S.normalise(raw)
            self.assertEqual(G.rule_roles(p).get("brief-a.28"), "over")
            wf = wf_of(raw)
            neg, sysp = wf["nodes"]["negotiate"]["task_message"], wf["system_prompt"]
            self.assertNotIn('First ask: "Can you make this payment today?"', neg + sysp)
            self.assertIn("First ask whether the customer can pay all of them today", neg)
            self.assertNotIn("(changes 28", neg)              # the hidden rule is not pointed at

    def test_changed_rule_with_more_content_stays(self):
        p = S.normalise(changes_policy())
        roles = G.rule_roles(p)
        for rid in ("brief-a.30", "brief-a.34", "brief-a.17"):
            self.assertNotEqual(roles.get(rid), "over", rid)
        neg = wf_of(changes_policy())["nodes"]["negotiate"]["task_message"]
        self.assertIn("(changes 30)", neg)

    def test_restated_heuristic(self):
        self.assertTrue(G._restated('First ask: "Can you make this payment today?".', "[changes x.28] First ask whether the customer can pay all of them today."))
        # long, tool-naming or multi-sentence rules hold more than the change
        self.assertFalse(G._restated("Read it back and ask. Call `create_promise` after a yes.", "Read all back together."))
        self.assertFalse(G._restated("Offer the floor once. Say the rest stays overdue.", "Offer the floor twice."))
        self.assertFalse(G._restated("State the amount.", "Persuasion attempts are counted per product."))
        # shared opening words alone do not make a restatement (review R09)
        self.assertFalse(G._restated("Do not offer the floor before attempt 2.", "[changes x.12] Do not offer a discount."))
        self.assertFalse(G._restated("If the customer asks, say the date.", "[changes x.3] If the customer is late, call."))

    def test_today_question_once_and_phrase_not_listed_as_fixed(self):
        for raw in (TG.raw("brief-b"), changes_policy(), spol("brief-b")):
            wf = wf_of(raw)
            neg, sysp = wf["nodes"]["negotiate"]["task_message"], wf["system_prompt"]
            line = [x for x in neg.splitlines() if x.startswith("The 'today' question is asked once")]
            self.assertEqual(len(line), 1)
            self.assertIn("not after a no, a dispute or a paid claim", line[0])
            self.assertIn('("Can you make this payment today?" only with one product)', line[0])
            self.assertNotIn("Can you make this payment today?", sysp)

    def test_single_product_has_no_today_once_line(self):
        for raw in (TG.raw("brief-a"), spol("brief-a"), spol("S2")):
            neg = wf_of(raw)["nodes"]["negotiate"]["task_message"]
            self.assertNotIn("The 'today' question is asked once", neg)

    def test_later_products_open_with_attempt_1(self):
        neg = wf_of(TG.raw("brief-b"))["nodes"]["negotiate"]["task_message"]
        self.assertIn("Later products open with Attempt 1.", neg)
        self.assertNotIn("Later products open", wf_of(TG.raw("brief-a"))["nodes"]["negotiate"]["task_message"])


class CustomerValues(unittest.TestCase):
    """2/3. N019 'once my tax refund comes in' went to the ladder; N186 'the minimum today' became 'Do you agree?'."""

    def test_check_line(self):
        for raw in (TG.raw("brief-a"), TG.raw("brief-b"), spol("S4")):
            neg = wf_of(raw)["nodes"]["negotiate"]["task_message"]
            self.assertIn(G.CHECK_LINE, neg)
        for w in ("condition", "after the weekend", "when I get paid", "'the minimum'", "not as your own offer",
                  "ask once more (a second is a refusal)", "Not for full today, an offer from the facts or a value already accepted"):
            self.assertIn(w, G.CHECK_LINE)

    def test_no_check_line_without_the_tool(self):
        neg = wf_of(TG.raw("brief-a"), check_tool=False)["nodes"]["negotiate"]["task_message"]
        self.assertNotIn(G.CHECK_LINE, neg)


class NoTalk(unittest.TestCase):
    """4. N092: "I'm not going into this, full stop." was not counted as the first 'do not want to talk'."""

    def test_wording_in_trigger_line(self):
        for name in ("brief-a-live", "brief-b"):
            neg = wf_of(TG.raw(name))["nodes"]["negotiate"]["task_message"]
            line = [x for x in neg.splitlines() if "twice" in x and "talk" in x][0]
            self.assertIn("'I'm not going into this'", line)
            self.assertNotIn("full stop", line)                         # "I can't pay, full stop" is a refusal
            self.assertIn("The second time: close at once", line)    # also for a note-action trigger (Brief B)

    def test_only_the_talk_trigger_gets_it(self):
        neg = wf_of(TG.raw("s3-live"))["nodes"]["negotiate"]["task_message"]
        for x in neg.splitlines():
            if "not going into this" in x:
                self.assertIn("talk", x.split(":")[0])

    def test_not_before_verification(self):
        greet = wf_of(TG.raw("brief-a-live"))["nodes"]["greet_verify"]["task_message"]
        self.assertNotIn("not going into this", greet)

    def test_extraction_label(self):
        wf = wf_of(TG.raw("brief-a-live"), extract_signals=True)
        x = [e for e in wf["nodes"]["negotiate"]["extraction_schema"] if e["name"] == "x_reply"][0]
        self.assertIn("not going into this", x["description"])
        self.assertNotIn("full stop", x["description"])


class Callback(unittest.TestCase):
    """5. N149: 'Would today at 12:00 work?' before any schedule_callback call."""

    def test_at_once(self):
        self.assertIn("at once", G.CALLBACK_LINE)
        self.assertIn("never ask 'would that work?'", G.CALLBACK_LINE)
        for name in ("brief-a", "brief-b"):
            greet = wf_of(TG.raw(name), enforce_callback=False)["nodes"]["greet_verify"]["task_message"]
            self.assertIn("at once (never ask 'would that work?' first)", greet)


class WrapUp(unittest.TestCase):
    """6. N173/N221/N222: 'One moment, please.' before every note call after the goodbye."""

    def test_no_filler(self):
        for name in ("brief-a", "brief-b", "fixture-x"):
            w = wf_of(TG.raw(name))["nodes"]["wrap_up"]
            for t in (w["task_message"], w["on_enter"]["prompt"]):
                self.assertNotIn("One moment", t)
                self.assertIn(G.WRAP_SILENT, t)
                self.assertIn("do not say it again", t)


class CloseSafety(unittest.TestCase):
    """7. close_call is not retried by the engine: the bank does not take a second close ('Nothing is accepted after
    it'), and a timed-out close may already be recorded (a retry would make two close_call rows; rule: exactly one).
    Connection failures are retried by the platform's connect_retries (default 2, safe: nothing reached the bank)."""

    def test_close_api_not_retried(self):
        wf = wf_of(TG.raw("brief-b"))
        self.assertEqual(wf["nodes"]["close_api"]["pre_actions"][0]["config"]["maxRetries"], 0)


class DescribedGreeting(unittest.TestCase):
    """8. Static greeting built from a described opening (option static_greeting)."""

    def test_built_lines(self):
        cases = {"brief-a": "Hello, I am a digital assistant calling from Freya Bank, and this call is recorded. Am I speaking with {title} {last_name}?",
                 "S1": "Hello, I am Nordlicht Bank's virtual assistant, and this call may be recorded for quality and training. Is this {first_name} {last_name}?",
                 "S7": "Hello, I am Tidewater Bank's virtual assistant, and this call is recorded. Is that {title} {last_name}?"}
        for name, line in cases.items():
            p = S.normalise(spol(name))
            self.assertEqual(G.static_opening(p), "", name)            # without the option: the model speaks it
            self.assertEqual(G.static_opening(p, True), line, name)

    def test_option(self):
        self.assertEqual(wf_of(spol("brief-b"), static_greeting=False)["nodes"]["greet_verify"]["on_enter"]["type"], "llm")
        oe = wf_of(spol("brief-b"), static_greeting=True)["nodes"]["greet_verify"]["on_enter"]
        self.assertEqual(oe, {"type": "tts", "text": "Hello, I am a digital assistant calling from Freya Bank, and this call is recorded."
                                                     " Am I speaking with {{title}} {{last_name}}?"})

    def test_forbidden_word_or_other_language_keeps_the_model(self):
        q = spol("brief-a")
        q["opening"]["forbidden_before_verification"] = list(q["opening"]["forbidden_before_verification"]) + ["Freya Bank"]
        self.assertEqual(G.static_opening(S.normalise(q), True), "")
        q = spol("brief-a")
        q["language"] = "German"
        self.assertEqual(G.static_opening(S.normalise(q), True), "")
        q = spol("brief-a")
        q["opening"]["text"] = "Say who you are and why you call."
        self.assertEqual(G.static_opening(S.normalise(q), True), "")

    def test_quoted_openings_unchanged(self):
        for name in ("S2", "S3", "S4", "S5", "S6"):
            p = S.normalise(spol(name))
            self.assertEqual(G.static_opening(p, True), G.static_opening(p), name)
            self.assertTrue(G.static_opening(p), name)


if __name__ == "__main__":
    unittest.main()
