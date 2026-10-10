"""Offline tests of builder/oracle.py (reference logic) and builder/policy_schema.py.

    cd build-the-builder && python3 -m unittest tests.test_oracle -v

Brief values are fine here (tests only). fixture-a mirrors Brief A/B; fixture-x is a different bank (other
weekend, holidays, bands, deadline kinds, callback window, outcome names, field names) to prove genericity.
"""
import datetime
import json
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import oracle as O  # noqa: E402
from builder import policy_schema as S  # noqa: E402

FIX = KIT / "tests" / "fixtures_policy"


def load(name):
    return S.normalise(json.loads((FIX / (name + ".policy.json")).read_text()))


D = datetime.date


class BriefA(unittest.TestCase):
    def setUp(self):
        self.p = load("fixture-a")
        self.cal = O.make_calendar(self.p)
        self.today = D(2026, 10, 14)  # Wednesday

    def test_now_parsing(self):
        self.assertEqual(O.parse_now("2026-10-14T11:00"), (D(2026, 10, 14), "11:00"))
        self.assertEqual(O.parse_now("2026-10-14 09:05:00Z"), (D(2026, 10, 14), "09:05"))
        self.assertEqual(O.parse_now("14.10.2026 11:00"), (D(2026, 10, 14), "11:00"))
        self.assertEqual(O.parse_now("garbage"), (None, ""))

    def test_business_days(self):
        self.assertTrue(O.is_business_day(D(2026, 10, 14), self.cal))
        self.assertFalse(O.is_business_day(D(2026, 10, 17), self.cal))   # Saturday
        self.assertFalse(O.is_business_day(D(2026, 10, 26), self.cal))   # holiday
        self.assertEqual(O.next_business_day(D(2026, 10, 23), self.cal), D(2026, 10, 27))

    def test_bands(self):
        self.assertEqual(O.band_for(5, self.p)["name"], "early")
        self.assertEqual(O.band_for(7, self.p)["name"], "early")
        self.assertEqual(O.band_for(8, self.p)["name"], "late")
        self.assertEqual(O.band_for(45, self.p)["name"], "late")
        self.assertIsNone(O.band_for(46, self.p))
        self.assertIsNone(O.band_for(0, self.p))

    def test_deadlines(self):
        # late band: 7th business day after Wed 14 Oct -> Fri 23 Oct
        self.assertEqual(O.deadline(self.p, 14, "2026-10-14T11:00"), "2026-10-23")
        # early band: 12th business day after today. Mon 26 Oct is a holiday and is skipped while counting:
        # 15,16,19,20,21,22,23,27,28,29,30 (11) -> Mon 2 Nov. (The brief's PUB-07 promise on 27 Oct is a customer
        # date rolled off the holiday, inside this deadline; PUB-13 records 30 Oct for the early-band card.)
        self.assertEqual(O.deadline(self.p, 5, "2026-10-14T11:00"), "2026-11-02")
        self.assertEqual(O.deadline(self.p, 3, "2026-10-16T09:00"), "2026-11-04")   # from a Friday

    def test_holiday_roll_and_promise_checks(self):
        prod = O.products_for_case(self.p, [{"product_id": "p1", "type": "personal_loan", "past_due": 540.0,
                                             "days_late": 5, "instalment_amount": 180.0}], "2026-10-14T11:00")[0]
        chk = O.check_promise(prod, 540, "2026-10-26", self.today, self.cal)
        self.assertEqual(chk["code"], "not_business_day")
        self.assertEqual(chk["suggest_date"], "2026-10-27")
        self.assertTrue(O.check_promise(prod, 540, "2026-10-27", self.today, self.cal)["ok"])
        self.assertEqual(O.check_promise(prod, 179.99, "2026-10-27", self.today, self.cal)["code"], "below_floor")
        self.assertTrue(O.check_promise(prod, 180, "2026-10-14", self.today, self.cal)["ok"])
        self.assertEqual(O.check_promise(prod, 541, "2026-10-27", self.today, self.cal)["code"], "above_full")
        self.assertEqual(O.check_promise(prod, 540, "2026-10-13", self.today, self.cal)["code"], "before_today")
        self.assertEqual(O.check_promise(prod, 540, "2026-11-03", self.today, self.cal)["code"], "after_deadline")
        self.assertEqual(O.check_promise(prod, 540, "2026-10-27", self.today, self.cal, ["p1"])["code"], "already_promised")
        self.assertEqual(O.check_promise(None, 540, "2026-10-27", self.today, self.cal)["code"], "unknown_product")

    def test_brief_b_two_deadlines(self):
        prods = O.products_for_case(self.p, [
            {"product_id": "p1", "type": "credit_card", "card_last4": "6604", "past_due": 300.0, "days_late": 4, "minimum_payment": 90.0},
            {"product_id": "p2", "type": "personal_loan", "past_due": 900.0, "days_late": 20, "instalment_amount": 300.0}],
            "2026-10-14T11:00")
        self.assertEqual(prods[0]["name"], "credit card ending 6604")
        self.assertEqual(prods[0]["deadline"], "2026-11-02")
        self.assertEqual(prods[1]["deadline"], "2026-10-23")
        self.assertTrue(O.check_promise(prods[0], 300, "2026-10-30", self.today, self.cal)["ok"])
        c = O.check_promise(prods[1], 900, "2026-10-30", self.today, self.cal)
        self.assertEqual((c["code"], c["suggest_date"]), ("after_deadline", "2026-10-23"))

    def test_offers(self):
        prod = O.products_for_case(self.p, [{"product_id": "p1", "type": "credit_card", "card_last4": "1903", "past_due": 2125.1,
                                             "days_late": 12, "minimum_payment": 637.5}], "2026-10-14T11:00")[0]
        self.assertEqual(prod["band"], "late")
        self.assertEqual([(o["amount"], o["date"]) for o in prod["offers"]], [(2125.1, "2026-10-16"), (637.5, "2026-10-23")])
        # said on a Friday: attempt 1 moves to the last business day of next week
        prod = O.products_for_case(self.p, [{"product_id": "p1", "type": "car_loan", "past_due": 75.0, "days_late": 3,
                                             "instalment_amount": 25.0}], "2026-10-16T10:00")[0]
        self.assertEqual(prod["offers"][0]["date"], "2026-10-23")

    def test_end_of_week(self):
        self.assertEqual(O.end_of_week_business_day(D(2026, 10, 14), self.cal), D(2026, 10, 16))
        self.assertEqual(O.end_of_week_business_day(D(2026, 10, 16), self.cal), D(2026, 10, 23))
        # week whose Friday is a holiday (25 Dec 2026): last business day is Thursday 24
        self.assertEqual(O.end_of_week_business_day(D(2026, 12, 21), self.cal), D(2026, 12, 24))

    def test_spoken_dates(self):
        fri = D(2026, 10, 16)
        self.assertEqual(O.parse_spoken_date("next Friday", fri, "following_week"), D(2026, 10, 23))
        self.assertEqual(O.parse_spoken_date("this Friday", self.today, "following_week"), D(2026, 10, 16))
        self.assertEqual(O.parse_spoken_date("next Friday", self.today, "following_week"), D(2026, 10, 23))
        self.assertEqual(O.parse_spoken_date("next Friday", self.today, "next_occurrence"), D(2026, 10, 16))
        self.assertEqual(O.parse_spoken_date("Monday the 26th", self.today), D(2026, 10, 26))
        self.assertEqual(O.parse_spoken_date("the 30th of October", self.today), D(2026, 10, 30))
        self.assertEqual(O.parse_spoken_date("6 November", self.today), D(2026, 11, 6))
        self.assertEqual(O.parse_spoken_date("tomorrow", self.today), D(2026, 10, 15))
        self.assertEqual(O.parse_spoken_date("2026-10-23", self.today), D(2026, 10, 23))
        self.assertIsNone(O.parse_spoken_date("when my salary comes", self.today))

    def test_fix_year(self):
        t = D(2026, 10, 14)
        self.assertEqual(O.fix_year(D(2025, 11, 2), t), D(2026, 11, 2))      # PUB-13 corrects: model wrote 2025
        self.assertEqual(O.fix_year(D(2025, 10, 1), t), D(2026, 10, 1).replace(year=2027))
        self.assertEqual(O.fix_year(D(2030, 10, 30), t), D(2026, 10, 30))
        self.assertEqual(O.fix_year(D(2026, 10, 30), t), D(2026, 10, 30))
        self.assertEqual(O.fix_year(D(2026, 10, 13), t), D(2026, 10, 13))    # same year in the past: a real error, left alone

    def test_identity_normalise(self):
        self.assertEqual(O.normalise_answer("14 March 1988", "dmy_to_iso"), "1988-03-14")
        self.assertEqual(O.normalise_answer("March 14th, 1988", "dmy_to_iso"), "1988-03-14")
        self.assertEqual(O.normalise_answer("the fourteenth of March 1988", "dmy_to_iso"), "1988-03-14")
        self.assertEqual(O.normalise_answer("14/03/1988", "dmy_to_iso"), "1988-03-14")
        self.assertEqual(O.normalise_answer("05/06/1987", "dmy_to_iso"), "1987-06-05")   # day first
        self.assertEqual(O.normalise_answer("3/14/1988", "dmy_to_iso"), "1988-03-14")    # impossible d/m -> m/d
        self.assertEqual(O.normalise_answer("05/06/1987", "mdy_to_iso"), "1987-05-06")
        self.assertEqual(O.normalise_answer("1988-03-14", "dmy_to_iso"), "1988-03-14")
        self.assertEqual(O.normalise_answer("44 71", "digits_only"), "4471")
        self.assertEqual(O.normalise_answer("no idea", "dmy_to_iso"), "no idea")
        # no order in the policy: unambiguous forms are converted, ambiguous numbers are left alone
        self.assertEqual(O.normalise_answer("14 March 1988", "none", "date"), "1988-03-14")
        self.assertEqual(O.normalise_answer("14/03/1988", "none", "date"), "1988-03-14")
        self.assertEqual(O.normalise_answer("05/06/1987", "none", "date"), "05/06/1987")
        self.assertIsNone(O.parse_spoken_date("next Friday", self.today))     # meaning not given by the policy

    def test_callback_window(self):
        f = lambda d, t, now="11:00": O.check_callback(d, t, self.today, now, self.p, self.cal)
        self.assertTrue(f("2026-10-15", "11:00")["ok"])
        self.assertTrue(f("2026-10-15", "19:00")["ok"])           # end included
        self.assertTrue(f("2026-10-15", "10:00")["ok"])
        self.assertFalse(f("2026-10-15", "19:01")["ok"])
        self.assertFalse(f("2026-10-15", "9:59")["ok"])
        self.assertTrue(f("2026-10-14", "15:00")["ok"])           # today, after now
        self.assertEqual(f("2026-10-14", "10:30")["code"], "not_after_now")
        self.assertEqual(f("2026-10-16", "11:00")["code"], "day_not_allowed")
        self.assertTrue(f("tomorrow", "2 pm")["ok"])
        # from a Friday the next business day is Monday
        r = O.check_callback("2026-10-19", "10:00", D(2026, 10, 16), "11:00", self.p, self.cal)
        self.assertTrue(r["ok"])
        self.assertEqual(O.parse_time("11"), "11:00")
        self.assertEqual(O.parse_time("2:30 pm"), "14:30")
        self.assertEqual(O.parse_time("12am"), "00:00")

    def test_closing(self):
        c = lambda **f: O.compute_closing(f, self.p)
        both = ["p1", "p2"]
        self.assertEqual(c(verified=True, promises=["p1"], products=["p1"])["outcome"], "promise")
        self.assertEqual(c(verified=True, promises=["p1"], products=["p1"])["reason"], "")
        r = c(verified=True, promises=["p1"], transfers=["restructuring"], products=both)
        self.assertEqual((r["outcome"], r["reason"]), ("transferred", "restructuring"))
        r = c(verified=False, id_failed=True, llm=dict(outcome="not_verified", reason="declined"))
        self.assertEqual((r["outcome"], r["reason"]), ("not_verified", "failed"))
        r = c(verified=True, notes=dict(p1="paid", p2="paid"), products=both)
        self.assertEqual((r["outcome"], r["reason"]), ("paid", ""))
        r = c(verified=True, notes=dict(p1="paid", p2="dispute"), products=both)
        self.assertEqual((r["outcome"], r["reason"]), ("no_promise", "dispute"))
        r = c(verified=True, notes=dict(p1="refused", p2="hardship"), products=both)
        self.assertEqual((r["outcome"], r["reason"]), ("no_promise", "refused"))
        r = c(verified=True, products=["p1"], llm=dict(outcome="no_promise", reason="hardship"))
        self.assertEqual((r["outcome"], r["reason"], r["source"]), ("no_promise", "hardship", "llm"))
        r = c(verified=True, products=["p1"], llm=dict(outcome="paid", reason="whatever"))
        self.assertEqual((r["outcome"], r["reason"]), ("paid", ""))
        r = c(verified=False, callback_done=True)
        self.assertEqual((r["outcome"], r["reason"]), ("not_reached", "callback"))
        r = c(verified=True, products=["p1"])
        self.assertEqual((r["outcome"], r["reason"], r["source"]), ("no_promise", "refused", "default"))

    def test_public_cases_expected_promises_are_valid(self):
        """Every expected promise of the kit's public cases passes the oracle's checks (T03 policies too)."""
        cases = json.loads((KIT / "public-test-set.json").read_text())
        for pol in ("fixture-a", "brief-b"):
            p = load(pol)
            cal = O.make_calendar(p)
            for case in cases:
                today, _t = O.parse_now(case["variables"]["now"])
                raw = [dict(pr, product_id="p%d" % (i + 1)) for i, pr in enumerate(case["products"])]
                prods = O.products_for_case(p, raw, case["variables"]["now"])
                for exp in case["expected"]["promises"]:
                    prod = [x for x in prods if x["id"] == exp["product_id"]][0]
                    r = O.check_promise(prod, exp["amount"], exp["date"], today, cal)
                    self.assertTrue(r["ok"], (pol, case["id"], exp, r))


class LiveRegressions(unittest.TestCase):
    """Live run of the extractor's Brief A policy (brief-a-live): PUB-06 recorded the deadline instead of attempt 1's
    Friday, PUB-07 jumped to the deadline for a holiday instead of the next business day."""

    def setUp(self):
        self.p = load("brief-a-live")
        self.cal = O.make_calendar(self.p)

    def test_compound_attempt_date_expression(self):
        expr = self.p["bands"][1]["attempts"][0]["date"]
        self.assertIn(";", expr)                       # the extractor's compound form
        pr = O.products_for_case(self.p, [{"product_id": "p1", "type": "credit_card", "card_last4": "1903", "past_due": 2125.1,
                                            "days_late": 12, "minimum_payment": 637.5}], "2026-10-14T11:00")[0]
        self.assertEqual([(o["amount"], o["alt_amount"], o["date"]) for o in pr["offers"]],
                         [(2125.1, 637.5, "2026-10-16"), (637.5, None, "2026-10-23")])
        pr = O.products_for_case(self.p, [{"product_id": "p1", "type": "personal_loan", "past_due": 540.0, "days_late": 5,
                                            "instalment_amount": 180.0}], "2026-10-16T11:00")[0]      # said on the Friday
        self.assertEqual(pr["offers"][0]["date"], "2026-10-23")

    def test_resolve_offer_date_parts(self):
        t, dl = D(2026, 10, 14), D(2026, 10, 23)
        f = lambda e: O.resolve_offer_date(e, t, dl, self.cal)
        self.assertEqual(f("end_of_week_business_day; if today is that day, end_of_next_week_business_day"), D(2026, 10, 16))
        self.assertEqual(f("deadline (latest date)"), dl)
        self.assertEqual(f("next business day, or the deadline"), D(2026, 10, 15))
        self.assertEqual(f("+12_business_days"), dl)                      # capped at the deadline
        self.assertIsNone(f("whatever the customer says"))

    def test_holiday_replacement_in_facts(self):
        facts = O.date_facts_text(self.p, D(2026, 10, 14), "11:00", self.cal)
        self.assertIn("Mon 26 Oct (holiday) -> Tuesday 27 October 2026 (2026-10-27)", facts)
        self.assertIn("Sat 17 Oct -> Monday 19 October 2026 (2026-10-19)", facts)
        pr = O.products_for_case(self.p, [{"product_id": "p1", "type": "personal_loan", "past_due": 540.0, "days_late": 5,
                                            "instalment_amount": 180.0}], "2026-10-14T11:00")[0]
        self.assertEqual(pr["deadline"], "2026-11-02")
        r = O.check_promise(pr, 540, "Monday the 26th", D(2026, 10, 14), self.cal)
        self.assertEqual((r["code"], r["suggest_date"]), ("not_business_day", "2026-10-27"))


class S3Closing(unittest.TestCase):
    """Extractor output for fixtures/S3 (another lender's closing table: mixed-promise outcome, note severity order,
    notes allowed before some transfers)."""

    def setUp(self):
        self.p = load("s3-live")

    def c(self, **f):
        r = O.compute_closing(f, self.p)
        return (r["outcome"], r["reason"])

    def test_derived_closing(self):
        cl = self.p["closing"]
        self.assertEqual(cl["promise"]["outcome"], "promise")
        self.assertEqual(cl["promise_mixed"]["outcome"], "partial_promise")
        self.assertEqual(cl["promise_mixed_except"], ["already_paid"])
        self.assertEqual(cl["note_severity"], ["disputed", "hardship", "technical", "declined"])
        self.assertEqual(cl["note_map"]["hardship"], {"outcome": "no_promise", "reason": "hardship"})
        self.assertEqual(cl["note_map"]["already_paid"], {"outcome": "paid", "reason": ""})
        self.assertTrue(self.p["notes"]["enabled"])

    def test_closing_rows(self):
        ps = ["p1", "p2", "p3"]
        self.assertEqual(self.c(verified=True, promises=ps, products=ps), ("promise", ""))
        self.assertEqual(self.c(verified=True, promises=["p1"], notes=dict(p2="declined"), products=["p1", "p2"]), ("partial_promise", ""))
        self.assertEqual(self.c(verified=True, promises=["p1"], notes=dict(p2="already_paid"), products=["p1", "p2"]), ("promise", ""))
        self.assertEqual(self.c(verified=True, notes=dict(p1="already_paid", p2="already_paid"), products=["p1", "p2"]), ("paid", ""))
        self.assertEqual(self.c(verified=True, notes=dict(p1="declined", p2="disputed", p3="already_paid"), products=ps),
                         ("no_promise", "disputed"))                      # most serious, not get_account order
        self.assertEqual(self.c(verified=True, notes=dict(p1="declined", p2="technical"), products=["p1", "p2"]), ("no_promise", "technical"))
        self.assertEqual(self.c(verified=True, promises=["p1"], transfers=["payment_plans"], products=ps), ("transferred", "payment_plan"))
        self.assertEqual(self.c(verified=True, transfers=["hardship_team"], products=ps), ("transferred", "hardship"))


class OtherBank(unittest.TestCase):
    """fixture-x: weekend Friday+Saturday, holidays Wed 3 and Mon 8 March 2027, previous-day roll."""

    def setUp(self):
        self.p = load("fixture-x")
        self.cal = O.make_calendar(self.p)
        self.today = D(2027, 3, 1)  # Monday

    def test_calendar(self):
        self.assertFalse(O.is_business_day(D(2027, 3, 5), self.cal))   # Friday = weekend here
        self.assertTrue(O.is_business_day(D(2027, 3, 7), self.cal))    # Sunday = business day here
        self.assertFalse(O.is_business_day(D(2027, 3, 3), self.cal))   # holiday

    def test_deadlines(self):
        # band A: 5th business day, today counts: 1, 2, 4, 7, 9 March
        self.assertEqual(O.deadline(self.p, 4, "01.03.2027 10:00"), "2027-03-09")
        # band B: 3rd business day after today: 2, 4, 7 March
        self.assertEqual(O.deadline(self.p, 30, "01.03.2027 10:00"), "2027-03-07")
        # band C: 11 calendar days -> Fri 12 March (weekend) -> previous business day Thu 11
        self.assertEqual(O.deadline(self.p, 100, "01.03.2027 10:00"), "2027-03-11")
        self.assertEqual(O.deadline(self.p, 121, "01.03.2027 10:00"), "")

    def test_products_and_offers(self):
        prods = O.products_for_case(self.p, [{"account_ref": "A-1", "kind": "overdraft", "iban_tail": "77", "arrears": 1000,
                                              "overdue_days": 30, "min_due": 100}], "2027-03-01T10:00")
        pr = prods[0]
        self.assertEqual((pr["id"], pr["name"], pr["full"], pr["floor"], pr["band"]), ("A-1", "overdraft on account 77", 1000.0, 100.0, "B"))
        # 50% of the full amount on the deadline, the floor on the deadline, the floor on the next business day
        self.assertEqual([(o["amount"], o["date"]) for o in pr["offers"]],
                         [(500.0, "2027-03-07"), (100.0, "2027-03-07"), (100.0, "2027-03-02")])

    def test_roll_previous(self):
        pr = O.products_for_case(self.p, [{"account_ref": "A-1", "kind": "overdraft", "arrears": 1000, "overdue_days": 4,
                                           "min_due": 100}], "2027-03-01T10:00")[0]
        r = O.check_promise(pr, 200, "2027-03-05", self.today, self.cal)   # Friday -> previous business day Thu 4
        self.assertEqual((r["code"], r["suggest_date"]), ("not_business_day", "2027-03-04"))

    def test_callback(self):
        f = lambda d, t: O.check_callback(d, t, self.today, "16:30", self.p, self.cal)
        self.assertTrue(f("2027-03-02", "16:59")["ok"])
        self.assertEqual(f("2027-03-02", "17:00")["code"], "too_late")   # end excluded
        self.assertEqual(f("2027-03-01", "16:45")["code"], "day_not_allowed")

    def test_weekday_rule(self):
        self.assertEqual(O.parse_spoken_date("next Thursday", self.today, "next_occurrence"), D(2027, 3, 4))

    def test_closing(self):
        c = lambda **f: O.compute_closing(f, self.p)
        r = c(verified=True, notes={"A-1": "settled_claim", "A-2": "declined"}, products=["A-1", "A-2"])
        self.assertEqual((r["outcome"], r["reason"]), ("no_commitment", "declined"))
        r = c(verified=True, notes={"A-1": "settled_claim"}, products=["A-1"])
        self.assertEqual((r["outcome"], r["reason"]), ("settled", ""))
        r = c(verified=True, transfers=["workout_team"], promises=["A-1"], products=["A-1"])
        self.assertEqual((r["outcome"], r["reason"]), ("handed_over", "plan"))
        r = c(verified=False, id_failed=True)
        self.assertEqual((r["outcome"], r["reason"]), ("unverified", "too_many_wrong"))


class Schema(unittest.TestCase):
    def test_fixtures_valid(self):
        for f in ("fixture-a", "fixture-x", "brief-a", "brief-b", "brief-a-live", "s3-live"):
            p = json.loads((FIX / (f + ".policy.json")).read_text())
            self.assertEqual(S.validate(p), [], f)

    def test_errors(self):
        p = json.loads((FIX / "fixture-a.policy.json").read_text())
        p["calendar"]["holidays"].append("26.10.2026")
        p["bands"][1]["min_days_late"] = 5
        p["triggers"][0]["queue"] = "nowhere"
        p["identity"]["questions"][0]["tool_field"] = "shoe_size"
        del p["rules"]
        errs = S.validate(p)
        self.assertTrue(any("holidays" in e for e in errs))
        self.assertTrue(any("overlaps" in e for e in errs))
        self.assertTrue(any("nowhere" in e for e in errs))
        self.assertTrue(any("shoe_size" in e for e in errs))
        self.assertTrue(any("rules" in e for e in errs))
        self.assertEqual(S.validate("x"), ["policy is not a JSON object"])

    def test_extra_keys_warn_only(self):
        p = json.loads((FIX / "fixture-a.policy.json").read_text())
        p["something_new"] = {"a": 1}
        errs, warns = S.check(p)
        self.assertEqual(errs, [])
        self.assertTrue(any("something_new" in w for w in warns))

    def test_normalise_tolerates_junk(self):
        n = S.normalise({"bands": [None, {"deadline": "x"}], "triggers": "x", "outcomes": {"table": [{"outcome": "a", "reason": "b / c"}]}})
        self.assertEqual([r["reason"] for r in n["outcomes"]["table"]], ["b", "c"])
        self.assertEqual(n["calendar"]["weekend"], [6, 7])


if __name__ == "__main__":
    unittest.main()


class NewFields(unittest.TestCase):
    """Fields added by the extractor: floor_expr, tool_format, max_wrong_scope, callback.per_weekday, conditions."""

    def test_expr_value_matches_extractor(self):
        from builder.extract import eval_amount_expr
        fields = {"past_due": 30.0, "minimum": 12, "x": "7.5"}
        for e in ("min(50, past_due)", "past_due * 0.1", "max(10%, minimum)", "(past_due - 5) / 2", "x + 1",
                  "min(50,", "foo(1)", "unknown_field", "", "past_due / 0", "-past_due + 100"):
            self.assertEqual(O.expr_value(e, fields), eval_amount_expr(e, fields), e)

    def test_floor_expr_in_build_product(self):
        p = load("S4")
        prods = O.products_for_case(p, [{"product_id": "a", "type": "overdraft", "past_due": 30.0, "days_late": 20},
                                        {"product_id": "b", "type": "overdraft", "past_due": 400.0, "days_late": 20},
                                        {"product_id": "c", "type": "loan", "past_due": 400.0, "days_late": 20,
                                         "instalment_amount": 120.0}], "2026-10-21T14:05")
        self.assertEqual([x["floor"] for x in prods], [30.0, 50.0, 120.0])
        self.assertIn("min(50", prods[1]["floor_field"])

    def test_identity_formats(self):
        p = load("S2")
        dob, pc = p["identity"]["questions"]
        self.assertEqual((dob["normalise"], dob["date_format"]), ("dmy_to_iso", "DD-MM-YYYY"))
        self.assertEqual(O.format_answer(O.normalise_answer("5/3/1990", dob["normalise"]), dob), "05-03-1990")
        self.assertEqual(O.format_answer(O.normalise_answer(" sw1a  1aa", pc["normalise"]), pc), "SW1A 1AA")
        self.assertEqual(O.format_answer("1990-03-05", {"date_format": "MM/DD/YYYY"}), "03/05/1990")
        self.assertEqual(O.format_answer("1990-03-05", {}), "1990-03-05")
        self.assertEqual(S.date_pattern("YYYY-MM-DD"), "")
        self.assertEqual(S.norm_kind("read numbers as month, day, year", "date"), "mdy_to_iso")
        self.assertEqual(S.text_ops("no spaces, lower case"), ["lower", "nospace"])

    def test_max_wrong_scope(self):
        self.assertEqual(load("S3")["identity"]["max_wrong_scope"], "per_question")
        self.assertEqual(load("S4")["identity"]["max_wrong_scope"], "total")
        self.assertEqual(S.normalise({})["identity"]["max_wrong_scope"], "total")

    def test_callback_per_weekday(self):
        p = load("S3")
        cal = O.make_calendar(p)
        thu, fri = D(2026, 10, 22), D(2026, 10, 23)
        wins = O.callback_windows(thu, "13:30", p, cal)
        self.assertEqual([(w["date"], w["end"]) for w in wins], [("2026-10-22", "20:00"), ("2026-10-23", "16:00")])
        self.assertTrue(O.check_callback("2026-10-23", "16:00", thu, "13:30", p, cal)["ok"])
        self.assertEqual(O.check_callback("2026-10-23", "16:30", thu, "13:30", p, cal)["code"], "too_late")
        self.assertTrue(O.check_callback("2026-10-22", "19:45", thu, "13:30", p, cal)["ok"])
        # an entry without hours closes that weekday
        p2 = S.normalise(dict(json.loads((FIX / "S3.policy.json").read_text()),
                              callback=dict(p["callback"], per_weekday={"fri": {}})))
        self.assertEqual(p2["callback"]["per_weekday"], {"5": {}})
        self.assertEqual([w["date"] for w in O.callback_windows(thu, "13:30", p2, cal)], ["2026-10-22"])
        self.assertEqual(O.callback_windows(fri, "10:00", p, cal)[0]["end"], "16:00")

    def test_trigger_condition(self):
        t = [x for x in load("S4")["triggers"] if x["condition"]][0]
        c = t["condition"]
        self.assertEqual((c["over"], c["field"], c["op"], c["value"]), ("all_products_sum", "past_due", ">", 3000.0))
        self.assertFalse(O.condition_holds(c, [{"past_due": 1000}, {"past_due": 2000}]))
        self.assertTrue(O.condition_holds(c, [{"past_due": 1000}, {"past_due": 2000.5}]))
        self.assertTrue(O.condition_holds(dict(c, over="any_product", value=1500), [{"past_due": 1000}, {"past_due": 2000}]))
        self.assertFalse(O.condition_holds(dict(c, over="every_product", value=1500), [{"past_due": 1000}, {"past_due": 2000}]))
        self.assertTrue(O.condition_holds(dict(c, over="product_count", op=">=", value=2), [{}, {}]))
        self.assertFalse(O.condition_holds(None, [{"past_due": 5000}]))
        bad = S.normalise({"triggers": [{"name": "t", "condition": {"field": "x", "op": "about", "value": "n"}}]})
        self.assertIsNone(bad["triggers"][0]["condition"]["value"])
