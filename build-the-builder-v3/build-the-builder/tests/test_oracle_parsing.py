"""Offline tests of the deterministic parsers in builder/oracle.py: amounts (words, separators, corrections), spoken
and relative dates, corrections in dates/times/digits.

    cd build-the-builder && python3 -m unittest tests.test_oracle_parsing -v

No brief values: every date is relative to a fixed `today` chosen here, the calendar is made up.
"""
import datetime
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import oracle as O  # noqa: E402

D = datetime.date
TODAY = D(2026, 10, 14)                     # a Wednesday
CAL = O.make_calendar({"calendar": {"weekend": [6, 7], "holidays": ["2026-10-30"]}})

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def words(n, hyphen=True, and_=True):
    """Independent reference: integer 0..999999 -> English words."""
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + (("-" if hyphen else " ") + ONES[n % 10] if n % 10 else "")
    if n < 1000:
        rest = n % 100
        return ONES[n // 100] + " hundred" + ((" and " if and_ else " ") + words(rest, hyphen, and_) if rest else "")
    rest = n % 1000
    return words(n // 1000, hyphen, and_) + " thousand" + ((" " if rest >= 100 or not and_ else " and ") + words(rest, hyphen, and_) if rest else "")


class Amounts(unittest.TestCase):
    def check(self, text, value):
        got = O.parse_amount(text)
        self.assertIsNotNone(got, text)
        self.assertAlmostEqual(got, value, places=2, msg=text)

    def test_numbers_and_separators(self):
        for text, v in [("420", 420), ("420.5", 420.5), ("420,50", 420.5), ("1,250.50", 1250.5), ("1.250,50", 1250.5),
                        ("1.250", 1250), ("1,250", 1250), ("12.500.000", 12500000), ("2'125.10", 2125.1),
                        ("EUR 2,125.10", 2125.1), ("€ 420", 420), ("420 euros", 420), ("$75", 75), ("0.5", 0.5),
                        (420, 420), (420.5, 420.5)]:
            self.check(text, v)

    def test_words(self):
        for text, v in [("four hundred and fifty", 450), ("four hundred fifty", 450), ("five hundred and twenty-five", 525),
                        ("twenty five", 25), ("a hundred", 100), ("a thousand", 1000), ("two grand", 2000),
                        ("three thousand and five", 3005), ("one thousand two hundred thirty four", 1234),
                        ("two hundred thousand", 200000), ("four fifty", 450), ("twelve fifty", 1250),
                        ("twenty five hundred", 2500), ("nineteen hundred", 1900), ("fifty euros", 50),
                        ("four hundred point five", 400.5), ("zero point twenty five", 0.25),
                        ("nine hundred ninety nine point nine nine", 999.99),
                        ("one thousand two hundred thirty four euros and fifty six cents", 1234.56),
                        ("three hundred euros and twenty cents", 300.2), ("Four Hundred Euros", 400)]:
            self.check(text, v)

    def test_mixed_and_pieces(self):
        for text, v in [("2 thousand 500", 2500), ("1.5k", 1500), ("2k", 2000), ("2 hundred", 200),
                        ("two thousand ... five hundred", 2500), ("two thousand, uh, five hundred", 2500),
                        ("1,250 euros and 50 cents", 1250.5), ("I can pay 300", 300), ("about 300 euros a month", 300)]:
            self.check(text, v)

    def test_corrections_take_the_last_stated_amount(self):
        for text, v in [("four hundred, sorry, I mean three hundred", 300), ("500 euros, no, 450", 450),
                        ("450, not 500", 450), ("not 500 but 450", 450), ("300, actually make it 350", 350),
                        ("two hundred... wait, two hundred and fifty", 250), ("400 instead of 500", 400),
                        ("five hundred four hundred", 400)]:
            self.check(text, v)

    def test_days_dates_and_others_are_not_amounts(self):
        self.check("three hundred euros on the 25th", 300)
        self.check("200 by 23 October", 200)
        self.check("200 by October 23", 200)
        self.check("300 in 2 weeks", 300)
        for text in ("the 25th", "10 percent", "10%", "", None, "next Friday", "no idea", "the twenty fifth"):
            self.assertIsNone(O.parse_amount(text), text)

    def test_every_amount_in_words_against_a_reference(self):
        bad = []
        for n in list(range(0, 2000)) + list(range(2000, 100000, 37)):
            for form in (words(n), words(n, hyphen=False), words(n, and_=False)):
                if O.parse_amount(form) != float(n):
                    bad.append((form, O.parse_amount(form)))
            cents = n % 100
            if n and cents:
                form = words(n) + " euros and " + words(cents) + " cents"
                if abs((O.parse_amount(form) or 0) - (n + cents / 100.0)) > 0.001:
                    bad.append((form, O.parse_amount(form)))
        self.assertEqual(bad[:10], [])

    def test_every_written_amount_in_both_conventions(self):
        bad = []
        for cents in range(1, 100000000, 98765):
            v = cents / 100.0
            us = "{:,.2f}".format(v)
            eu = us.replace(",", " ").replace(".", ",").replace(" ", ".")
            for form in (us, eu, "€" + us, eu + " EUR", "%.2f" % v):
                if abs((O.parse_amount(form) or -1) - v) > 0.001:
                    bad.append((form, O.parse_amount(form)))
        self.assertEqual(bad[:10], [])

    def test_never_raises(self):
        for x in (object(), [1], {"a": 1}, "....", "-", "point", "and and", "hundred hundred", "k", True):
            O.parse_amount(x)
        self.assertIsNone(O.parse_amount(True))


class Dates(unittest.TestCase):
    def test_relative_forms(self):
        for text, want in [("end of the month", D(2026, 10, 31)), ("end of next month", D(2026, 11, 30)),
                           ("month-end", D(2026, 10, 31)), ("end of this week", D(2026, 10, 16)),
                           ("end of next week", D(2026, 10, 23)), ("this time next week", D(2026, 10, 21)),
                           ("the 5th of next month", D(2026, 11, 5)), ("next month on the 3rd", D(2026, 11, 3)),
                           ("the fifth of this month", D(2026, 10, 5))]:
            self.assertEqual(O.relative_date(text, TODAY), want, text)
            self.assertEqual(O.said_date(text, TODAY), want, text)
        # with a calendar: the last business day (31 Oct is a Saturday, 30 Oct a holiday)
        self.assertEqual(O.said_date("end of the month", TODAY, cal=CAL), D(2026, 10, 29))
        self.assertEqual(O.said_date("end of the month", D(2026, 12, 2), cal=CAL), D(2026, 12, 31))
        self.assertEqual(O.said_date("end of next month", D(2026, 12, 2), cal=CAL), D(2027, 1, 29))
        self.assertEqual(O.said_date("end of next week", TODAY, cal=CAL), D(2026, 10, 23))
        self.assertEqual(O.said_date("end of this week", D(2026, 10, 27), cal=CAL), D(2026, 10, 29))

    def test_spoken_relative_forms_in_parse_spoken_date(self):
        for text, want in [("a week from today", D(2026, 10, 21)), ("two weeks from now", D(2026, 10, 28)),
                           ("ten days from now", D(2026, 10, 24)), ("a week today", D(2026, 10, 21)),
                           ("in a fortnight", D(2026, 10, 28)), ("a fortnight from today", D(2026, 10, 28)),
                           ("Monday next week", D(2026, 10, 19)), ("next week Friday", D(2026, 10, 23)),
                           ("Friday next week", D(2026, 10, 23)), ("the day after tomorrow", D(2026, 10, 16)),
                           ("tomorrow", D(2026, 10, 15)), ("today", TODAY), ("in 3 days", D(2026, 10, 17)),
                           ("in about five weeks", D(2026, 11, 18)), ("the twenty-second of October", D(2026, 10, 22)),
                           ("October twenty second", D(2026, 10, 22)), ("the 3rd", D(2026, 11, 3)),
                           ("twelfth of January", D(2027, 1, 12))]:
            self.assertEqual(O.parse_spoken_date(text, TODAY), want, text)
            self.assertEqual(O.said_date(text, TODAY), want, text)

    def test_vague_days_stay_unread(self):
        for text in ("next week", "this weekend", "sometime next month", "soon", "", None, "end of the year"):
            self.assertIsNone(O.said_date(text, TODAY), text)

    def test_corrections_take_the_last_stated_day(self):
        for text, want in [("the twenty first, sorry, I mean the twentieth", D(2026, 10, 20)),
                           ("Friday, no, Monday", D(2026, 10, 19)), ("the 20th, not the 21st", D(2026, 10, 20)),
                           ("not Friday but Monday", D(2026, 10, 19)), ("end of the month, sorry, end of next week", D(2026, 10, 23)),
                           ("22 October... actually 23 October", D(2026, 10, 23)), ("tomorrow, wait, no, today", TODAY)]:
            self.assertEqual(O.said_date(text, TODAY), want, text)

    def test_said_date_for_identity_answers(self):
        self.assertEqual(O.normalise_answer("14 March 1988", "dmy_to_iso"), "1988-03-14")
        self.assertEqual(O.normalise_answer("the 14th, sorry, the 15th of March 1988", "dmy_to_iso"), "1988-03-15")
        self.assertEqual(O.normalise_answer("1988-03-14", "", "date"), "1988-03-14")

    def test_every_day_of_the_next_month_in_words(self):
        bad = []
        for k in range(1, 29):
            ords = "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split()
            w = ords[k - 1] if k <= 20 else "twenty-" + ords[k - 21]
            for form in ("the %s of next month" % w, "next month on the %d" % k, "the %dth of next month" % k):
                if O.said_date(form, TODAY) != D(2026, 11, k):
                    bad.append((form, O.said_date(form, TODAY)))
        self.assertEqual(bad, [])

    def test_never_raises(self):
        for x in (object(), 5, "end of", "of next month", "the 99th of next month", "end of next"):
            O.said_date(x, TODAY)
            O.relative_date(x, TODAY)
            O.parse_spoken_date(x, TODAY)
        self.assertIsNone(O.said_date("the 31st of next month", TODAY))       # 31 November does not exist


class TimesAndDigits(unittest.TestCase):
    def test_time_corrections(self):
        self.assertEqual(O.parse_time("three, sorry, four pm"), "16:00")
        self.assertEqual(O.parse_time("at 2 pm, no, 3 pm"), "15:00")
        self.assertEqual(O.parse_time("half past ten"), "10:30")
        self.assertEqual(O.parse_time("in the afternoon at two"), "14:00")

    def test_spoken_digits(self):
        for text, want in [("four o seven one", "4071"), ("one hundred two three", "123"), ("four hundred and five", "405"),
                           ("four four, no, seven one", "4471"),
                           ("four nought seven one", "4071"), ("twelve hundred", "1200"), ("four hundred seventy one", "471"),
                           ("forty-four seventy-one", "4471"), ("double oh seven one", "0071"), ("4471", "4471"),
                           ("the last four digits are four four seven one", "4471")]:
            self.assertEqual("".join(c for c in O.spoken_digits(text) if c.isdigit()), want, text)


class ReviewRegressions(unittest.TestCase):
    """Review of T03: each item is a case the first version got wrong."""

    def test_1_euros_then_cents_without_the_word_cents(self):
        for text, v in [("twelve euros fifty", 12.5), ("12 euros 50", 12.5), ("one twenty six euros fifty", 126.5),
                        ("12 euros and 50", 12.5), ("1,250 euros and 50 cents", 1250.5), ("50 cents", 0.5)]:
            self.assertAlmostEqual(O.parse_amount(text), v, places=2, msg=text)
        self.assertEqual(O.parse_amount("300 euros 200 euros"), 200)          # two amounts, not euros + cents

    def test_2_point_without_a_whole_part_and_before_a_multiplier(self):
        self.assertEqual(O.parse_amount("point five"), 0.5)
        self.assertEqual(O.parse_amount("one point two five thousand"), 1250)
        self.assertEqual(O.parse_amount("two point five k"), 2500)
        self.assertEqual(O.parse_amount("1.5 thousand"), 1500)

    def test_3_space_as_thousands_separator(self):
        self.assertEqual(O.parse_amount("1 250"), 1250)
        self.assertEqual(O.parse_amount("1 250,00"), 1250)
        self.assertEqual(O.parse_amount("12 500.50 EUR"), 12500.5)

    def test_4_clock_time_or_number_after_at_or_by_is_not_the_amount(self):
        self.assertEqual(O.parse_amount("two hundred euros at 3"), 200)
        self.assertEqual(O.parse_amount("200 by 5"), 200)
        self.assertEqual(O.parse_amount("200 by the 25"), 200)
        self.assertEqual(O.parse_amount("300 on Friday at 3"), 300)

    def test_5_callback_times_never_worse_than_before(self):
        self.assertEqual(O.parse_time("not before 3 pm"), "15:00")
        self.assertEqual(O.parse_time("four pm, no, four thirty"), "16:30")
        self.assertEqual(O.parse_time("in the afternoon, sorry, at three"), "15:00")
        self.assertEqual(O.parse_time("half past three, sorry, four"), "03:30")     # old reading of the whole text
        self.assertEqual(O.parse_time("three, sorry, four pm"), "16:00")
        self.assertEqual(O.parse_time("noon, sorry, one pm"), "13:00")

    def test_6_partial_correction_is_merged_into_the_day_said_before(self):
        self.assertEqual(O.said_date("14 March 1988, no, 1989", None, future=False), D(1989, 3, 14))
        self.assertEqual(O.said_date("3rd of March 1988, sorry, the 4th", None, future=False), D(1988, 3, 4))
        self.assertEqual(O.said_date("14 March 1988, sorry, April", None, future=False), D(1988, 4, 14))
        self.assertEqual(O.normalise_answer("14 March 1988, no, 1989", "dmy_to_iso"), "1989-03-14")
        self.assertEqual(O.said_date("23 October, sorry, the 22nd", TODAY), D(2026, 10, 22))
        self.assertIsNone(O.said_date("Friday, no, I don't know", TODAY))
        self.assertIsNone(O.said_date("14 March 1988, sorry, the 31st of February", None, future=False))

    def test_7_end_of_the_week_said_on_a_weekend(self):
        sat, sun = D(2026, 10, 17), D(2026, 10, 18)
        for day in (sat, sun):
            self.assertEqual(O.said_date("end of the week", day), D(2026, 10, 23))
            self.assertEqual(O.said_date("end of the week", day, cal=CAL), D(2026, 10, 23))
            self.assertEqual(O.said_date("end of next week", day, cal=CAL), D(2026, 10, 23))   # the week starting after it
        self.assertEqual(O.said_date("end of the week", D(2026, 10, 16)), D(2026, 10, 16))     # Friday itself
        self.assertEqual(O.said_date("end of the month", D(2026, 10, 31), cal=CAL), D(2026, 11, 30))

    def test_9_shorter_correction_of_digits_keeps_the_old_reading(self):
        self.assertEqual("".join(c for c in O.spoken_digits("four four, no, seven one") if c.isdigit()), "4471")
        self.assertEqual("".join(c for c in O.spoken_digits("one hundred two three") if c.isdigit()), "123")

    def test_10_bare_period_and_this_weekday_on_a_weekend(self):
        self.assertEqual(O.parse_spoken_date("two weeks", TODAY), D(2026, 10, 28))
        self.assertEqual(O.parse_spoken_date("a month", TODAY), D(2026, 11, 13))
        self.assertEqual(O.parse_spoken_date("this Friday", D(2026, 10, 18)), D(2026, 10, 23))   # said on a Sunday
        self.assertEqual(O.parse_spoken_date("this Friday", TODAY), D(2026, 10, 16))
        self.assertEqual(O.parse_spoken_date("this Monday", TODAY), D(2026, 10, 19))


class Periods(unittest.TestCase):
    """chain250 AD-16 / EDGE-05: a period is vague (ask once, then the deadline); a weekday said next is inside it."""

    def test_said_period(self):
        for text, want in [("at the end of next week", (D(2026, 10, 19), D(2026, 10, 25), True)),
                           ("next week", (D(2026, 10, 19), D(2026, 10, 25), False)),
                           ("at the end of the month, when my salary comes", (TODAY, D(2026, 10, 31), True)),
                           ("month-end", (TODAY, D(2026, 10, 31), True)), ("end of the week", (TODAY, D(2026, 10, 18), True)),
                           ("early next month", (D(2026, 11, 1), D(2026, 11, 30), False)),
                           ("end of next month", (D(2026, 11, 1), D(2026, 11, 30), True)),
                           ("end of the month, sorry, end of next week", (D(2026, 10, 19), D(2026, 10, 25), True))]:
            self.assertEqual(O.said_period(text, TODAY, CAL), want, text)
        for text in ("a week from today", "in two weeks", "this weekend", "soon", "when my salary comes", "", None, "a month"):
            self.assertIsNone(O.said_period(text, TODAY, CAL), text)
        # said on a Saturday: this week has no business day left
        self.assertEqual(O.said_period("end of the week", D(2026, 10, 17), CAL), (D(2026, 10, 19), D(2026, 10, 25), True))
        self.assertEqual(O.said_period("end of the month", D(2026, 12, 31), CAL)[1], D(2026, 12, 31))
        self.assertEqual(O.said_period("next month", D(2026, 12, 2), CAL)[:2], (D(2027, 1, 1), D(2027, 1, 31)))

    def test_a_period_names_no_day(self):
        import re
        for text, named in [("end of next week", False), ("end of the month", False), ("Friday next week", True),
                            ("this time next week", True), ("the 5th of next month", True), ("the fifth of next month", True)]:
            self.assertEqual(bool(re.search(O.DAY_NAMED_RE, O.day_words_to_digits(text.lower()))), named, text)

    def test_period_day(self):
        self.assertEqual(O.period_day("Thursday", "end of next week", TODAY, CAL), D(2026, 10, 22))   # AD-16
        self.assertEqual(O.period_day("thursday.", "at the end of next week", TODAY, CAL), D(2026, 10, 22))
        self.assertEqual(O.period_day("Thursday at the end of next week", "Thursday at the end of next week", TODAY, CAL), D(2026, 10, 22))
        self.assertEqual(O.period_day("Friday", "end of the month", TODAY, CAL), D(2026, 10, 30))       # the last Friday
        self.assertEqual(O.period_day("Friday", "early next month", TODAY, CAL), D(2026, 11, 6))       # the first Friday
        self.assertEqual(O.period_day("Monday", "end of the week", TODAY, CAL), None)                  # not in it any more
        for text, period in [("next Thursday", "end of next week"), ("this Thursday", "next week"), ("Thursday", ""),
                             ("Thursday", "soon"), ("Thursday the 15th", "next week"), ("the 27th", "end of the month"), (None, None)]:
            self.assertIsNone(O.period_day(text, period, TODAY, CAL), (text, period))


if __name__ == "__main__":
    unittest.main()
