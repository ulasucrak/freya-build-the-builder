"""Offline tests of builder/codegen/generic_post.py, the post-hook of every api tool of the generic workflow.

The hook is built with generate.build_code (the way the generator embeds it) and executed locally, both through
testmode.run_code_locally (the sandbox wrapper) and with separate globals/locals (a strict sandbox).

    cd build-the-builder && python3 -m unittest tests.test_generic_post -v
"""
import ast
import copy
import json
import pathlib
import sys
import time
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import generate as G  # noqa: E402
from builder import testmode as T  # noqa: E402

SRC = (G.CODEGEN / "generic_post.py").read_text()
TOKEN = "CALL_LANGUAGE_HERE"
MSG = "Kayit bulunamadi, lutfen tekrar deneyin"   # a service message text that must never reach the model


def build(lang=None, rt=None):
    src = SRC.replace(TOKEN, lang) if lang else SRC
    return G.build_code("generic_post", rt or {"names": {}}, "post", source=src)


CODE = {"tr": build("Turkish"), "en": build("English"), "raw": build()}


def run(res, lang="tr", vs=None, code=None):
    """Run the hook on `res` through run_code_locally; returns the (changed in place) result."""
    st, _out = T.run_code_locally("result = params['result']\n" + (code or CODE[lang]), {"result": res},
                                  vs if vs is not None else {})
    assert st == "success", _out
    return res


def run_strict(src, vs, res):
    exec(compile(src, "snippet", "exec"), {"vars": vs, "result": res}, {})
    return res


def ok_env(data, code="1"):
    return {"status": "success", "status_code": 200, "header": {"Content-Type": "application/json"},
            "message": "Successfully executed POST request",
            "data": {"data": copy.deepcopy(data),
                     "result": {"guid": "g1", "returnCode": code, "messageText": "Success", "code": code,
                                "message": "Success", "type": None,
                                "details": {"guid": "g1", "rootCause": None, "errorOrigin": None}}}}


def err_env(http, code, typ, text=MSG):
    return {"status": "error", "status_code": http, "message": "HTTP " + str(http) + " " + text,
            "data": {"result": {"guid": "g2", "returnCode": code, "messageText": text, "code": code, "message": text,
                                "type": typ, "details": {"guid": "g2", "rootCause": "NotFound", "errorOrigin": "gw"}}}}


DEBT = {
    "cardNumberLast4": "1234",
    "productName": "Any Card",
    "statementDate": "05.10.2026",
    "lastPayDate": "15.10.2026",
    "debtTL": {"statementPaymentStatus": "HAS_DEBT", "transferAmount": 0.0, "statementDebt": 5000.13,
               "minPayment": 2000.05, "remainingDebt": 2500.0, "totalDebt": 1234567.89},
    "debtUSD": {"statementPaymentStatus": "HAS_DEBT", "statementDebt": 1234.5, "minPayment": 0.5, "totalDebt": -12.0},
    "debtEUR": {"statementPaymentStatus": "NO_DEBT", "statementDebt": 0.0, "minPayment": None},
    "statementPaymentStatus": "HAS_DEBT",
    "automaticPaymentFlag": "N",
}


class HookShape(unittest.TestCase):
    def test_size_and_sandbox_rules(self):
        for k, code in CODE.items():
            self.assertLess(len(code), 23000, k)
            compile(code, k, "exec")
            self.assertNotIn("{{", code)
            self.assertNotIn("}}", code)
            names = set()
            for n in ast.walk(ast.parse(code)):
                for x in (getattr(n, "id", None), getattr(n, "attr", None), getattr(n, "arg", None),
                          n.name if isinstance(n, ast.FunctionDef) else None):
                    if isinstance(x, str):
                        names.add(x)
            self.assertEqual([x for x in names if x.startswith("_")], [], k)
        self.assertIn("LANG_PLACEHOLDER = \"" + TOKEN + "\"", SRC)   # the documented agreement with the generator
        self.assertNotIn(TOKEN, CODE["tr"])

    def test_no_case_specific_names(self):
        low = SRC.lower()
        for word in ("customerno", "cardnumber", "cards.list", "fb_ivr", "fbe000", "debttl", "statement"):
            self.assertNotIn(word, low)


class Envelope(unittest.TestCase):
    def test_success_unwraps_data(self):
        res = run(ok_env({"cards": [{"cardNumberLast4": "4253", "productName": "P"}], "validCardsCount": 1,
                          "exitState": "1_CARD"}))
        d = res["data"]
        self.assertTrue(d["ok"])
        self.assertEqual(d["validCardsCount"], 1)
        self.assertEqual(d["cards"][0]["cardNumberLast4"], "4253")
        self.assertNotIn("result", d)
        self.assertNotIn("header", res)
        self.assertNotIn("what_to_do_next", d)          # nothing to speak, nothing to say
        self.assertNotIn("_spoken", json.dumps(d))     # counts, last digits: no spoken forms

    def test_success_codes(self):
        for code in ("1", 1, "0", 0, "00", "OK", "success"):
            self.assertTrue(run(ok_env({"a": 1}, code=code))["data"]["ok"], code)
        res = run(ok_env({"a": 1}, code="E42"))      # HTTP 200 but an error code
        self.assertFalse(res["data"]["ok"])
        self.assertEqual(res["data"]["code"], "E42")
        self.assertNotIn("a", res["data"])

    def test_404_warning(self):
        res = run(err_env(404, "SVC_404_CODE", "WARNING"))
        d = res["data"]
        self.assertEqual((d["ok"], d["code"], d["type"], d["http_status"]), (False, "SVC_404_CODE", "WARNING", 404))
        self.assertEqual(d["what_to_do_next"], "The service answered SVC_404_CODE/WARNING (HTTP 404). Follow the rule "
                                               "for this situation; never read the service's message text aloud.")
        self.assertNotIn(MSG, json.dumps(res))         # neither in data nor in the envelope's message
        self.assertNotIn("guid", json.dumps(res))

    def test_500_error(self):
        res = run(err_env(500, "TECH01", "ERROR", "Teknik hata: ABORTED: "))
        self.assertIn("TECH01/ERROR (HTTP 500)", res["data"]["what_to_do_next"])
        self.assertNotIn("ABORTED", json.dumps(res))

    def test_error_without_body_or_code(self):
        res = run({"status": "error", "status_code": 401, "data": None, "message": "Unauthorized token expired"})
        self.assertEqual(res["data"]["what_to_do_next"][:41], "The service answered an error (HTTP 401).")
        res = run({"status": "error", "data": None})
        self.assertFalse(res["data"]["ok"])
        self.assertTrue(res["data"]["what_to_do_next"].startswith("The service did not answer."))

    def test_error_body_in_message_text(self):
        body = json.dumps({"result": {"returnCode": "NF1", "messageText": MSG, "type": "WARNING"}})
        res = run({"status": "error", "status_code": 404, "data": None, "message": "HTTP error: " + body})
        self.assertEqual(res["data"]["code"], "NF1")
        self.assertNotIn(MSG, json.dumps(res))

    def test_body_as_json_string(self):
        res = run({"status": "success", "status_code": 200, "data": json.dumps(ok_env({"dueDate": "2026-11-05"})["data"])})
        self.assertEqual(res["data"]["dueDate_spoken"], "5 Kasım 2026 Perşembe")

    def test_flat_bodies(self):
        res = run({"status": "success", "status_code": 200, "data": {"ok": True, "due_date": "2026-11-05", "amount_due": 120.5}})
        self.assertEqual(res["data"]["amount_due_spoken"], "yüz yirmi virgül beş")   # amount, currency unknown
        res = run({"status": "success", "status_code": 200, "data": {"ok": False, "error": "NOT_FOUND"}})
        self.assertEqual((res["data"]["ok"], res["data"]["code"]), (False, "NOT_FOUND"))
        res = run({"status": "success", "status_code": 200, "data": {"ok": False, "error": MSG}})
        self.assertFalse(res["data"]["ok"])
        self.assertNotIn(MSG, json.dumps(res))
        res = run({"status": "success", "status_code": 200, "data": {"result": {"name": "x", "total": 5}}})
        self.assertTrue(res["data"]["ok"])              # a 'result' object without status fields is data
        self.assertEqual(res["data"]["result"]["total_spoken"], "beş")

    def test_list_data(self):
        res = run(ok_env([{"date": "01.12.2026"}, {"date": "bad"}]))
        self.assertEqual(res["data"]["items"][0]["date_spoken"], "1 Aralık 2026 Salı")
        self.assertNotIn("date_spoken", res["data"]["items"][1])

    def test_garbage_never_raises(self):
        for junk in (None, "x", {}, {"data": None, "status": "error"}, {"data": "not json"}, {"data": [1, 2]},
                     {"data": {"data": "x", "result": "y"}, "status_code": "abc"}, {"data": {"result": {"returnCode": None}}}):
            st, out = T.run_code_locally("result = params['result']\n" + CODE["tr"], {"result": copy.deepcopy(junk)}, {})
            self.assertEqual(st, "success", (junk, out))
            run_strict(CODE["raw"], {}, copy.deepcopy(junk))

    def test_strict_sandbox(self):
        res = run_strict(CODE["tr"], {}, ok_env(DEBT))
        self.assertEqual(res["data"]["debtTL"]["statementDebt_spoken"], "beş bin lira on üç kuruş")


class Spoken(unittest.TestCase):
    def test_turkish_amounts_per_currency_block(self):
        d = run(ok_env(DEBT))["data"]
        tl, usd, eur = d["debtTL"], d["debtUSD"], d["debtEUR"]
        self.assertEqual(tl["statementDebt_spoken"], "beş bin lira on üç kuruş")
        self.assertEqual(tl["minPayment_spoken"], "iki bin lira beş kuruş")
        self.assertEqual(tl["remainingDebt_spoken"], "iki bin beş yüz lira")       # zero kuruş is not said
        self.assertEqual(tl["transferAmount_spoken"], "sıfır lira")
        self.assertEqual(tl["totalDebt_spoken"],
                         "bir milyon iki yüz otuz dört bin beş yüz altmış yedi lira seksen dokuz kuruş")
        self.assertEqual(usd["statementDebt_spoken"], "bin iki yüz otuz dört dolar elli sent")
        self.assertEqual(usd["minPayment_spoken"], "elli sent")
        self.assertEqual(usd["totalDebt_spoken"], "eksi on iki dolar")
        self.assertEqual(eur["statementDebt_spoken"], "sıfır avro")
        self.assertNotIn("minPayment_spoken", eur)                                  # null: nothing invented
        self.assertNotIn("debtGBP", d)                                              # missing block stays missing
        self.assertNotIn("debtGBP_spoken", d)
        self.assertNotIn("statementPaymentStatus_spoken", tl)
        self.assertNotIn("cardNumberLast4_spoken", d)
        self.assertIn("_spoken", d["what_to_do_next"])

    def test_turkish_dates(self):
        d = run(ok_env(dict(DEBT, now="2026-10-12T10:30:00+03:00", day="2026-10-12T00:00:00", iso="2026-10-12",
                            bad="31.02.2026", text="due 05.10.2026")))["data"]
        self.assertEqual(d["statementDate_spoken"], "5 Ekim 2026 Pazartesi")
        self.assertEqual(d["lastPayDate_spoken"], "15 Ekim 2026 Perşembe")
        self.assertEqual(d["iso_spoken"], "12 Ekim 2026 Pazartesi")
        self.assertEqual(d["now_spoken"], "12 Ekim 2026 Pazartesi, saat 10:30")
        self.assertEqual(d["day_spoken"], "12 Ekim 2026 Pazartesi")
        self.assertNotIn("bad_spoken", d)
        self.assertNotIn("text_spoken", d)

    def test_gbp_and_sibling_currency_and_strings(self):
        d = run(ok_env({"debtGBP": {"minPayment": 0.01, "totalDebt": 3.0},
                        "fee": {"value": "12.50", "currency": "EUR"},
                        "limitInfo": {"creditLimit": "1250,50 TL"}, "charge": "€3", "price": "12 XYZ"}))["data"]
        self.assertEqual(d["debtGBP"]["minPayment_spoken"], "bir peni")
        self.assertEqual(d["debtGBP"]["totalDebt_spoken"], "üç sterlin")
        self.assertEqual(d["fee"]["value_spoken"], "on iki avro elli sent")
        self.assertEqual(d["limitInfo"]["creditLimit_spoken"], "bin iki yüz elli lira elli kuruş")
        self.assertEqual(d["charge_spoken"], "üç avro")
        self.assertNotIn("price_spoken", d)       # letters that are no currency: not an amount

    def test_not_amounts(self):
        d = run(ok_env({"statementWeek": 3, "statementDay": 16, "validCardsCount": 2, "responseCode": 0,
                        "customerNo": 90000001, "phoneNumber": "5550000", "ratePercent": 3.5, "flag": True,
                        "debtUSD": {"lastPaymentDayCount": 4}}))["data"]
        self.assertNotIn("_spoken", json.dumps(d))

    def test_existing_spoken_kept(self):
        d = run(ok_env({"debtTL": {"totalDebt": 5.0, "totalDebt_spoken": "given"}}))["data"]
        self.assertEqual(d["debtTL"]["totalDebt_spoken"], "given")

    def test_english(self):
        d = run(ok_env(dict(DEBT, now="2026-10-12T10:30:00Z")), lang="en")["data"]
        self.assertEqual(d["statementDate_spoken"], "Monday 5 October 2026")
        self.assertEqual(d["now_spoken"], "Monday 12 October 2026 at 10:30")
        self.assertEqual(d["debtUSD"]["statementDebt_spoken"], "one thousand two hundred thirty-four dollars and fifty cents")
        self.assertEqual(d["debtUSD"]["minPayment_spoken"], "fifty cents")
        self.assertEqual(d["debtUSD"]["totalDebt_spoken"], "minus twelve dollars")
        self.assertEqual(d["debtTL"]["statementDebt_spoken"], "five thousand Turkish lira and thirteen kuruş")
        self.assertEqual(d["debtEUR"]["statementDebt_spoken"], "zero euros")
        d = run(ok_env({"debtGBP": {"a": 1.01, "b": 2.02}, "amount": 7.05}), lang="en")["data"]
        self.assertEqual(d["debtGBP"]["a_spoken"], "one pound and one penny")
        self.assertEqual(d["debtGBP"]["b_spoken"], "two pounds and two pence")
        self.assertEqual(d["amount_spoken"], "seven point zero five")

    def test_language_sources(self):
        res = ok_env({"d": "2026-10-12"})
        self.assertEqual(run(copy.deepcopy(res), "raw")["data"]["d_spoken"], "Monday 12 October 2026")   # default
        self.assertEqual(run(copy.deepcopy(res), "raw", vs={"w_lang": "tr"})["data"]["d_spoken"], "12 Ekim 2026 Pazartesi")
        code = build(rt={"names": {}, "language": "Türkçe"})
        self.assertEqual(run(copy.deepcopy(res), code=code)["data"]["d_spoken"], "12 Ekim 2026 Pazartesi")
        code = build("German")
        self.assertEqual(run(copy.deepcopy(res), code=code, vs={"w_lang": "tr"})["data"]["d_spoken"],
                         "Monday 12 October 2026")   # the generator's language wins; non-Turkish -> English words

    def test_number_words(self):
        cases = {1000: "bin lira", 1001000: "bir milyon bin lira", 100: "yüz lira", 110: "yüz on lira",
                 2000000000: "iki milyar lira", 999999: "dokuz yüz doksan dokuz bin dokuz yüz doksan dokuz lira",
                 1: "bir lira", 0.99: "doksan dokuz kuruş"}
        d = run(ok_env({"debtTL": dict(("a%d" % i, v) for i, v in enumerate(cases))}))["data"]["debtTL"]
        for i, (v, want) in enumerate(cases.items()):
            self.assertEqual(d["a%d_spoken" % i], want, v)
        d = run(ok_env({"debtUSD": {"a": 1000000.0, "b": 115.0, "c": 21.0}}), lang="en")["data"]["debtUSD"]
        self.assertEqual((d["a_spoken"], d["b_spoken"], d["c_spoken"]),
                         ("one million dollars", "one hundred fifteen dollars", "twenty-one dollars"))

    def test_fast_on_big_answers(self):
        big = {"items": [dict(DEBT) for _ in range(500)], "deep": DEBT}
        t0 = time.time()
        d = run(ok_env(big))["data"]
        self.assertLess(time.time() - t0, 2.0)
        self.assertIn("statementDate_spoken", d["items"][0])
        self.assertNotIn("statementDate_spoken", d["items"][100])   # bounded work per list


if __name__ == "__main__":
    unittest.main()
