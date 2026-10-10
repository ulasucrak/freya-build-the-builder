"""Offline tests for builder.casegen: expected ledgers come from builder.oracle, never from the (mocked) LLM.

    cd build-the-builder && python -m unittest tests.test_casegen -v

Skipped when builder.oracle / builder.policy_schema (task T04) are not importable yet.
"""
import datetime
import json
import os
import pathlib
import sys
import unittest

KIT = pathlib.Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(KIT))

try:
    from builder import oracle, policy_schema  # noqa: F401
    HAVE_ORACLE = True
except Exception:  # pragma: no cover
    HAVE_ORACLE = False

from builder import casegen  # noqa: E402

TOOLS = {
    "verify_identity": {"fields": {"field": {"type": "string", "enum": ["birth_date", "pin_digits"], "required": True},
                                   "value": {"type": "string", "required": True}}},
    "get_account": {"fields": {}},
    "create_promise": {"fields": {"product_id": {"type": "string", "required": True},
                                  "amount": {"type": "number", "required": True},
                                  "date": {"type": "string", "required": True}}},
    "add_note": {"fields": {"product_id": {"type": "string"}, "reason": {"type": "string", "enum": ["contested", "settled"]}}},
    "schedule_callback": {"fields": {"date": {"type": "string"}, "time": {"type": "string"}}},
    "transfer_to_agent": {"fields": {"queue": {"type": "string", "enum": ["care_team", "people"]}}},
    "close_call": {"fields": {"outcome": {"type": "string", "enum": ["ok_promise", "no_deal", "away", "locked", "moved"]},
                              "reason": {"type": "string"}}},
}


def make_policy(max_count=2, date_format="DD/MM/YYYY HH:MM"):
    """A small invented bank (test data only)."""
    return {
        "source": {"briefs": ["x.md"], "overrides_applied": []},
        "variables": [{"name": "given_name", "meaning": "first name"}, {"name": "family_name", "meaning": "surname"},
                      {"name": "salutation", "meaning": "Mr/Ms"}, {"name": "clock_now", "meaning": "current time"}],
        "clock": {"now_variable": "clock_now", "timezone": None, "date_format": date_format},
        "calendar": {"weekend": [6, 7], "holidays": ["2026-11-02", "2026-11-11"], "roll_rule": "next_business_day"},
        "opening": {"text": "Hello", "disclosures": [], "forbidden_before_verification": ["arrears"]},
        "third_party": {"rules": [], "cases": [
            {"trigger": "someone else answers", "say": "", "action": "callback", "outcome": "away", "reason": "call_again"},
            {"trigger": "number belongs to someone else", "say": "", "action": "end", "outcome": "away", "reason": "bad_number"}]},
        "identity": {"questions": [
            {"order": 1, "tool_field": "pin_digits", "ask": "last 3 digits", "answer_format": "digits", "normalise": "digits_only"},
            {"order": 2, "tool_field": "birth_date", "ask": "date of birth", "answer_format": "date", "normalise": "dmy_to_iso"}],
            "max_wrong": 2, "wrong_counts_rules": [], "fail": {"outcome": "locked", "reason": "security", "say": "Sorry."}},
        "products": {"max_count": max_count, "order": "as_returned", "types": {
            "card": {"speak_as": "card ending {last_digits}", "floor_field": "min_due", "full_field": "overdue"},
            "loan": {"speak_as": "loan", "floor_field": "instalment", "full_field": "overdue"}}},
        "bands": [
            {"name": "soft", "min_days_late": 1, "max_days_late": 20,
             "deadline": {"kind": "nth_business_day", "n": 8, "counted_from": "today"},
             "attempts": [{"n": 1, "amount": "full", "date": "end_of_week_business_day", "text": ""}]},
            {"name": "hard", "min_days_late": 21, "max_days_late": 60,
             "deadline": {"kind": "nth_business_day", "n": 4, "counted_from": "today"},
             "attempts": [{"n": 1, "amount": "full", "date": "next_business_day", "text": ""},
                          {"n": 2, "amount": "floor", "date": "deadline", "text": ""}]}],
        "amounts": {"min": "floor", "max": "full", "below_min": "reject", "ask_open_amount": False, "rules": []},
        "dates": {"customer_date_after_deadline": "offer the deadline", "vague_date": "ask", "rules": []},
        "promise": {"readback": "read back", "confirm_required": True, "retry_on_failure": 1, "success_say": "Done.",
                    "failure_say": "Could not.", "per_product": True},
        "callback": {"days": ["today", "next_business_day"], "start": "09:00", "end": "17:00", "end_inclusive": False,
                     "must_be_after_now": True},
        "triggers": [
            {"name": "contested", "detect": "says the debt is not theirs", "action": "note", "queue": None,
             "outcome": "no_deal", "reason": "contested", "note_reason": "contested", "say": None, "precedence": 2},
            {"name": "illness", "detect": "mentions serious illness", "action": "transfer", "queue": "care_team",
             "outcome": "moved", "reason": "care", "note_reason": None, "say": None, "precedence": 1},
            {"name": "bad_number", "detect": "not the customer's number", "action": "close", "queue": None,
             "outcome": "away", "reason": "bad_number", "note_reason": None, "say": None, "precedence": 0}],
        "notes": {"tool_reasons": ["contested", "settled"], "when": "for each product without a promise",
                  "forbidden_on_transfer": True},
        "outcomes": {"table": [{"outcome": "ok_promise", "reason": None, "when": "promise recorded"},
                               {"outcome": "no_deal", "reason": "contested", "when": "disputed"},
                               {"outcome": "no_deal", "reason": "refused", "when": "refused"},
                               {"outcome": "no_deal", "reason": "system", "when": "tool failed"},
                               {"outcome": "away", "reason": "call_again", "when": "callback"},
                               {"outcome": "away", "reason": "bad_number", "when": "wrong number"},
                               {"outcome": "locked", "reason": "security", "when": "identity failed"},
                               {"outcome": "moved", "reason": "care", "when": "transfer"}],
                     "precedence": [], "multi_product": ""},
        "phrases": {},
        "tools": TOOLS,
        "rules": [{"id": "1", "text": "Be polite."}],
    }


class FakeLLM:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        if self.fail:
            raise RuntimeError("model down")
        ids = [c["id"] for c in json.loads(prompt.split("CASES:", 1)[1].split("\n\nAnswer JSON", 1)[0])]
        # the model tries to change facts: those keys must be ignored
        return {"cases": [{"id": i, "script": "SCRIPT " + i, "checks": ["check " + i],
                           "variables": {"given_name": "Ada", "family_name": "Quist", "clock_now": "1999-01-01T00:00",
                                         "unknown_var": "x"},
                           "expected": {"outcome": "hacked"}, "identity_text": {}} for i in ids]}


@unittest.skipUnless(HAVE_ORACLE, "builder.oracle / builder.policy_schema not available yet (task T04)")
class CasegenTests(unittest.TestCase):
    BASE = datetime.date(2026, 10, 12)

    def gen(self, policy=None, llm=None, **kw):
        return casegen.generate_cases(policy or make_policy(), ask_json=llm, n_max=kw.pop("n_max", 12),
                                      base_date=self.BASE, seed=kw.pop("seed", 3), **kw)

    def test_public_test_set_shape(self):
        cases, warns = self.gen(llm=FakeLLM())
        self.assertGreaterEqual(len(cases), 8)
        self.assertLessEqual(len(cases), 12)
        for c in cases:
            for k in ("id", "brief", "family", "level", "title", "variables", "identity", "products", "script",
                      "expected", "checks"):
                self.assertIn(k, c, k)
            self.assertEqual(set(c["expected"]), {"outcome", "reason", "promises", "notes", "callbacks", "transfers"})
            self.assertTrue(c["expected"]["outcome"])
            self.assertEqual(set(c["identity"]), {"pin_digits", "birth_date"})
        self.assertEqual(len(set(c["id"] for c in cases)), len(cases))
        kinds = set(c["kind"] for c in cases)
        for k in ("happy", "id_fail", "third_party_callback", "deadline_cap", "second_attempt", "multi_product",
                  "tool_failure"):
            self.assertIn(k, kinds)

    def test_llm_cannot_change_expected_values(self):
        with_llm, _ = self.gen(llm=FakeLLM())
        without, warns = self.gen(llm=None)
        self.assertEqual([c["expected"] for c in with_llm], [c["expected"] for c in without])
        self.assertTrue(all(c["script"].startswith("SCRIPT ") for c in with_llm))
        for c in with_llm:
            self.assertNotIn("unknown_var", c["variables"])
            self.assertEqual(c["variables"]["given_name"], "Ada")
            self.assertNotEqual(c["variables"]["clock_now"], "1999-01-01T00:00")   # the call time is ours

    def test_llm_failure_falls_back_to_templates(self):
        cases, warns = self.gen(llm=FakeLLM(fail=True))
        self.assertTrue(cases)
        self.assertTrue(any("template" in w for w in warns))
        self.assertTrue(all(c["script"] for c in cases))

    def test_expected_ledgers_follow_oracle(self):
        policy = make_policy()
        cases, _ = self.gen(policy)
        npol = policy_schema.normalise(policy)
        cal = oracle.make_calendar(npol)
        by = {c["kind"]: c for c in cases}
        for kind in ("happy", "deadline_cap", "second_attempt", "multi_product"):
            c = by[kind]
            today, _t = oracle.parse_now(c["variables"]["clock_now"])
            self.assertIsNotNone(today, c["variables"]["clock_now"])
            self.assertTrue(oracle.is_business_day(today, cal))
            prods = oracle.products_for_case(npol, c["products"], today.isoformat() + "T10:00")
            self.assertEqual(c["expected"]["outcome"], "ok_promise")
            for pr, built in zip(c["expected"]["promises"], prods):
                ok = oracle.check_promise(built, pr["amount"], pr["date"], today, cal)
                self.assertTrue(ok["ok"], (kind, pr, ok))
        dl = by["deadline_cap"]
        today, _t = oracle.parse_now(dl["variables"]["clock_now"])
        expected_deadline = oracle.deadline(npol, dl["products"][0]["days_late"], today.isoformat() + "T10:00")
        self.assertEqual(dl["expected"]["promises"][0]["date"], expected_deadline)
        sa = by["second_attempt"]
        self.assertEqual(sa["expected"]["promises"][0]["amount"], sa["products"][0]["instalment" if sa["products"][0]["type"] == "loan" else "min_due"])
        happy = by["happy"]
        self.assertEqual(happy["expected"]["promises"][0]["date"], oracle.parse_now(happy["variables"]["clock_now"])[0].isoformat())

    def test_identity_third_party_triggers_failure(self):
        cases, _ = self.gen()
        by = {c["kind"]: c for c in cases}
        self.assertEqual((by["id_fail"]["expected"]["outcome"], by["id_fail"]["expected"]["reason"]), ("locked", "security"))
        self.assertIn("wrong", by["id_fail"]["script"].lower())
        tp = by["third_party_callback"]
        self.assertTrue(tp.get("third_party"))
        self.assertEqual(tp["family"], "Third party")
        self.assertEqual((tp["expected"]["outcome"], tp["expected"]["reason"]), ("away", "call_again"))
        cb = tp["expected"]["callbacks"][0]
        npol = policy_schema.normalise(make_policy())
        today, now_t = oracle.parse_now(tp["variables"]["clock_now"])
        self.assertTrue(oracle.check_callback(cb["date"], cb["time"], today, now_t, npol, oracle.make_calendar(npol))["ok"])
        tr = by["trigger:illness"]
        self.assertEqual(tr["expected"]["transfers"], [{"queue": "care_team"}])
        self.assertEqual(tr["expected"]["notes"], [])                 # notes forbidden on transfer
        self.assertEqual((tr["expected"]["outcome"], tr["expected"]["reason"]), ("moved", "care"))
        nt = by["trigger:contested"]
        self.assertEqual(nt["expected"]["notes"], [{"product_id": "p1", "reason": "contested"}])
        self.assertNotIn("trigger:bad_number", by)                    # a third-party ending, not an account trigger
        tf = by["tool_failure"]
        self.assertEqual(tf["faults"], [{"tool": "create_promise", "fail_first_n": 1}])
        self.assertEqual(len(tf["expected"]["promises"]), 1)

    def test_single_product_bank_has_no_multi_case(self):
        cases, _ = self.gen(make_policy(max_count=1))
        self.assertNotIn("multi_product", [c["kind"] for c in cases])
        self.assertTrue(all(len(c["products"]) <= 1 for c in cases))

    def test_n_max(self):
        cases, _ = self.gen(n_max=5)
        self.assertEqual(len(cases), 5)


@unittest.skipUnless(HAVE_ORACLE, "builder.oracle not available yet (task T04)")
class FormatTests(unittest.TestCase):
    def test_format_now(self):
        dt = datetime.datetime(2026, 10, 14, 9, 5)
        self.assertEqual(casegen.format_now(dt, "DD/MM/YYYY HH:MM"), "14/10/2026 09:05")
        self.assertEqual(casegen.format_now(dt, "YYYY-MM-DDTHH:MM (example: 2026-10-14T11:00)"), "2026-10-14T09:05")
        self.assertEqual(casegen.format_now(dt, ""), "2026-10-14T09:05")
        self.assertEqual(casegen.format_now(dt, "unknown words"), "2026-10-14T09:05")

    def test_identity_values(self):
        import random
        rng = random.Random(1)
        pol = make_policy()
        v = casegen.identity_value({"tool_field": "pin3", "answer_format": "digits"}, pol, rng)
        self.assertEqual(len(v), 3)
        d = casegen.identity_value({"tool_field": "birth_date", "answer_format": "date"}, pol, rng)
        self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")
        pol["tools"]["verify_identity"]["notes"] = "birth_date as DD-MM-YYYY"
        d2 = casegen.identity_value({"tool_field": "birth_date", "answer_format": "date"}, pol, rng)
        self.assertRegex(d2, r"^\d{2}-\d{2}-\d{4}$")
        self.assertNotEqual(casegen.wrong_value("1984-06-22", "date"), "1984-06-22")
        self.assertNotEqual(casegen.wrong_value("4471", "digits"), "4471")


if __name__ == "__main__":
    unittest.main()
