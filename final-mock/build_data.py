"""Builds data.json for the Worker from the bank's test-environment file (test-customers.json) and the error
table of the service contract (section 4). Usage: python build_data.py <path to test-customers.json>"""
import json, sys, uuid

src = json.load(open(sys.argv[1]))
BASES = ["/voice/v1/", "/"]

def err(status, code, typ, message, root):
    g = uuid.uuid5(uuid.NAMESPACE_URL, code + message).hex
    return {"status": status, "body": {"result": {"guid": g, "returnCode": code, "messageText": message, "code": code, "message": message,
            "type": typ, "details": {"guid": g, "rootCause": root, "errorOrigin": "ivr-gateway-test:ivr-channel-v0"}}}}

NF = {  # contract section 4: the 404 of each service; services without one answer a 400 INVALID_ARGUMENTS_ERROR
    "cards.list": err(404, "FB_IVR_CHANNEL_102", "WARNING", "Müşteriye ait geçerli kart bilgisi bulunamadı.", "NotFoundException"),
    "cards.balance": err(404, "FB_IVR_CHANNEL_120", "WARNING", "Kart bakiye servisi hatası: kart bulunamadı", "NotFoundException"),
    "statement.week.get": err(404, "FB_IVR_CHANNEL_121", "WARNING", "Kart bulunamadı", "NotFoundException"),
    "statement.pdf.send": err(404, "FB_IVR_CHANNEL_107", "WARNING", "Pdf dokümanı bulunamadı", "com.freyabank.ivrgateway.ivrchannel.rest.exception.NotFoundException"),
}
INVALID = lambda msg: err(400, "INVALID_ARGUMENTS_ERROR", "ERROR", msg, "InvalidArgumentsException")
REQUIRED = {"cards.list": ["customerNo"], "cards.balance": ["cardNumber"], "statement.week.get": ["cardNumber"],
            "statement.week.decide": ["cardNumber", "newStatementWeek"], "statement.week.change": ["cardNumber", "newStatementWeek"],
            "contact.email.get": ["customerNo", "intAddressInfoType"], "statement.pdf.send": ["customerNo", "cardNumber", "currencyType", "statementDate", "emailAddress"]}
KEY = {"cards.list": ["customerNo"], "contact.email.get": ["customerNo"], "statement.pdf.send": ["customerNo"],
       "cards.balance": ["cardNumber"], "statement.week.get": ["cardNumber"],
       "statement.week.decide": ["cardNumber", "newStatementWeek"], "statement.week.change": ["cardNumber", "newStatementWeek"]}

def answer(e):
    a = {"status": e["http"], "body": e["response"]}
    if e.get("delaySeconds"): a["delaySeconds"] = e["delaySeconds"]
    return a

rules = {s: [] for s in KEY}
for c in src["customers"]:
    for svc, entry in (c.get("services") or {}).items():
        if svc not in KEY: continue
        if "http" in entry:                                   # keyed by customer
            rules[svc].append(dict(match={"customerNo": c["customerNo"]}, **answer(entry)))
        else:
            for card, sub in entry.items():
                if "http" in sub:                             # keyed by card
                    rules[svc].append(dict(match={"cardNumber": card}, **answer(sub)))
                else:
                    for week, e in sub.items():               # keyed by card, then week
                        rules[svc].append(dict(match={"cardNumber": card, "newStatementWeek": week}, **answer(e)))

routes = []
for svc in KEY:
    routes.append({"method": "POST", "paths": [b + svc for b in BASES], "required": REQUIRED[svc],
                   "invalid": INVALID("{field} cannot be empty"), "rules": rules[svc],
                   "default": NF.get(svc) or INVALID("invalid arguments")})
routes.append({"method": "POST", "paths": ["/oauth2/token"], "rules": [],                     # contract section 2 (not checked in test)
               "default": {"status": 200, "body": {"access_token": "test-token", "token_type": "Bearer", "expires_in": 1800}}})
json.dump({"not_found": {"error": "not found"}, "routes": routes}, open("data.json", "w"), ensure_ascii=False, indent=1)
print({s: len(r) for s, r in rules.items()}, "delays:", sum(1 for r in rules.values() for x in r if x.get("delaySeconds")))
