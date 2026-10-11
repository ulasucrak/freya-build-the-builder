# Tool check_offer (code_exec, called by the model): checks a date and/or an amount the customer named for one
# product before the read-back, with the same check_promise as the create_promise guard, so the two never disagree.
# `extra` is the model's arguments (product_id, date, amount); the dict assigned to `out` is the tool result.
args = extra if isinstance(extra, dict) else dict()
cal = make_calendar(RT)
today = today_of(vs)
prod = find_product(products_of(vs), args.get("product_id"))
said = str(args.get("date") or "").strip()
named = str(args.get("amount") or "").strip().lower()
# per-product memory (w_offers_<id>, one variable per product: parallel calls merge key by key): o offers this tool
# had the agent make, r the ones the customer turned down, f the failing codes since the last valid check, p the last
# bare number, s the pending suggestion. The agent does not call this tool for its own offers.
mk = key_of("w_offers_", prod.get("id")) if prod else ""
m = load_json(vget(vs, mk), dict()) if prod else dict()
m = m if isinstance(m, dict) else dict()
for k in "orf":
    m[k] = m.get(k) if isinstance(m.get(k), list) else []
# a number split over two messages ('14', then '00 next Tuesday'): '00' / '000' alone can only continue the bare
# number before it, so the two are one amount (1400); anything else is read as said
amt = None
fr = re.match(r"(00?0)(?![\d.,])\s*(.*)$", named or said)
if m.get("p") and fr:
    amt = to_number(str(m["p"]) + fr.group(1))
    said = said if named else fr.group(2).strip()
m["p"] = "" if fr else next((x for x in (named, said) if re.match(r"\d{1,3}$", x)), "")
# a period with no day in it ('end of next week', 'end of the month') is vague: the rules say ask once for the exact
# day, then propose the deadline. It goes back in the result (`period`), so the next call can put a weekday said
# alone ('Thursday') inside it; the engine keeps the last result in the check_offer_response variables
per = ""
if said and said_period(said, today, cal) and not re.search(DAY_NAMED_RE, day_words_to_digits(said_parts(said)[-1])):
    per = said
    said = "?"
# corrections ('Friday, sorry, Monday') and relative days are resolved here; check_promise then gets an exact date,
# or a day that cannot be read ('?')
if said and not per:
    said = iso(period_day(said, said if said_period(said, today, cal) else str(vget(vs, "check_offer_response.data.period") or ""), today, cal)
               or said_date(said, today, nm("next_weekday", ""), cal)) or "?"
# words, both separator conventions, corrections: the last stated amount; 'the full amount' / 'the minimum' are the
# product's full amount / floor; otherwise the old reading (digits only), and nothing readable is not checked
if amt is None and named:
    amt = parse_amount(named)
    if amt is None and prod is not None:
        amt = prod.get("floor") if re.search(r"\b(min|minimum|floor|lowest|least)\b", named) else (
            prod.get("full") if re.search(r"\b(full|whole|all|everything|total)\b", named) else to_number(named))
out = dict(ok=False, code="", date="", amount=None, suggest_date="", suggest_amount=None, period=per)
if prod is None:
    out["code"] = "unknown_product"
    out["what_to_do_next"] = "Unknown product: call check_offer again with a product_id from the account facts."
else:
    pid = str(prod.get("id"))
    # a missing part is checked with a value that always passes, so only the part the customer named is judged.
    # Recorded promises: the post-hook's marks (the create_promise guard also counts unconfirmed parallel ones)
    chk = check_promise(prod, amt if amt is not None else prod.get("full"), said or prod.get("deadline"), today, cal,
                        csv_list(vget(vs, "w_promised")) + list(per_key(vs, "w_promised_")), nm("next_weekday", ""))
    code = chk.get("code") or "error"
    d = to_date(chk.get("date")) if said else None
    out["code"] = code
    out["ok"] = ok = bool(chk.get("ok"))
    out["date"] = date_label(d) if d else ""
    out["amount"] = money(amt) if amt is not None else None
    sd = to_date(chk.get("suggest_date"))
    out["suggest_date"] = date_label(sd) if sd else ""
    out["suggest_amount"] = money(chk.get("suggest_amount")) or None
    what = " and ".join([x for x in (out["amount"], out["date"]) if x])
    cur = [x for x in (out["amount"], iso(d)) if x]
    dl = date_label(to_date(prod.get("deadline")))
    sg = out["suggest_date"]
    t = ("Valid for " + pid + ((": " + what) if what else "") + ". Use it in the read-back.") if ok else dict(
        bad_date=("This is a period, not one exact day. " if per else "The day could not be read as one exact date. ")
        + "Ask once for the exact day and call check_offer with it." + (" If they still name no day, propose the deadline, " + dl
                                                                        + ", with the amount being discussed." if dl else ""),
        after_deadline="After the latest day that can be recorded. Say the latest day you can record is " + sg + " and offer it.",
        not_business_day="Not a business day: say only that (never a holiday) and offer " + sg + " instead.",
        before_today="This day is in the past. Offer " + sg + " instead.",
        below_floor="Below the floor: say it cannot be recorded and offer " + str(out["suggest_amount"]) + ", as the rules say.",
        above_full="More than the full past-due amount. Offer " + str(out["suggest_amount"]) + ".",
        bad_amount="The amount could not be read. Ask for the whole amount once.",
        already_promised="A promise is already recorded for this product; it cannot be changed in this call.",
    ).get(code, "The check did not work. Use the account facts to decide.")
    if code == "bad_date":
        out["suggest_date"] = dl
    # the customer named something else (no part of it) after the last suggestion: that suggestion was turned down
    if m.get("s") and not any(x in m["s"] for x in cur):
        m["r"] = (m["r"] + [m["s"]])[-6:]
    m["s"] = ""
    m["f"] = [] if ok else (m["f"] + [code])[-6:]
    n = m["f"].count(code)
    sug = " and ".join([x for x in (out["suggest_amount"] or out["amount"], iso(sd) or iso(d)) if x])
    M = len(prod.get("offers") or [])
    if n > 2:
        # N056: the same rejection over and over (a number split over two messages): stop the loop
        t = "Third time the same problem: stop asking; offer a callback if the rules allow one, else follow the refusal rules."
    elif n == 2 and code != "bad_date":
        t = "Same problem again: do not repeat your answer; ask ONE question for the whole amount and exact day together."
    elif not ok and sug and code not in ("bad_date", "bad_amount", "already_promised"):
        if sug in m["r"]:
            t += " But " + sug + " was already refused: do not offer it again; follow the refusal rules."
        elif M and len(m["o"]) >= M and sug not in m["o"]:
            t += " No offers left for this product: do not offer it; follow the refusal rules."
        else:
            m["o"] = m["o"] if sug in m["o"] else (m["o"] + [sug])[-6:]
            m["s"] = sug
            # the day to offer may be a planned attempt the agent made itself (it does not call this tool for those)
            rep = [str(o.get("n")) for o in prod.get("offers") or [] if sd and to_date(o.get("date")) == sd]
            if rep:
                t += " Attempt " + rep[0] + " in the account facts is on this day too: if it was already refused, do not offer it again."
    if not ok and m["o"] + m["r"]:
        t += (" Offered via this tool for " + pid + ": " + ("; ".join(m["o"]) or "none") + "; refused: " + ("; ".join(m["r"]) or "none")
              + ". Never repeat these. Offers left for this product: at most " + str(max(0, M - len(m["o"]))) + " minus your own.")
    out["what_to_do_next"] = t
    vset(vs, mk, json.dumps(m, separators=(",", ":")))
