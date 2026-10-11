# Tool check_offer (code_exec, called by the model): checks a date and/or an amount the customer named for one
# product before the read-back, with the same check_promise as the create_promise guard, so the two never disagree.
# `extra` is the model's arguments (product_id, date, amount); the dict assigned to `out` is the tool result.
args = extra if isinstance(extra, dict) else dict()
cal = make_calendar(RT)
today = today_of(vs)
nw = nm("next_weekday", "")
prod = find_product(products_of(vs), args.get("product_id"))
said = str(args.get("date") or "").strip()
named = str(args.get("amount") or "").strip().lower()
# per-product memory (w_offers_<id>, one variable per product: parallel calls merge key by key): o offers this tool
# had the agent make, a the planned attempts among them, f the failures (code and input) since the last valid check,
# p the last bare number. The agent does not call this tool for its own offers.
mk = key_of("w_offers_", prod.get("id")) if prod else ""
m = load_json(vget(vs, mk), 0)
m = m if isinstance(m, dict) else dict()
for k in "oaf":
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
               or said_date(said, today, nw, cal)) or "?"
# words, both separator conventions, corrections: the last stated amount; 'the full amount' / 'the minimum' are the
# product's full amount / floor; otherwise the old reading (digits only), and nothing readable is not checked
if amt is None and named:
    amt = parse_amount(named)
    if amt is None and prod is not None:
        amt = prod.get("floor") if re.search(r"\b(min|minimum|floor|lowest|least)\b", named) else (
            prod.get("full") if re.search(r"\b(full|whole|all|everything|total)\b", named) else to_number(named))
out = dict(ok=False, code="unknown_product", period=per, what_to_do_next="Unknown product: use a product_id from the account facts.")
if prod is not None:
    pid = str(prod.get("id"))
    # a missing part is checked with a value that always passes, so only the part the customer named is judged
    chk = check_promise(prod, amt if amt is not None else prod.get("full"), said or prod.get("deadline"), today, cal, promised_of(vs), nw)
    code = chk.get("code") or "error"
    d = to_date(chk.get("date")) if said else None
    sd = to_date(chk.get("suggest_date"))
    a = money(amt) if amt is not None else None
    sa = money(chk.get("suggest_amount")) or None
    sg = date_label(sd) if sd else ""
    dl = date_label(to_date(prod.get("deadline")))
    ok = bool(chk.get("ok"))
    out.update(ok=ok, code=code, date=date_label(d) if d else "", amount=a, suggest_date=dl if code == "bad_date" else sg, suggest_amount=sa)
    t = ("Valid for " + pid + ": " + (" and ".join([x for x in (a, out["date"]) if x]) or "ok") + ". Use it in the read-back.") if ok else dict(
        bad_date=("A period" if per else "Not one exact day") + ": ask once for the exact day." + (" If they still name no day, propose the deadline, " + dl + ", with the amount discussed." if dl else ""),
        after_deadline="Too late: say the latest is " + sg + " and offer it.",
        not_business_day="Not a business day: say only that (never a holiday) and offer " + sg + ".",
        before_today="In the past: offer " + sg + ".",
        below_floor="Below the floor: say it cannot be recorded and offer " + str(sa) + ".",
        above_full="Above the full amount: offer " + str(sa) + ".",
        bad_amount="Ask for the whole amount once.",
        already_promised="Already promised for this product; it cannot be changed now.",
    ).get(code, UNC)
    # FX11: alternatives not decided yet ('200 or 300', 'Friday or Monday'): no fixed rule picks one (twice in a row).
    # Decided before the offer memory: an unclear input burns no attempt, logs no failure and records no offer
    if unclear(vs, "co" + pid, "amount" if two_values(named, parse_amount) else "date" if two_values(args.get("date"), lambda x: said_date(x, today, nw, cal)) else ""):
        out.update(ok=False, code="unclear", amount=None, date="")
        t = UNC
    else:
        # N056: the same input rejected again (a number split over two messages, read the same wrong way): ask once, then
        # stop the loop. Haggling (50, 80, 100: all below the floor) is new input each time and never counts
        key = code + ":" + " and ".join([x for x in (a, iso(d)) if x])
        m["f"] = [] if ok else (m["f"] + [key])[-6:]
        n = m["f"].count(key)
        sug = " and ".join([x for x in (sa or a, iso(sd) or iso(d)) if x])
        if n > 2:
            t = "Third time: stop asking; offer a callback if the rules allow one, else follow the refusal rules."
        elif n == 2 and code != "bad_date":
            t = "Same problem again: ask ONE question for the whole amount and exact day. " + t
        elif not ok and sug and code not in ("bad_date", "bad_amount", "already_promised"):
            # a suggestion that is a planned attempt (its day or amount) uses up that attempt; corrections do not
            pl = [str(o.get("n")) for o in prod.get("offers") or [] if (to_date(o.get("date")) == sd if sd else sa in (money(o.get("amount")), money(o.get("alt"))))]
            if sug in m["o"] or (pl and pl[0] in m["a"]):
                t += " Already offered via this tool: if refused, do not offer it again."
            elif pl:
                m["a"] += pl[:1]
                # the agent may have made that attempt itself (it does not call this tool for its own offers)
                t += " Attempt " + pl[0] + (" in the account facts is on this day too" if sd else "'s amount") + ": if already refused, do not offer it again."
            m["o"] = m["o"] if sug in m["o"] else (m["o"] + [sug])[-6:]
        if not ok and m["o"]:
            t += " Offered via this tool: " + "; ".join(m["o"]) + ". Offers left for this product: at most " + str(max(0, len(prod.get("offers") or []) - len(m["a"]))) + " minus your own."
        vset(vs, mk, json.dumps(m, separators=(",", ":")))
    out["what_to_do_next"] = t
