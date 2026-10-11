#!/usr/bin/env python3
"""Generate records.js (the RECORDS table of the mock) from the bank's test-customers.json.

    python3 final-mock/gen_records.py [path/to/test-customers.json]

Mechanical and reproducible: every answer of the test environment is copied as-is. Each response body is
stored as its exact JSON text (minified), so number literals such as 0.0 or 5000.13 come back byte-for-byte
the way the bank's file writes them. Names and other fields the answers do not need are dropped.

Layout of records.js:
  CUSTOMERS[customerNo][service]               -> entry   (cards.list, contact.email.get, statement.pdf.send)
  CARDS[cardNumber][service]                   -> entry   (cards.balance, statement.week.get)
  CARDS[cardNumber][service][newStatementWeek] -> entry   (statement.week.decide, statement.week.change)
  entry = {"http": <status>, "body": "<exact JSON text>"} plus "delaySeconds" when the file has one.
  SAMPLE = {"customerNo", "cardNumber"}: the record served when a request field is an unfilled
           template variable such as "{{customerNumber}}".
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CASE_REL = os.path.join("Build the Builder - Final Case", "test-environment", "test-customers.json")
CANDIDATES = [
    os.path.join(HERE, "..", CASE_REL),              # main checkout: <repo>/final-mock -> <repo>/<case folder>
    os.path.join(HERE, "..", "..", "..", CASE_REL),  # task worktree: <repo>/.worktrees/<id>/final-mock
]

CUSTOMER_SERVICES = ("cards.list", "contact.email.get", "statement.pdf.send")
CARD_SERVICES = ("cards.balance", "statement.week.get")
CARD_WEEK_SERVICES = ("statement.week.decide", "statement.week.change")


def find_source(argv):
    if len(argv) > 1:
        return argv[1]
    if os.environ.get("TEST_CUSTOMERS_JSON"):
        return os.environ["TEST_CUSTOMERS_JSON"]
    for c in CANDIDATES:
        if os.path.isfile(c):
            return os.path.normpath(c)
    sys.exit("test-customers.json not found; pass its path as the first argument")


def entry(leaf, where):
    if not isinstance(leaf, dict) or "http" not in leaf or "response" not in leaf:
        sys.exit("unexpected entry at %s: %r" % (where, leaf))
    extra = set(leaf) - {"http", "response", "delaySeconds"}
    if extra:
        sys.exit("unknown keys %s at %s" % (sorted(extra), where))
    out = {"http": int(leaf["http"]),
           "body": json.dumps(leaf["response"], ensure_ascii=False, separators=(",", ":"))}
    if leaf.get("delaySeconds"):
        out["delaySeconds"] = leaf["delaySeconds"]
    return out


def build(src):
    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)
    customers, cards, sample = {}, {}, None
    for cu in data["customers"]:
        cno = str(cu["customerNo"]).strip()
        if cno in customers:
            sys.exit("duplicate customer %s" % cno)
        services = cu.get("services") or {}
        unknown = set(services) - set(CUSTOMER_SERVICES + CARD_SERVICES + CARD_WEEK_SERVICES)
        if unknown:
            sys.exit("unknown services %s for %s" % (sorted(unknown), cno))
        customers[cno] = {s: entry(services[s], "%s/%s" % (cno, s)) for s in CUSTOMER_SERVICES if s in services}
        for s in CARD_SERVICES + CARD_WEEK_SERVICES:
            for card, node in (services.get(s) or {}).items():
                card_entry = cards.setdefault(card, {"customerNo": cno})
                if card_entry["customerNo"] != cno:
                    sys.exit("card %s appears under customers %s and %s" % (card, card_entry["customerNo"], cno))
                if s in CARD_SERVICES:
                    card_entry[s] = entry(node, "%s/%s/%s" % (cno, s, card))
                else:
                    card_entry[s] = {str(w).strip(): entry(leaf, "%s/%s/%s/%s" % (cno, s, card, w))
                                     for w, leaf in sorted(node.items())}
        # sample record: first customer whose every per-customer answer is a 200 and whose first card
        # answers 200 everywhere, without delay
        if sample is None and all(customers[cno].get(s, {}).get("http") == 200 for s in CUSTOMER_SERVICES):
            first = json.loads(customers[cno]["cards.list"]["body"])["data"]["cards"][0]["cardNumber"]
            c = cards.get(first) or {}
            if all(c.get(s, {}).get("http") == 200 and not c[s].get("delaySeconds") for s in CARD_SERVICES) and \
                    all(c.get(s) and all(e["http"] == 200 for e in c[s].values()) for s in CARD_WEEK_SERVICES):
                sample = {"customerNo": cno, "cardNumber": first}
    if sample is None:
        sys.exit("no customer usable as sample record")
    for c in cards.values():
        del c["customerNo"]
    return customers, cards, sample


def main(argv):
    src = find_source(argv)
    customers, cards, sample = build(src)
    out = os.path.join(HERE, "records.js")
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("// GENERATED by gen_records.py from the bank's test-environment/test-customers.json. Do not edit.\n")
        fh.write("// Regenerate: python3 final-mock/gen_records.py [path/to/test-customers.json]\n")
        fh.write("export const SAMPLE = %s;\n" % dump(sample))
        fh.write("export const CUSTOMERS = %s;\n" % dump(customers))
        fh.write("export const CARDS = %s;\n" % dump(cards))
    print("wrote %s: %d customers, %d cards, %d bytes (source %s)"
          % (out, len(customers), len(cards), os.path.getsize(out), os.path.normpath(src)))


if __name__ == "__main__":
    main(sys.argv)
