"""Sends every request of test-customers.json to the mock and compares status and body exactly."""
import json, sys, time, urllib.request, urllib.error
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8799/voice/v1/"
src = json.load(open(sys.argv[1]))
def post(svc, data):
    req = urllib.request.Request(BASE + svc, data=json.dumps({"data": data, "call": {"ani": "x", "lang": "tr"}}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as r: return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
ok = bad = 0; fails = []
for c in src["customers"]:
    cn = c["customerNo"]
    for svc, entry in c["services"].items():
        cases = []
        if "http" in entry: cases.append(({"customerNo": cn, "aiFlow": "CardOps", "intAddressInfoType": "E", "cardNumber": "540000******0000",
                                           "currencyType": "TL", "statementDate": "01.01.2026", "emailAddress": "a@b.example"}, entry))
        else:
            for card, sub in entry.items():
                if "http" in sub: cases.append(({"cardNumber": card}, sub))
                else:
                    for wk, e in sub.items(): cases.append(({"cardNumber": card, "newStatementWeek": int(wk), "statementDay": 5}, e))
        for data, exp in cases:
            if exp.get("delaySeconds"): print("delayed entry:", svc, cn, exp["delaySeconds"], "s"); t0 = time.time()
            st, body = post(svc, data)
            if exp.get("delaySeconds"): print("   took %.1f s" % (time.time() - t0))
            if st == exp["http"] and body == exp["response"]: ok += 1
            else: bad += 1; fails.append((svc, cn, data.get("cardNumber"), st, exp["http"]))
print(f"exact matches: {ok}, mismatches: {bad}"); [print("  ", f) for f in fails[:10]]
for name, svc, data in [("unknown customer", "cards.list", {"customerNo": "11111111"}), ("unknown card", "cards.balance", {"cardNumber": "540000******9999"}),
                        ("unknown week", "statement.week.decide", {"cardNumber": "540000******1234", "newStatementWeek": 1}),
                        ("empty field", "cards.balance", {"cardNumber": ""}), ("number customerNo", "cards.list", {"customerNo": 90000001}),
                        ("no pdf entry", "statement.pdf.send", {"customerNo": "11111111", "cardNumber": "x", "currencyType": "TL", "statementDate": "x", "emailAddress": "x"})]:
    st, body = post(svc, data); print(f"{name:18} -> {st} {body.get('result', {}).get('code')} {body.get('result', {}).get('message')}")
