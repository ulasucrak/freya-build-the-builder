#!/usr/bin/env python3
"""Generate openapi.json (OpenAPI 3.0) for the mock:  python3 final-mock/gen_openapi.py [out.json]

Inline schemas only (no $ref): the builder reads requestBody/response properties directly.
The servers URL is a placeholder (REPLACE) filled in after deploy.
"""
import json, os, sys

S = lambda d, **kw: {"type": "string", "description": d, **kw}
I = lambda d, **kw: {"type": "integer", "description": d, **kw}
N = lambda d, **kw: {"type": "number", "description": d, **kw}
B = lambda d, **kw: {"type": "boolean", "description": d, **kw}
DATE = "Date as dd.MM.yyyy, e.g. 05.11.2026."

CALL = {
    "type": "object",
    "description": "Call metadata the bank logs on its side. Nothing depends on it and the test environment does not check it.",
    "properties": {
        "ani": S("Caller's phone number."),
        "lang": S("Call language; \"tr\".", default="tr"),
    },
}

RESULT = {
    "type": "object",
    "description": "Outcome of the call. Success = HTTP 200 and returnCode \"1\". Any other returnCode is an error (then the answer has no data object).",
    "properties": {
        "guid": S("Bank trace id of this answer (give it when opening a ticket)."),
        "returnCode": S("\"1\" on success, otherwise the error code: INVALID_ARGUMENTS_ERROR, FB_IVR_CHANNEL_102, FB_IVR_CHANNEL_107, FB_IVR_CHANNEL_120, FB_IVR_CHANNEL_121 or FBE00099."),
        "messageText": S("Short status text for logs only, never read to the caller."),
        "code": S("Same value as returnCode."),
        "message": S("Same value as messageText."),
        "type": S("null on success, \"ERROR\" or \"WARNING\" on errors.", nullable=True, enum=["ERROR", "WARNING", None]),
        "details": {
            "type": "object",
            "description": "Diagnostics for logs; do not branch on them.",
            "properties": {
                "guid": S("Same as result.guid."),
                "rootCause": S("Diagnostic string or null.", nullable=True),
                "errorOrigin": S("Diagnostic string or null.", nullable=True),
            },
        },
    },
}

def _plain(node):
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items() if k != "description"}
    return node


ERROR_SCHEMA = {
    "type": "object",
    "description": "Error answer: only a result object, no data.",
    "properties": {"result": _plain(RESULT)},
}


def err(text):
    return {"description": text, "content": {"application/json": {"schema": ERROR_SCHEMA}}}


E400 = lambda extra="": err("INVALID_ARGUMENTS_ERROR (type ERROR): a required data field is missing or empty (the message names the field)" + extra + ".")
E500 = err("FBE00099 (type ERROR): technical error on the bank's side. The service failed; nothing was done.")


def op(op_id, summary, description, req_props, required, resp_props, ok_desc, errors):
    body = {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {"type": "object", "description": "Service fields: " + "; ".join(
                "%s (%s%s): %s" % (k, v.get("type"), ", required" if k in required else ", optional", v.get("description", "").rstrip("."))
                for k, v in req_props.items()) + ".",
                "properties": req_props, "required": required},
            "call": CALL,
        },
    }
    responses = {
        "200": {
            "description": ok_desc,
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "data": {"type": "object", "properties": resp_props},
                    "result": RESULT,
                },
            }}},
        }
    }
    responses.update(errors)
    return {"post": {
        "operationId": op_id,
        "summary": summary,
        "description": description,
        "requestBody": {"required": True, "content": {"application/json": {"schema": body}}},
        "responses": responses,
    }}


CUSTOMER_NO = S("Customer number the phone menu hands over with the call, e.g. \"90000001\". Sent as a string or a number, both accepted.")
CARD_IN = S("Card number exactly as cards_list returned it in cards[].cardNumber (masked in the test environment, e.g. \"540000******1234\"). Never rebuilt from digits the caller says.")
WEEK_IN = I("Requested statement week, 1 to 4 (week of the month the statement is cut). Number or string accepted. Must differ from the card's current statementWeek.", minimum=1, maximum=4)
DAY_IN = I("The card's current statementDay from statement_week_get (required by the request model; the bank recalculates the day itself).")

DEBT = {
    "type": "object",
    "description": "Debt in one currency. The block can be missing for a currency (e.g. no debtGBP): missing is not the same as NO_DEBT, never assume zeros.",
    "properties": {
        "statementPaymentStatus": S("HAS_DEBT or NO_DEBT for the issued statement in this currency.", enum=["HAS_DEBT", "NO_DEBT"]),
        "transferAmount": N("Amount carried over from the previous period."),
        "statementDebt": N("Debt on the issued statement."),
        "minPayment": N("Minimum payment on the issued statement."),
        "remainingDebt": N("What is left of statementDebt after payments made since the statement."),
        "remainingMinPayment": N("What is left of minPayment after payments made since the statement."),
        "currentPeriodExpense": N("Spending after the statement date."),
        "totalDebt": N("Total debt including spending after the statement. Can be negative (credit balance; status NO_DEBT then)."),
        "totalDebtPaymentStatus": S("HAS_DEBT or NO_DEBT for totalDebt.", enum=["HAS_DEBT", "NO_DEBT"]),
        "remainAutomaticPaymentAmount": N("Amount still to be collected by automatic payment; meaningful only when automaticPaymentFlag is Y."),
    },
}

paths = {}
paths["/voice/v1/cards.list"] = op(
    "cards_list",
    "List the caller's cards the assistant may talk about.",
    "Returns the customer's active cards (status A) for a customer number. Two cards can share the same productName and cardNumberLast4 and differ only in the first six digits. exitState is 1_CARD for one card, MULTI_CARD for several. HTTP 404 FB_IVR_CHANNEL_102 (WARNING) means the customer has no card the assistant can work with; HTTP 500 FBE00099 is a technical error. Amounts and dates: none here.",
    {"customerNo": CUSTOMER_NO, "aiFlow": S("Use case asking; always \"CardOps\" for the statement/debt flow (not validated in test).", enum=["CardOps"], default="CardOps")},
    ["customerNo"],
    {
        "cards": {"type": "array", "description": "The customer's cards.", "items": {"type": "object", "properties": {
            "cardNumber": S("Card number to send to the other services exactly as given (masked first 6 + last 4 in the test environment)."),
            "cardNumberLast4": S("Last four digits of the card; not unique across the customer's cards."),
            "productName": S("Card product name, free text from the card system (can be empty)."),
            "status": S("Card status; always A (active) in this list.", enum=["A"]),
            "isMainCard": B("false for a card that is not the customer's own main card (it is still in the list)."),
        }}},
        "validCardsCount": I("Number of cards in the list."),
        "exitState": S("1_CARD or MULTI_CARD.", enum=["1_CARD", "MULTI_CARD"]),
    },
    "The customer's cards.",
    {"400": E400(" (customerNo)"),
     "404": err("FB_IVR_CHANNEL_102 (type WARNING): the customer has no valid card (unknown customer numbers get this too). Not a technical failure."),
     "500": E500},
)
paths["/voice/v1/cards.balance"] = op(
    "cards_balance",
    "Get the card's statement and due dates and its debt per currency.",
    "Returns the last issued statement date and its due date (statementDate, lastPayDate), the coming ones (nextStatementDate, nextLastPayDate), and one debt block per currency: debtTL, debtUSD, debtEUR, debtGBP. A block can be missing. Amounts are plain numbers with a dot as decimal separator, dates dd.MM.yyyy. The cardNumber in this answer is masked differently (first 4 + last 4); do not match it against the card list. HTTP 404 FB_IVR_CHANNEL_120 = card not found; 500 FBE00099 = technical error. The answer can occasionally take more than ten seconds.",
    {"cardNumber": CARD_IN},
    ["cardNumber"],
    {
        "cardNumber": S("Card number masked as first 4 + last 4 (e.g. 5400********1234); not the cards_list format."),
        "cardNumberLast4": S("Last four digits of the card."),
        "productName": S("Card product name."),
        "statementDate": S("Date of the last issued statement. " + DATE),
        "lastPayDate": S("Due date of the last issued statement. " + DATE),
        "nextStatementDate": S("Date of the coming statement. " + DATE),
        "nextLastPayDate": S("Due date of the coming statement. " + DATE),
        "debtTL": DEBT,
        "debtUSD": DEBT,
        "debtEUR": DEBT,
        "debtGBP": DEBT,
        "statementPaymentStatus": S("HAS_DEBT or NO_DEBT for the issued statement, card overall.", enum=["HAS_DEBT", "NO_DEBT"]),
        "totalDebtPaymentStatus": S("HAS_DEBT or NO_DEBT for the total debt, card overall.", enum=["HAS_DEBT", "NO_DEBT"]),
        "automaticPaymentFlag": S("Y if automatic payment is set up, otherwise N.", enum=["Y", "N"]),
    },
    "Statement dates and debt per currency.",
    {"400": E400(" (cardNumber)"),
     "404": err("FB_IVR_CHANNEL_120 (type WARNING): card not found."),
     "500": E500},
)
paths["/voice/v1/statement.week.get"] = op(
    "statement_week_get",
    "Get the card's current statement week and day, and whether it shares a statement with another card.",
    "Returns statementWeek (1-4) and statementDay (day of month) as numbers, the statement and due dates, and joint-statement info: relationType is empty when the card has its own statement, M (this is the main card of the pair) or C (this card is linked to the main card) when two cards share one statement; relatedCardNumber and relatedCardProductName then name the other card. Changing the week of either card moves the shared statement of both. There is no field saying whether the week may still be changed. HTTP 404 FB_IVR_CHANNEL_121 = card not found; 500 FBE00099 = technical error.",
    {"cardNumber": CARD_IN},
    ["cardNumber"],
    {
        "cardNumber": S("Card number as in the card list."),
        "cardNumberLast4": S("Last four digits of the card."),
        "productName": S("Card product name."),
        "statementWeek": I("Current statement week, 1 to 4.", minimum=1, maximum=4),
        "statementDay": I("Current statement day of the month."),
        "statementDate": S("Last issued statement date. " + DATE),
        "lastPayDate": S("Its due date. " + DATE),
        "nextStatementDate": S("Coming statement date. " + DATE),
        "nextLastPayDate": S("Its due date. " + DATE),
        "relationType": S("Empty string = own statement; M = main card of a joint statement; C = card linked to the main card.", enum=["", "M", "C"]),
        "relatedCardNumber": S("The other card of a joint statement, else empty string."),
        "relatedCardProductName": S("Product name of the other card of a joint statement, else empty string."),
    },
    "The card's statement week, day, dates and joint-statement info.",
    {"400": E400(" (cardNumber)"),
     "404": err("FB_IVR_CHANNEL_121 (type WARNING): card not found."),
     "500": E500},
)
paths["/voice/v1/statement.week.decide"] = op(
    "statement_week_decide",
    "Preview the statement and due dates the card would get with a new statement week (changes nothing).",
    "Preview only: returns the first statement date (statementDate), its due date (statementDueDate, not lastPayDate) and the statement day the card would get with the requested week. It does not change the card. Requesting the card's current week, an unknown card or a week outside 1-4 gives HTTP 400 INVALID_ARGUMENTS_ERROR. responseCode 0 = fine. HTTP 500 FBE00099 = technical error.",
    {"cardNumber": CARD_IN, "newStatementWeek": WEEK_IN, "statementDay": DAY_IN},
    ["cardNumber", "newStatementWeek", "statementDay"],
    {
        "statementDate": S("First statement date under the requested week. " + DATE),
        "statementDueDate": S("Due date of that statement. " + DATE),
        "statementDay": I("Statement day of the month under the requested week."),
        "responseCode": I("0 = fine."),
        "responseMessage": S("Status text, e.g. Success."),
    },
    "The dates the card would get with the requested week.",
    {"400": E400("; also for the card's current week, an unknown card or a week outside 1-4"),
     "500": E500},
)
paths["/voice/v1/statement.week.change"] = op(
    "statement_week_change",
    "Change the card's statement week (call only after the caller agreed to the previewed dates).",
    "Changes the statement week of the card (for a joint statement, both cards move). Success = exitState OK and responseCode 0. statementWeek and statementDay come back as strings here; ignore statementDay and use the dates from statement_week_decide. Requesting the card's current week, an unknown card or a week outside 1-4 gives HTTP 400 INVALID_ARGUMENTS_ERROR; HTTP 500 FBE00099 = technical error. Has a side effect: do not retry automatically.",
    {"cardNumber": CARD_IN, "newStatementWeek": WEEK_IN, "statementDay": DAY_IN},
    ["cardNumber", "newStatementWeek", "statementDay"],
    {
        "responseCode": I("0 = fine."),
        "responseMessage": S("Status code text, e.g. \"00\"."),
        "statementWeek": S("The new statement week, as a string."),
        "statementDay": S("Day as a string; ignore it (use the dates from the preview)."),
        "exitState": S("OK when the change went through.", enum=["OK"]),
    },
    "The statement week was changed.",
    {"400": E400("; also for the card's current week, an unknown card or a week outside 1-4"),
     "401": {"description": "Gateway: token missing or expired; get a new token and retry once. Never returned by the test environment (tokens are not checked)."},
     "500": E500},
)
paths["/voice/v1/contact.email.get"] = op(
    "contact_email_get",
    "Get the customer's registered e-mail address and phone.",
    "Returns the registered e-mail in ibanEmail (ttsEmail is the same address prepared for reading aloud) and the phone in ibanCountryCode / ibanAreaCode / ibanPhoneNum. No registered e-mail = communicationType Phone and/or an empty ibanEmail: check the field, not only the type. The iban prefix is historical and has nothing to do with IBANs. Unknown customer or empty intAddressInfoType gives HTTP 400 INVALID_ARGUMENTS_ERROR; HTTP 500 FBE00099 = technical error.",
    {"customerNo": CUSTOMER_NO, "intAddressInfoType": S("What to fetch: E e-mail, P phone, B both. Required (empty gives 400); the test environment answers the same for all three. Send E to get the e-mail.", enum=["E", "P", "B"], default="E")},
    ["customerNo", "intAddressInfoType"],
    {
        "communicationType": S("Both when an e-mail and a phone are registered, Phone when only a phone is."),
        "ibanCountryCode": S("Phone country code."),
        "ibanAreaCode": S("Phone area code."),
        "ibanPhoneNum": S("Phone number."),
        "ibanEmail": S("Registered e-mail address; empty string when none."),
        "ttsEmail": S("The same e-mail address prepared for reading aloud; empty string when none."),
        "commGramType": S("Communication preference code."),
        "exitState": S("Done on success."),
    },
    "The customer's registered contact details.",
    {"400": E400(" (customerNo, intAddressInfoType); also for an unknown customer"),
     "500": E500},
)
paths["/voice/v1/statement.pdf.send"] = op(
    "statement_pdf_send",
    "Send the issued statement as a PDF to an e-mail address.",
    "Sends the statement PDF of the card for the given currency and statement date to emailAddress. The bank sends to whatever address is in the field and does not compare it with the registered one. Success = data.exitState DONE. HTTP 404 FB_IVR_CHANNEL_107 (WARNING) = no statement document to send; HTTP 400 INVALID_ARGUMENTS_ERROR = a field is missing or empty; HTTP 500 FBE00099 = technical error. Has a side effect: do not retry automatically.",
    {
        "customerNo": CUSTOMER_NO,
        "cardNumber": CARD_IN,
        "currencyType": S("Currency of the statement: TL, USD, EUR or GBP.", enum=["TL", "USD", "EUR", "GBP"]),
        "statementDate": S("The statementDate from cards_balance (the issued statement). " + DATE),
        "emailAddress": S("E-mail address to send the PDF to."),
    },
    ["customerNo", "cardNumber", "currencyType", "statementDate", "emailAddress"],
    {"exitState": S("DONE when the PDF was sent.", enum=["DONE"])},
    "The statement PDF was sent.",
    {"400": E400(""),
     "404": err("FB_IVR_CHANNEL_107 (type WARNING): no statement document to send (\"Pdf dokümanı bulunamadı\")."),
     "500": E500},
)

spec = {
    "openapi": "3.0.3",
    "info": {
        "title": "Freya Bank voice statement and debt services (test-environment mock)",
        "version": "2026-10-01",
        "description": "Mock of Freya Bank's seven voice-assistant services for the statement/debt use case, per the bank's service contract of 1 October 2026. Every service is POST with JSON in and out. Request body: {\"data\": {service fields}, \"call\": {\"ani\": caller's number, \"lang\": \"tr\"}}. Response: {\"data\": {...}, \"result\": {...}}; success = HTTP 200 and result.returnCode \"1\"; error answers carry only result (returnCode/code, messageText/message, type ERROR or WARNING). Dates are strings dd.MM.yyyy; amounts are JSON numbers with a dot decimal separator, never rounded. Services return raw values only, no sentences for the caller. The caller is already identified: start from cards_list with the customer number. Send cardNumber exactly as cards_list returned it. The test environment does not check the token, headers or the call object; only data fields decide the answer.",
    },
    "servers": [{"url": "https://freya-statement-mock.REPLACE.workers.dev"}],
    "paths": paths,
    "components": {},
}
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "openapi.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(spec, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("wrote", out)
