# Pre-hook of create_promise: normalise product id, amount and date; stop a request that breaks the policy
# (amount outside floor..full, date not a business day, in the past or after the product's deadline, a second
# promise for the same product, any promise after a transfer, a further try after the allowed retries).
# Attempts are counted here: the post-hook does not run when the bank cannot be reached.
config = extra
body = body_of(config)
pf = nm("promise_product", "product_id")
af = nm("promise_amount", "amount")
dfld = nm("promise_date", "date")
cal = make_calendar(RT)
today = today_of(vs)
prods = products_of(vs)
prod = find_product(prods, body.get(pf))
if prod is not None:
    body[pf] = prod.get("id")
amt = to_number(body.get(af))
if amt is not None:
    body[af] = round(amt, 2)
dd = to_date(body.get(dfld)) or parse_spoken_date(body.get(dfld), today, nm("next_weekday", ""))
dd = fix_year(dd, today)
if dd is not None:
    body[dfld] = iso(dd)
pid = str(body.get(pf) or "")
vset(vs, "w_cp_pending", pid)
vset(vs, "w_cp_last", json.dumps(dict(amount=body.get(af), date=body.get(dfld))))
vset(vs, "w_cp_block", "")
pm = RT.get("promise") or dict()
retry = int(num(pm.get("retry_on_failure"), 1))
tries = int(num(vget(vs, "w_cp_tries_" + pid, 0)))
chk = dict(ok=True)
if nm("guard", "on") == "on" and prods:
    chk = check_promise(prod, amt, dd, today, cal, promised_of(vs), nm("next_weekday", ""))
    stop = [t for t in RT.get("account_triggers") or [] if t.get("action") in ("transfer", "close")
            and str(t.get("name")) in csv_list(vget(vs, "w_trigger_hits"))]
    if chk.get("ok") and stop:
        chk = dict(ok=False, code="account-rule", message="A rule applies to this account: " + str(stop[0].get("line")))
    if chk.get("ok") and transfers_of(vs):
        chk = dict(ok=False, code="after-transfer", message="The call was transferred. No promise may be recorded after a transfer.")
    if chk.get("ok") and tries > retry:
        chk = dict(ok=False, code="no-more-retries", message="create_promise already failed for this product as often as the rules allow. Do not try again.")
    if not chk.get("ok"):
        nxt = "Do not say it was recorded."
        if chk.get("suggest_date"):
            nxt += " The date you may offer instead is " + date_label(to_date(chk.get("suggest_date"))) + "."
        if chk.get("suggest_amount") is not None:
            nxt += " The amount you may offer instead is " + money(chk.get("suggest_amount")) + "."
        if chk.get("code") == "no-more-retries":
            nxt += " Tell the customer you could not record it, as the rules say."
            se = pair((RT.get("closing") or dict()).get("service_error"))
            if (RT.get("notes") or dict()).get("enabled") and pid:
                for k, v in ((RT.get("closing") or dict()).get("note_map") or dict()).items():
                    if pair(v) == se:
                        mark(vs, "w_intent_", pid, k)
        else:
            nxt += " Explain it in one sentence, follow the rules for this case, and call create_promise again only after a new read-back and a clear yes."
        chk["next"] = nxt
        block_request(config, vs, "w_cp_block", chk, "create_promise")
if chk.get("ok"):
    tries += 1
    vset(vs, "w_cp_tries_" + pid, str(tries))
    if tries > 1:
        vset(vs, "w_last_event", "Retry " + str(tries - 1) + " of create_promise for " + pid + " is being sent.")
    else:
        request_sent(vs)
