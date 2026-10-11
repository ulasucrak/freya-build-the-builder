"""FX7: oracle fixes. A bare weekday said on that weekday is next week's (N007); products after the first open with
their Attempt 1 offer (N186/N205/N242-244); a word shared by product names is listed as ambiguous (N246).

    cd build-the-builder && python3 -m unittest tests.test_fx7_oracle -v
"""
import datetime
import pathlib
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "tests"))

from builder import generate as G  # noqa: E402
from builder import oracle as O  # noqa: E402
from builder import testmode as T  # noqa: E402
import test_generate as TG  # noqa: E402

D = datetime.date


class BareWeekday(unittest.TestCase):
    def test_resolve_weekday_same_day_is_next_week(self):
        for k in range(7):
            today = D(2026, 10, 12) + datetime.timedelta(days=k)          # Monday .. Sunday
            self.assertEqual(O.resolve_weekday(today.weekday(), today, "", ""), today + datetime.timedelta(days=7))
            other = (today.weekday() + 1) % 7
            self.assertEqual(O.resolve_weekday(other, today, "", ""), today + datetime.timedelta(days=1))
            # 'this <weekday>' on that weekday keeps today; 'next' is unchanged
            self.assertEqual(O.resolve_weekday(today.weekday(), today, "this", ""), today)
            self.assertEqual(O.resolve_weekday(today.weekday(), today, "next", "next_occurrence"),
                             today + datetime.timedelta(days=7))

    def test_parse_spoken_date_bare_weekday(self):
        tue = D(2026, 10, 13)
        self.assertEqual(O.parse_spoken_date("Tuesday", tue), D(2026, 10, 20))
        self.assertEqual(O.parse_spoken_date("on tuesday", tue), D(2026, 10, 20))
        self.assertEqual(O.parse_spoken_date("Wednesday", tue), D(2026, 10, 14))
        self.assertEqual(O.parse_spoken_date("today", tue), tue)
        self.assertEqual(O.parse_spoken_date("this Tuesday", tue), tue)
        self.assertEqual(O.parse_spoken_date("Tuesday the 13th", tue), tue)       # a day number still wins


def prod(i, pid, name, offers=True):
    return dict(index=i, id=pid, name=name, full=300.0, floor=90.0, days_late=5,
                offers=[dict(n=1, amount=150.0), dict(n=2, amount=90.0)] if offers else [])


class FactsLines(unittest.TestCase):
    FIRST = "First question for it: its Attempt 1 offer, never 'can you pay today'."

    def test_single_product_has_no_extra_lines(self):
        facts = O.account_facts_text([prod(1, "p1", "personal loan")], None)
        self.assertNotIn(self.FIRST, facts)
        self.assertNotIn("Ambiguous names", facts)

    def test_products_after_the_first_open_with_attempt_1(self):
        for ladder in (False, True):
            facts = O.account_facts_text([prod(1, "p1", "credit card"), prod(2, "p2", "personal loan"),
                                          prod(3, "p3", "overdraft", offers=False)], None, None, ladder)
            self.assertEqual(facts.count(self.FIRST), 1, facts)
            blocks = facts.split("\n2. ")
            self.assertNotIn(self.FIRST, blocks[0])
            self.assertIn(self.FIRST, blocks[1].split("\n3. ")[0])
            self.assertNotIn("Ambiguous names", facts)

    def test_shared_name_words_are_ambiguous(self):
        facts = O.account_facts_text([prod(1, "p1", "Personal Loan"), prod(2, "p2", "Car Loan"),
                                      prod(3, "p3", "credit card")], None)
        self.assertIn("Ambiguous names ('loan': Personal Loan or Car Loan): if the customer uses only that word, ask which one.",
                      facts)
        facts = O.account_facts_text([prod(1, "p1", "card ending 1234"), prod(2, "p2", "store card"),
                                      prod(3, "p3", "loan for the car")], None)
        self.assertIn("'card': card ending 1234 or store card", facts)
        self.assertNotIn("'for'", facts)
        self.assertNotIn("'the'", facts)
        self.assertEqual(facts.count("Ambiguous names"), 1)


class GeneratedPrepareAccount(unittest.TestCase):
    """The prepare_account code node (oracle embedded) writes the new lines into w_account_facts."""

    def test_generated_facts(self):
        wf = G.generate_workflow(TG.raw("brief-b"))
        vs = {"now": "2026-10-14T11:00"}
        T.run_code_locally(TG.code_of(wf["nodes"]["setup"]), {}, vs)
        vs["w_account_products"] = [
            {"product_id": "p1", "type": "credit_card", "card_last4": "4821", "past_due": 420.0, "days_late": 6,
             "minimum_payment": 126.0},
            {"product_id": "p2", "type": "credit_card", "card_last4": "9911", "past_due": 300.0, "days_late": 6,
             "minimum_payment": 90.0}]
        T.run_code_locally(TG.code_of(wf["nodes"]["prepare_account"]), {}, vs)
        facts = vs.get("w_account_facts") or ""
        self.assertIn("2. product_id p2", facts)
        self.assertEqual(facts.count(FactsLines.FIRST), 1, facts)
        self.assertIn(FactsLines.FIRST, facts.split("2. product_id p2")[1])
        self.assertIn("Ambiguous names (", facts)


if __name__ == "__main__":
    unittest.main()
