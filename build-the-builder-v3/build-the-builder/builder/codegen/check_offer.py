# Tool check_offer (code_exec, called by the model): checks a date and/or an amount the customer named for one
# product before the read-back, with the same check_promise as the create_promise guard, so the two never disagree.
# `extra` is the model's arguments (product_id, date, amount); the dict assigned to `out` is the tool result.
args = extra if isinstance(extra, dict) else dict()
cal = make_calendar(RT)
today = today_of(vs)
prods = products_of(vs)
prod = find_product(prods, args.get("product_id"))
said = str(args.get("date") or "").strip()
# corrections ('Friday, sorry, Monday') and relative days ('end of the month') are resolved here; check_promise
# then gets an exact date, or a day that cannot be read ('?')
if said:
    said = iso(said_date(said, today, nm("next_weekday", ""), cal)) or "?"
# words, both separator conventions, corrections: the last stated amount; 'the full amount' / 'the minimum' are the
# product's full amount / floor; otherwise the old reading (digits only), and nothing readable is not checked
named = str(args.get("amount") or "").strip().lower()
amt = parse_amount(named) if named else None
if amt is None and named and prod is not None:
    amt = prod.get("floor") if re.search(r"\b(min|minimum|floor|lowest|least)\b", named) else (
        prod.get("full") if re.search(r"\b(full|whole|all|everything|total)\b", named) else to_number(named))
out = dict(ok=False, code="", date="", amount=None, suggest_date="", suggest_amount=None, what_to_do_next="")
if prod is None:
    out["code"] = "unknown_product"
    out["what_to_do_next"] = "Unknown product. Call check_offer again with a product_id exactly as listed in the account facts."
else:
    # a missing part is checked with a value that always passes, so only the part the customer named is judged
    chk = check_promise(prod, amt if amt is not None else prod.get("full"), said or prod.get("deadline"), today, cal,
                        promised_of(vs), nm("next_weekday", ""))
    code = chk.get("code") or "error"
    d = to_date(chk.get("date")) if said else None
    out["code"] = code
    out["ok"] = bool(chk.get("ok"))
    out["date"] = date_label(d) if d else ""
    out["amount"] = money(amt) if amt is not None else None
    sd = to_date(chk.get("suggest_date"))
    out["suggest_date"] = date_label(sd) if sd else ""
    out["suggest_amount"] = money(chk.get("suggest_amount")) if chk.get("suggest_amount") is not None else None
    what = " and ".join([x for x in (out["amount"], out["date"]) if x])
    if chk.get("ok"):
        out["what_to_do_next"] = "Valid for " + str(prod.get("id")) + ((": " + what) if what else "") + ". Use it in the read-back."
    elif code == "bad_date":
        out["what_to_do_next"] = "The day could not be read as one exact date. Ask the customer for the exact day, as the rules say for a vague date."
    elif code == "after_deadline":
        out["what_to_do_next"] = ("This day is after the latest date that can be recorded. Say the latest date you can record is "
                                  + out["suggest_date"] + " and offer it.")
    elif code == "not_business_day":
        out["what_to_do_next"] = ("This day is not a business day. Say only that it is not a business day, never call it a public or bank holiday, "
                                  "and offer " + out["suggest_date"] + " instead.")
    elif code == "before_today":
        out["what_to_do_next"] = "This day is in the past. Offer " + out["suggest_date"] + " instead."
    elif code == "below_floor":
        out["what_to_do_next"] = "This amount is below the floor. Say it cannot be recorded and offer " + str(out["suggest_amount"]) + ", as the rules say."
    elif code == "above_full":
        out["what_to_do_next"] = "This amount is more than the full past-due amount. Offer " + str(out["suggest_amount"]) + "."
    elif code == "already_promised":
        out["what_to_do_next"] = "A promise is already recorded for this product. It cannot be changed in this call; follow the rules for that."
    else:
        out["what_to_do_next"] = "The check did not work. Use the account facts to decide."
