# Tool check_offer (code_exec, called by the model): checks a date and/or an amount the customer named for one
# product before the read-back, with the same check_promise as the create_promise guard, so the two never disagree.
# `extra` is the model's arguments (product_id, date, amount); the dict assigned to `out` is the tool result.
args = extra if isinstance(extra, dict) else dict()
cal = make_calendar(RT)
today = today_of(vs)
prods = products_of(vs)
prod = find_product(prods, args.get("product_id"))
said = str(args.get("date") or "").strip()
prev = str(vget(vs, "w_offer_vague") or "")
wd = re.fullmatch(r"(?:on\s+)?([a-z]+day)", said.lower())
wk = re.search(r"\b(next|this)\s+week\b", prev.lower())
if wd and wk and today is not None and weekday_from_word(wd.group(1)) is not None:
    # 'end of next week' ... 'Thursday': that weekday in the week named before (no 'next <weekday>' rule needed)
    said = iso(week_monday(today) + datetime.timedelta(days=(7 if wk.group(1) == "next" else 0) + weekday_from_word(wd.group(1))))
amt = to_number(args.get("amount"))
out = dict(ok=False, code="", date="", amount=None, suggest_date="", suggest_amount=None, what_to_do_next="")
refused = str(args.get("refused") or "").strip().lower() in ("yes", "true", "1")
too_much = str(args.get("too_much") or "").strip().lower() in ("yes", "true", "1")
if prod is not None and (refused or too_much) and not said and amt is None:
    # persuasion offers in code: the model reports a refusal / 'too much', the code says the one offer to make now
    pid = str(prod.get("id"))
    fl, full = prod.get("floor"), prod.get("full")
    tk = key_of("w_toomuch_", pid)
    if refused and not too_much and prev and to_date(prod.get("deadline")):
        # no exact day after a vague period: the vague-date rule applies (propose the deadline), not a persuasion attempt
        vset(vs, "w_offer_vague", "")
        dl = to_date(prod.get("deadline"))
        a = fl if str(vget(vs, tk, "") or "") and fl is not None else full
        out.update(code="vague_deadline", ok=True, amount=money(a), date=date_label(dl))
        out["what_to_do_next"] = ("No exact day after a vague period: propose " + money(a) + " on the latest date you can record, " + date_label(dl)
                                  + ", as the rules say for a vague date. This is not a persuasion attempt; if the customer refuses it, call check_offer with refused=yes.")
    elif too_much and fl is not None and fl != full and not str(vget(vs, tk, "") or ""):
        vset(vs, tk, "yes")
        out.update(code="floor_offer", ok=True, amount=money(fl))
        out["what_to_do_next"] = ("Offer " + money(fl) + " for " + str(prod.get("name")) + " once, for the same day that was being discussed, as the rules say "
                                  "for an amount that is too much. Make no other offer now; if the customer refuses it, call check_offer with refused=yes.")
    else:
        rk = key_of("w_refusals_", pid)
        n = int(num(vget(vs, rk, 0))) + 1
        vset(vs, rk, str(n))
        offers = prod.get("offers") or []
        if n <= len(offers):
            o = offers[n - 1]
            a = o.get("alt") if (str(vget(vs, tk, "") or "") and o.get("alt") is not None) else o.get("amount")
            d = to_date(o.get("date"))
            out.update(code="offer", ok=True, amount=money(a), date=date_label(d))
            out["what_to_do_next"] = ("Persuasion attempt " + str(n) + " of " + str(len(offers)) + " for " + str(prod.get("name")) + ": offer exactly "
                                      + money(a) + " on " + date_label(d) + ", once. Make no other offer; if the customer refuses it, call check_offer with refused=yes.")
        else:
            out.update(code="no_offers_left", ok=False)
            out["what_to_do_next"] = ("No persuasion attempts are left for " + str(prod.get("name")) + ". Make no other offer for it (no other amount, no other day). "
                                      "Continue with the next product, or follow the rules for a refusal.")
elif prod is None:
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
        vset(vs, "w_offer_vague", said)
        dl = to_date(prod.get("deadline"))
        if prev:
            out["what_to_do_next"] = ("Still no exact day. Propose the latest date you can record, " + date_label(dl) + ", as the rules say for a vague date."
                                      if dl else "Still no exact day: follow the rules for a vague date.")
        else:
            out["what_to_do_next"] = ("The day could not be read as one exact date. Ask once for the exact day. If the customer still gives none, propose the latest date you can record, "
                                      + date_label(dl) + ", as the rules say for a vague date." if dl else "The day could not be read as one exact date. Ask once for the exact day, as the rules say for a vague date.")
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
