"""Offline tests for builder.extract / builder.llm (no model calls).

    cd build-the-builder && python -m unittest tests.test_extract -v

Live check (needs Claude auth; runs the real extractor and checks Brief A/B facts):
    EXTRACT_LIVE=1 python -m unittest tests.test_extract.LiveFacts -v
Or check already-saved outputs in policies/ (skipped when the files are missing):
    python -m unittest tests.test_extract.SavedPolicyFacts -v
"""
import copy
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import extract, llm  # noqa: E402

OPENAPI = KIT / "tools" / "openapi.json"
BRIEF_A = KIT / "briefs" / "brief-a.md"
BRIEF_B = KIT / "briefs" / "brief-b.md"
TOOL_NAMES = {"verify_identity", "get_account", "create_promise", "add_note", "schedule_callback",
              "transfer_to_agent", "close_call"}


class OpenapiParsing(unittest.TestCase):
    def setUp(self):
        self.info = extract.parse_openapi(OPENAPI)
        self.tools = self.info["tools"]

    def test_seven_tools(self):
        self.assertEqual(set(self.tools), TOOL_NAMES)

    def test_fields_enums_required(self):
        v = self.tools["verify_identity"]["fields"]
        self.assertEqual(v["field"]["enum"], ["date_of_birth", "phone_last4"])
        self.assertTrue(v["answer"]["required"])
        self.assertEqual(self.tools["transfer_to_agent"]["fields"]["queue"]["enum"],
                         ["restructuring", "general", "support"])
        self.assertEqual(self.tools["add_note"]["fields"]["reason"]["enum"],
                         ["refused", "hardship", "dispute", "paid", "service_error"])
        self.assertFalse(self.tools["add_note"]["fields"]["text"]["required"])
        cp = self.tools["create_promise"]["fields"]
        self.assertEqual(cp["date"]["format"], "date")
        self.assertEqual(cp["amount"]["type"], "number")
        cc = self.tools["close_call"]["fields"]
        self.assertIn("not_reached", cc["outcome"]["enum"])
        self.assertIsNone(cc["reason"]["enum"])
        self.assertFalse(cc["reason"]["required"])

    def test_response_fields_and_url(self):
        ga = self.tools["get_account"]
        prod = ga["response_fields"]["products"]["items"]["properties"]
        self.assertEqual(prod["type"]["enum"], ["credit_card", "personal_loan", "car_loan"])
        self.assertIn("minimum_payment", prod)
        self.assertEqual(self.tools["verify_identity"]["response_fields"]["hint"]["enum"], ["day_month_swapped"])
        self.assertTrue(ga["url"].endswith("/get_account"))
        self.assertTrue(ga["url"].startswith("https://"))
        self.assertEqual(ga["method"], "POST")

    def test_refs_and_changed_api(self):
        spec = {
            "servers": [{"url": "https://x.test/v2/"}],
            "components": {"schemas": {"Q": {"type": "string", "enum": ["a", "b"]},
                                       "Body": {"type": "object", "required": ["q"],
                                                "properties": {"q": {"$ref": "#/components/schemas/Q"},
                                                               "n": {"type": ["integer", "null"]}}}}},
            "paths": {"/do_it": {"post": {"operationId": "do_it", "requestBody": {"content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}},
                "responses": {"200": {"description": "ok"}}}},
                "/odd": {"get": {"parameters": [{"name": "k", "in": "query", "required": True,
                                                 "schema": {"type": "string"}}]}}},
        }
        tools = extract.parse_openapi(spec)["tools"]
        self.assertEqual(tools["do_it"]["fields"]["q"]["enum"], ["a", "b"])
        self.assertTrue(tools["do_it"]["fields"]["q"]["required"])
        self.assertEqual(tools["do_it"]["fields"]["n"]["type"], "integer")
        self.assertEqual(tools["do_it"]["url"], "https://x.test/v2/do_it")
        self.assertTrue(tools["odd"]["fields"]["k"]["required"])


class Merge(unittest.TestCase):
    def test_later_overrides_scalars_and_logs(self):
        log = []
        out = extract.merge_policies({"products": {"max_count": 1, "order": "as_returned"}},
                                     {"products": {"max_count": 3}}, "", log)
        self.assertEqual(out["products"], {"max_count": 3, "order": "as_returned"})
        self.assertTrue(any("max_count" in x for x in log))

    def test_keyed_lists_merge(self):
        base = {"bands": [{"name": "early", "min_days_late": 1}, {"name": "late", "min_days_late": 8}],
                "triggers": [{"name": "dispute", "action": "close"}],
                "rules": [{"id": "a.1", "text": "x"}],
                "outcomes": {"table": [{"outcome": "paid", "reason": None, "when": "r39"}]}}
        over = {"bands": [{"name": "late", "min_days_late": 10}],
                "triggers": [{"name": "dispute", "action": "note"}, {"name": "new", "action": "say"}],
                "rules": [{"id": "b.1", "text": "y"}],
                "outcomes": {"table": [{"outcome": "paid", "reason": None, "when": "all noted paid"}]}}
        log = []
        out = extract.merge_policies(base, over, "", log)
        self.assertEqual([b["min_days_late"] for b in out["bands"]], [1, 10])
        self.assertEqual([t["action"] for t in out["triggers"]], ["note", "say"])
        self.assertEqual([r["id"] for r in out["rules"]], ["a.1", "b.1"])
        self.assertEqual(len(out["outcomes"]["table"]), 1)
        self.assertEqual(out["outcomes"]["table"][0]["when"], "all noted paid")
        self.assertTrue(log)
        self.assertEqual(base["bands"][1]["min_days_late"], 8)  # inputs untouched

    def test_notes_concatenate_and_none_keeps(self):
        out = extract.merge_policies({"calendar": {"notes": ["a"], "roll_rule": "none"}},
                                     {"calendar": {"notes": ["b", "a"], "roll_rule": None}})
        self.assertEqual(out["calendar"]["notes"], ["a", "b"])
        self.assertEqual(out["calendar"]["roll_rule"], "none")


class Normalise(unittest.TestCase):
    def test_dates(self):
        for raw in ("2026-10-26", "2026/10/26", "26.10.2026", "26/10/2026", "26 October 2026",
                    "October 26, 2026", "Monday 26th October 2026", "26 Oct 2026"):
            self.assertEqual(extract.norm_date(raw), "2026-10-26", raw)
        self.assertIsNone(extract.norm_date("Christmas"))
        self.assertIsNone(extract.norm_date("2026-02-30"))

    def test_times(self):
        cases = {"10:00": "10:00", "10": "10:00", "7pm": "19:00", "7 PM": "19:00", "19h": "19:00",
                 "9.30": "09:30", "1000": "10:00", "12am": "00:00", 19: "19:00", "10h30": "10:30"}
        for raw, want in cases.items():
            self.assertEqual(extract.norm_time(raw), want, raw)
        self.assertIsNone(extract.norm_time("noonish"))
        self.assertIsNone(extract.norm_time("25:00"))

    def test_calendar_section(self):
        w = []
        cal = extract.normalise_section("calendar", {"weekend": ["Saturday", "sun", 6], "holidays": [
            "2026-12-25", {"date": "1 January 2027", "name": "x"}, "Easter Monday"],
            "roll_rule": "next business day"}, w)
        self.assertEqual(cal["weekend"], [6, 7])
        self.assertEqual(cal["holidays"], ["2026-12-25", "2027-01-01"])
        self.assertEqual(cal["roll_rule"], "next_business_day")
        self.assertTrue(any("Easter" in n for n in cal["notes"]))
        self.assertTrue(w)

    def test_bands_and_odd_shapes(self):
        w = []
        bands = extract.normalise_section("bands", {"early": {"min_days_late": "1", "max_days_late": "7 days",
                                                              "deadline": 12, "attempts": 1}}, w)
        self.assertEqual(bands[0]["name"], "early")
        self.assertEqual((bands[0]["min_days_late"], bands[0]["max_days_late"]), (1, 7))
        self.assertEqual(bands[0]["deadline"]["n"], 12)
        self.assertEqual(len(bands[0]["attempts"]), 1)
        # unexpected table shape: never crash, degrade to notes
        opening = extract.normalise_section("opening", [["col1", "col2"], ["a", "b"]], w)
        self.assertIn("notes", opening)
        trig = extract.normalise_section("triggers", ["someone asks something odd"], w)
        self.assertEqual(trig[0]["action"], "say")

    def test_outcome_rows_split(self):
        o = extract.normalise_section("outcomes", {"table": [{"outcome": "x", "reason": ["a", "b"], "when": ""}]}, [])
        self.assertEqual([(r["outcome"], r["reason"]) for r in o["table"]], [("x", "a"), ("x", "b")])


def _fake_parts():
    p1 = {
        "source": {"briefs": ["doc.md"], "overrides_applied": []},
        "variables": [{"name": "customer_id", "meaning": "id"}],
        "clock": {"now_variable": "now", "timezone": None, "date_format": "YYYY-MM-DDTHH:MM"},
        "calendar": {"weekend": [6, 7], "holidays": ["2030-01-02"], "roll_rule": "next_business_day"},
        "opening": {"text": "Hi", "disclosures": [], "forbidden_before_verification": []},
        "third_party": {"cases": [], "rules": []},
        "identity": {"questions": [{"order": 1, "tool_field": "date_of_birth", "ask": "dob?",
                                    "answer_format": "date", "normalise": "dmy_to_iso"}],
                     "max_wrong": 3, "wrong_counts_rules": [], "fail": {"outcome": "not_verified",
                                                                         "reason": "failed", "say": "x"}},
        "products": {"max_count": 1, "types": {"credit_card": {"speak_as": "card", "floor_field": "minimum_payment",
                                                               "full_field": "past_due"}}, "order": "as_returned"},
        "bands": [{"name": "b1", "min_days_late": 1, "max_days_late": 9,
                   "deadline": {"kind": "nth_business_day", "n": 4, "counted_from": "today"}, "attempts": []}],
        "callback": {"days": ["today"], "start": "9:00", "end": "17:00", "end_inclusive": True,
                     "must_be_after_now": True},
    }
    p2 = {
        "amounts": {"min": "floor", "max": "full", "below_min": "reject", "ask_open_amount": False, "rules": []},
        "dates": {"customer_date_after_deadline": "", "vague_date": "", "rules": []},
        "promise": {"readback": "", "confirm_required": True, "retry_on_failure": "1", "success_say": "",
                    "failure_say": "", "per_product": True},
        "triggers": [{"name": "dispute", "detect": "not mine", "action": "close", "outcome": "no_promise",
                      "reason": "dispute", "precedence": 1}],
        "notes": {"tool_reasons": [], "when": "", "forbidden_on_transfer": False},
        "outcomes": {"table": [{"outcome": "promise", "reason": None, "when": "x"}], "precedence": [],
                     "multi_product": ""},
        "phrases": {"bye": "Goodbye."},
    }
    return p1, p2


class ExtractFlowOffline(unittest.TestCase):
    """extract_policy with the model mocked: replicates, diff, resolve, rules fallback, validation."""

    def run_flow(self, disagree):
        p1, p2 = _fake_parts()
        calls = []

        def fake_ask_json(prompt, *, system=None, model=None, purpose, max_turns=1, timeout_s=0, retries=1,
                          deadline=None):
            calls.append(purpose)
            if purpose.startswith("extract policy part 1"):
                out = copy.deepcopy(p1)
                if disagree and "replicate 2" in purpose:
                    out["calendar"]["holidays"].append("2030-01-03")
                return out
            if purpose.startswith("extract policy part 2"):
                return copy.deepcopy(p2)
            if purpose.startswith("extract verbatim rules"):
                return {"rules": [{"id": "doc.1", "text": "Rule one."}]}
            if purpose.startswith("resolve"):
                self.assertIn("2030-01-03", prompt)
                return {"calendar": {"value": dict(p1["calendar"], holidays=["2030-01-02", "2030-01-03"]),
                                     "evidence": ["quote"]}}
            raise AssertionError(purpose)

        with tempfile.TemporaryDirectory() as d:
            brief = pathlib.Path(d) / "doc.md"
            brief.write_text("1. Rule one.\n")
            with mock.patch.object(llm, "ask_json", side_effect=fake_ask_json):
                warnings = []
                pol = extract.extract_policy([str(brief)], OPENAPI, warnings=warnings)
        return pol, calls, warnings

    def test_agreeing_replicates(self):
        pol, calls, warnings = self.run_flow(disagree=False)
        self.assertEqual(len(calls), 5)
        self.assertFalse(any(c.startswith("resolve") for c in calls))
        self.assertEqual(list(pol), extract.TOP_KEYS)
        self.assertEqual(pol["callback"]["start"], "09:00")
        self.assertEqual(pol["promise"]["retry_on_failure"], 1)
        self.assertEqual(set(pol["tools"]), TOOL_NAMES)
        self.assertEqual(pol["source"]["briefs"], ["doc.md"])
        self.assertEqual(extract.local_validate(pol), [])
        self.assertEqual(pol["rules"], [{"id": "doc.1", "text": "Rule one."}])

    def test_disagreement_triggers_resolve(self):
        pol, calls, warnings = self.run_flow(disagree=True)
        self.assertTrue(any(c.startswith("resolve disagreement on calendar") for c in calls))
        self.assertEqual(pol["calendar"]["holidays"], ["2030-01-02", "2030-01-03"])

    def test_diff_ignores_wording(self):
        a = {"triggers": [{"name": "x", "detect": "says A", "action": "close", "outcome": "o", "reason": "r"}]}
        b = {"triggers": [{"name": "y", "detect": "says B", "action": "close", "outcome": "o", "reason": "r"}]}
        self.assertEqual(extract.diff_replicates(a, b, ["triggers"]), {})
        b["triggers"][0]["reason"] = "q"
        self.assertIn("triggers", extract.diff_replicates(a, b, ["triggers"]))


class Validation(unittest.TestCase):
    def test_local_validate_reports(self):
        probs = extract.local_validate({"calendar": {"holidays": ["26/10/2026"], "weekend": [8]},
                                        "callback": {"start": "10"}, "bands": [{"min_days_late": "1"}]})
        text = "\n".join(probs)
        for needle in ("missing top-level key: tools", "not ISO", "bad weekday", "callback.start",
                       "bands[0].min_days_late", "identity.max_wrong"):
            self.assertIn(needle, text)

    def test_cross_check_enums(self):
        tools = extract.parse_openapi(OPENAPI)["tools"]
        w = []
        extract.cross_check({"tools": tools, "outcomes": {"table": [{"outcome": "bogus"}]},
                             "triggers": [{"name": "t", "queue": "nowhere"}],
                             "identity": {"questions": [{"tool_field": "mothers_maiden"}]},
                             "products": {"types": {"boat_loan": {}}}}, w)
        text = "\n".join(w)
        for needle in ("bogus", "nowhere", "mothers_maiden", "boat_loan", "never listed"):
            self.assertIn(needle, text)


class RawMaterial(unittest.TestCase):
    def test_eml_csv_json_and_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "1-mail.eml").write_bytes(
                b"From: ops@bank.test\r\nSubject: Policy\r\nMIME-Version: 1.0\r\n"
                b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/plain\r\n\r\n"
                b"Callbacks run 09:00-17:00.\r\n--XX\r\nContent-Type: text/csv\r\n"
                b"Content-Disposition: attachment; filename=codes.csv\r\n\r\ncode;meaning\r\nA1;paid\r\n--XX--\r\n")
            (d / "2-memo.txt").write_text("MEMO: callbacks now end at 18:00.")
            (d / "3-codes.csv").write_text("code,meaning\nZ9,dispute\n")
            (d / "4-x.json").write_text('{"a": 1}')
            (d / "5-blob.bin").write_bytes(b"\xff\xfe\x00weird")
            docs = extract.read_documents([d])
            names = [n for n, _ in docs]
            self.assertEqual(names, ["1-mail.eml", "2-memo.txt", "3-codes.csv", "4-x.json", "5-blob.bin"])
            mail = docs[0][1]
            self.assertIn("Subject: Policy", mail)
            self.assertIn("09:00-17:00", mail)
            self.assertIn("A1 | paid", mail)
            self.assertIn("Z9 | dispute", docs[2][1])
            self.assertFalse(docs.order_known)  # folder without dates: name order, flagged unknown
            block = extract.documents_block(docs)
            self.assertIn('name="2-memo.txt"', block)
            self.assertIn("order of these documents is not known", block)
            self.assertIn("latest dated", block)

    def test_numbered_rules_fallback(self):
        rules = extract.numbered_rules([("brief-a.md", BRIEF_A.read_text())])
        self.assertEqual(len(rules), 47)
        self.assertEqual(rules[0]["id"], "brief-a.1")
        r30 = next(r for r in rules if r["id"] == "brief-a.30")
        self.assertIn("Attempt 2, late band only", r30["text"])


class LlmWrapper(unittest.TestCase):
    def test_parse_json_text(self):
        self.assertEqual(llm.parse_json_text('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.parse_json_text('Here: {"a": [1, 2]} done'), {"a": [1, 2]})
        with self.assertRaises(ValueError):
            llm.parse_json_text("nothing")

    def test_hook_records_success_and_failure(self):
        recs = []
        hook = llm.on_call(recs.append)
        try:
            with mock.patch.object(llm, "_backend", return_value="cli"), \
                    mock.patch.object(llm, "_ask_cli", return_value='{"ok": 1}'):
                self.assertEqual(llm.ask_json("p", purpose="unit"), {"ok": 1})
            with mock.patch.object(llm, "_backend", return_value="cli"), \
                    mock.patch.object(llm, "_ask_cli", side_effect=llm.LLMError("boom")):
                with self.assertRaises(llm.LLMError):
                    llm.ask("p", purpose="unit fail")
        finally:
            llm.remove_hook(hook)
        self.assertEqual([r["ok"] for r in recs], [True, False])
        for r in recs:
            for k in ("model", "purpose", "start", "duration_s", "ok"):
                self.assertIn(k, r)
        self.assertEqual(recs[0]["model"], llm.default_model())

    def test_model_env(self):
        with mock.patch.dict(os.environ, {"BUILDER_MODEL": "m-x"}):
            self.assertEqual(llm.default_model(), "m-x")


class ReviewFixes(unittest.TestCase):
    def test_allof_with_sibling_properties(self):
        spec = {"components": {"schemas": {"Base": {"type": "object", "required": ["customer_id"],
                                                    "properties": {"customer_id": {"type": "string"}}}}},
                "paths": {"/t": {"post": {"operationId": "t", "requestBody": {"content": {"application/json": {
                    "schema": {"allOf": [{"$ref": "#/components/schemas/Base"}],
                               "properties": {"extra": {"type": "integer"}}, "required": ["extra"]}}}}}}}}
        f = extract.parse_openapi(spec)["tools"]["t"]["fields"]
        self.assertEqual(set(f), {"customer_id", "extra"})
        self.assertTrue(f["extra"]["required"] and f["customer_id"]["required"])

    def test_oneof_skips_null_and_unions_enums(self):
        spec = {"paths": {"/t": {"post": {"operationId": "t", "requestBody": {"content": {"application/json": {
            "schema": {"type": "object", "properties": {"r": {"anyOf": [
                {"type": "null"}, {"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["c"]},
                {"const": "d"}]}}}}}}}}}}
        r = extract.parse_openapi(spec)["tools"]["t"]["fields"]["r"]
        self.assertEqual(r["type"], "string")
        self.assertEqual(r["enum"], ["a", "b", "c", "d"])

    def test_weekend_holidays_as_strings(self):
        cal = extract.normalise_section("calendar", {"weekend": "Saturday, Sunday",
                                                     "holidays": "2030-01-02; 3 March 2030\n2030-12-24"}, [])
        self.assertEqual(cal["weekend"], [6, 7])
        self.assertEqual(cal["holidays"], ["2030-01-02", "2030-03-03", "2030-12-24"])

    def test_month_first_fallback(self):
        self.assertEqual(extract.norm_date("12/25/2026"), "2026-12-25")
        self.assertEqual(extract.norm_date("05/06/2026"), "2026-06-05")  # ambiguous: day first
        self.assertIsNone(extract.norm_date("13/13/2026"))

    def test_raw_files_given_explicitly_keep_order(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "mail.eml").write_bytes(b"From: a@b.test\r\nDate: Mon, 02 Mar 2026 09:00:00 +0000\r\n"
                                         b"Subject: Rules\r\n\r\nWindow 08:00-16:00.\r\n")
            (d / "codes.csv").write_text("code;label\nX1;alpha\nX2;beta\n")
            (d / "memo.txt").write_text("Date: 2026-03-05\nMEMO: window now ends 15:00.\n")
            docs = extract.read_documents([d / "memo.txt", d / "mail.eml", d / "codes.csv"])
            self.assertEqual([n for n, _ in docs], ["memo.txt", "mail.eml", "codes.csv"])
            self.assertTrue(docs.order_known)
            self.assertIn("Window 08:00-16:00.", docs[1][1])
            self.assertIn("X2 | beta", docs[2][1])
            self.assertIn("MEMO", docs[0][1])
            self.assertEqual(docs.dates["mail.eml"], "2026-03-02 09:00:00")
            self.assertEqual(docs.dates["memo.txt"], "2026-03-05 00:00:00")
            block = extract.documents_block(docs)
            self.assertIn('order="earliest"', block)
            self.assertIn('date="2026-03-02 09:00:00"', block)

    def test_folder_skips_dotfiles_and_sorts_by_date(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1\x00")
            (d / ".hidden").mkdir()
            (d / ".hidden" / "x.txt").write_text("Date: 2026-01-01\nignore me")
            (d / "a-memo.txt").write_text("Date: 2026-03-05\nMEMO overrides.")
            (d / "b-mail.eml").write_bytes(b"From: a@b.test\r\nDate: Mon, 02 Mar 2026 09:00:00 +0000\r\n"
                                           b"Subject: Rules\r\n\r\nBody.\r\n")
            (d / "c-codes.csv").write_text("Date: 2026-03-01\ncode,label\nX1,alpha\n")
            docs = extract.read_documents([d])
            self.assertEqual([n for n, _ in docs], ["c-codes.csv", "b-mail.eml", "a-memo.txt"])
            self.assertTrue(docs.order_known)
            (d / "d-undated.txt").write_text("no date here")
            docs = extract.read_documents([d])
            self.assertEqual([n for n, _ in docs], ["a-memo.txt", "b-mail.eml", "c-codes.csv", "d-undated.txt"])
            self.assertFalse(docs.order_known)
            # an explicitly passed .DS_Store is ignored too
            self.assertEqual(len(extract.read_documents([d / ".DS_Store"])), 0)

    def test_no_brief_words_in_builder_code(self):
        words = ["wrong number", "wrong_number", "deceased", "restructuring", "hardship", "already paid", "scam",
                 "sympathy", "robot", "credit card", "personal loan", "car loan", "2026-", "10:00", "19:00",
                 "salary", "minimum_payment", "instalment", "date_of_birth", "phone_last4", "day_month_swapped"]
        for f in (KIT / "builder").glob("*.py"):
            if f.name not in ("extract.py", "llm.py", "__init__.py"):
                continue
            text = f.read_text().lower()
            for w in words:
                self.assertFalse(w.lower() in text, "%s contains %r" % (f.name, w))


class HoldoutClasses(unittest.TestCase):
    """Offline checks for the mismatch classes found on the S1-S4 holdout (T19)."""

    def test_attempt_date_expressions(self):
        cases = {"third working day after today": "nth_business_day:3", "3rd business day": "nth_business_day:3",
                 "in 5 banking days": "nth_business_day:5", "next business day after today": "next_business_day",
                 "nth_business_day 4": "nth_business_day:4", "deadline": "deadline",
                 "last working day of this week": "last working day of this week"}
        for raw, want in cases.items():
            self.assertEqual(extract.date_expr(raw), want, raw)

    def test_numbers_in_words(self):
        self.assertEqual(extract._number("three thousand"), 3000)
        self.assertEqual(extract._number("two hundred and fifty"), 250)
        self.assertEqual(extract._number("EUR 3,000"), 3000)
        self.assertIsNone(extract._number("lots"))
        trig = extract.normalise_section("triggers", [{"name": "big", "action": "transfer", "condition": {
            "over": "sum of all products", "field": "past_due", "op": "more than", "value": "three thousand"}}], [])
        self.assertEqual(trig[0]["condition"], {"over": "all_products_sum", "field": "past_due", "op": ">",
                                                "value": 3000})

    def test_floor_expression(self):
        self.assertEqual(extract.eval_amount_expr("min(50, past_due)", {"past_due": 35.0}), 35.0)
        self.assertEqual(extract.eval_amount_expr("min(50, past_due)", {"past_due": 380}), 50.0)
        self.assertEqual(extract.eval_amount_expr("max(10%, 0) * 100", {}), 10.0)
        self.assertIsNone(extract.eval_amount_expr("__import__('os').system('x')", {}))
        self.assertIsNone(extract.eval_amount_expr("unknown_field + 1", {}))

    def test_scope_weekday_window_next_weekday(self):
        idn = extract.normalise_section("identity", {"questions": [], "max_wrong": "2",
                                                     "max_wrong_scope": "per question"}, [])
        self.assertEqual((idn["max_wrong"], idn["max_wrong_scope"]), (2, "per_question"))
        self.assertEqual(extract.normalise_section("identity", {"max_wrong": 3}, [])["max_wrong_scope"], "total")
        cb = extract.normalise_section("callback", {"start": "8", "end": "20:00",
                                                    "per_weekday": {"Friday": {"start": "8am", "end": "4pm"}}}, [])
        self.assertEqual(cb["per_weekday"], {"5": {"start": "08:00", "end": "16:00"}})
        self.assertEqual(extract.normalise_section("dates", {"next_weekday": "first occurrence"}, [])["next_weekday"],
                         "next_occurrence")
        self.assertEqual(extract.normalise_section("dates", {"next_weekday": "following week"}, [])["next_weekday"],
                         "following_week")

    def test_closing_section_and_reasonless_close_tool(self):
        tools = {"close_call": {"fields": {"customer_id": {"type": "string", "enum": None, "required": True},
                                           "code": {"type": "string", "enum": ["OK1", "NO1", "ID1"],
                                                    "required": True}}}}
        policy = {
            "tools": tools,
            "outcomes": {"table": [{"outcome": "OK1", "reason": "promise booked"},
                                   {"outcome": "NO1", "reason": "refused"}, {"outcome": "NO1", "reason": "x"}]},
            "identity": {"fail": {"outcome": "ID1", "reason": "too many wrong answers"}},
            "third_party": {"cases": [{"outcome": "NO1", "reason": "busy"}]},
            "triggers": [{"name": "t", "outcome": "NO1", "reason": "dispute", "note_reason": "N-X"}],
            "closing": extract.normalise_section("closing", {
                "promise": ["OK1", "booked"], "note_map": {"N-X": "NO1 / dispute"}, "transfer_overrides_promise": "yes"},
                []),
        }
        self.assertEqual(extract.close_tool(policy), ("close_call", "code", False))
        w = []
        extract.align_close_reasons(policy, w)
        self.assertEqual(policy["outcomes"]["table"], [{"outcome": "OK1", "reason": None},
                                                       {"outcome": "NO1", "reason": None}])
        self.assertIsNone(policy["identity"]["fail"]["reason"])
        self.assertIsNone(policy["third_party"]["cases"][0]["reason"])
        self.assertIsNone(policy["triggers"][0]["reason"])
        self.assertEqual(policy["triggers"][0]["note_reason"], "N-X")
        self.assertEqual(policy["closing"]["note_map"], {"N-X": {"outcome": "NO1", "reason": None}})
        self.assertTrue(policy["closing"]["transfer_overrides_promise"])
        self.assertTrue(w)
        # the kit's close tool has a reason field: nothing is dropped
        kit = extract.parse_openapi(OPENAPI)["tools"]
        p2 = {"tools": kit, "outcomes": {"table": [{"outcome": "promise", "reason": None}]}}
        self.assertTrue(extract.close_tool(p2)[2])


class EvalScorer(unittest.TestCase):
    """tools/eval_extract.py scores the saved holdout policies (skipped when they are missing)."""

    def test_saved_holdout_policies_match_gold(self):
        sys.path.insert(0, str(KIT / "tools"))
        import eval_extract
        for bank in ("S1", "S2", "S3", "S4"):
            if not (KIT / "policies" / ("%s.policy.json" % bank)).exists():
                self.skipTest("policies/%s.policy.json not generated" % bank)
            facts, _briefs, _api, cases = eval_extract.bank_inputs(bank)
            policy = json.loads((KIT / "policies" / ("%s.policy.json" % bank)).read_text())
            rows = eval_extract.Scorer(bank, facts, policy, cases).run()
            bad = [r for r in rows if not r[2]]
            self.assertEqual(bad, [], bank)


class TimeBudget(unittest.TestCase):
    """A slow model (mocked) must not push extraction past the budget (scaled down: 4 min -> 3 s)."""

    def _run(self, mode, budget=3.0):
        p1, p2 = _fake_parts()
        calls = []

        def slow_ask(prompt, *, system=None, model=None, purpose, max_turns=1, timeout_s=0):
            start = time.time()
            calls.append((purpose, start, timeout_s))
            if purpose.startswith("resolve") or "(JSON repair)" in purpose:
                latency, answer = 60.0, "{}"          # far beyond any budget
            elif purpose.startswith("extract policy part 1"):
                latency, answer = 0.8, json.dumps(p1)
                if "replicate 2" in purpose:
                    if mode == "repair":
                        answer = "not json at all"   # forces a repair attempt
                    else:                            # disagreeing replicate forces a resolve call
                        answer = json.dumps(dict(p1, calendar=dict(p1["calendar"], holidays=[])))
            elif purpose.startswith("extract policy part 2"):
                latency, answer = 0.8, json.dumps(p2)
            else:
                latency, answer = 0.5, json.dumps({"rules": [{"id": "x.1", "text": "t"}]})
            if latency > timeout_s:
                time.sleep(max(0.0, timeout_s))
                raise llm.LLMError("timed out after %.2fs" % timeout_s)
            time.sleep(latency)
            return answer

        with tempfile.TemporaryDirectory() as d:
            brief = pathlib.Path(d) / "doc.md"
            brief.write_text("1. Rule one.\n")
            with mock.patch.object(llm, "ask", side_effect=slow_ask), \
                    mock.patch.object(llm, "MIN_REPAIR_S", 0.1), \
                    mock.patch.object(extract, "MIN_RESOLVE_S", 0.1), \
                    mock.patch.object(extract, "END_MARGIN_S", 0.1):
                t0 = time.time()
                warnings = []
                pol = extract.extract_policy([str(brief)], OPENAPI, budget_s=budget, warnings=warnings)
                elapsed = time.time() - t0
        self.assertLess(elapsed, budget)
        for purpose, start, timeout in calls:
            self.assertLessEqual(start + timeout, t0 + budget + 0.05, purpose)
        self.assertEqual(set(pol["tools"]), TOOL_NAMES)
        return calls, warnings

    def test_budget_holds_with_slow_repair(self):
        calls, _ = self._run("repair")
        self.assertTrue(any("(JSON repair)" in c[0] for c in calls))

    def test_budget_holds_with_slow_resolve(self):
        calls, warnings = self._run("resolve")
        self.assertTrue(any(c[0].startswith("resolve") for c in calls))
        self.assertTrue(any("resolve call failed" in w for w in warnings))

    def test_repair_skipped_when_little_time_left(self):
        with mock.patch.object(llm, "ask", return_value="garbage") as ask:
            with self.assertRaises(llm.LLMError) as cm:
                llm.ask_json("p", purpose="x", timeout_s=100, deadline=time.time() + 5)
        self.assertEqual(ask.call_count, 1)
        self.assertIn("repair skipped", str(cm.exception))
        self.assertLessEqual(ask.call_args.kwargs["timeout_s"], 5)


# ------------------------------------------------------------------ facts (live / saved outputs)
# Brief facts live ONLY here (tests), never in builder code or prompts.
HOLIDAYS = ["2026-10-26", "2026-11-11", "2026-11-26", "2026-12-25", "2027-01-01"]


def check_facts(tc, pol, multi):
    tc.assertEqual(sorted(pol["calendar"]["holidays"]), HOLIDAYS)
    tc.assertEqual(sorted(pol["calendar"]["weekend"]), [6, 7])
    bands = sorted(pol["bands"], key=lambda b: b["min_days_late"])
    tc.assertEqual([(b["min_days_late"], b["max_days_late"], b["deadline"]["n"], len(b["attempts"]))
                    for b in bands], [(1, 7, 12, 1), (8, 45, 7, 2)])
    for b in bands:
        tc.assertEqual(b["deadline"]["counted_from"], "today")
    cb = pol["callback"]
    tc.assertEqual((cb["start"], cb["end"], cb["end_inclusive"]), ("10:00", "19:00", True))
    tc.assertEqual(pol["identity"]["max_wrong"], 3)
    tc.assertEqual([q["tool_field"] for q in pol["identity"]["questions"]], ["date_of_birth", "phone_last4"])
    tc.assertEqual(pol["products"]["max_count"], 3 if multi else 1)
    pairs = {(r["outcome"], r["reason"]) for r in pol["outcomes"]["table"]}
    for pair in [("promise", None), ("paid", None), ("transferred", "restructuring"), ("no_promise", "dispute"),
                 ("not_verified", "declined"), ("not_reached", "deceased")]:
        tc.assertIn(pair, pairs)
    tc.assertEqual(extract.local_validate(pol), [])
    tc.assertGreaterEqual(len(pol["rules"]), 47 + (11 if multi else 0))


class SavedPolicyFacts(unittest.TestCase):
    def _load(self, name):
        p = KIT / "policies" / name
        if not p.exists():
            self.skipTest("%s not generated yet" % p)
        return json.loads(p.read_text())

    def test_brief_a(self):
        check_facts(self, self._load("brief-a.policy.json"), multi=False)

    def test_brief_b(self):
        check_facts(self, self._load("brief-b.policy.json"), multi=True)


@unittest.skipUnless(os.environ.get("EXTRACT_LIVE"), "set EXTRACT_LIVE=1 for live model calls")
class LiveFacts(unittest.TestCase):
    def test_brief_a_live(self):
        check_facts(self, extract.extract_policy([BRIEF_A], OPENAPI), multi=False)

    def test_brief_b_live(self):
        check_facts(self, extract.extract_policy([BRIEF_A, BRIEF_B], OPENAPI), multi=True)


if __name__ == "__main__":
    unittest.main()
