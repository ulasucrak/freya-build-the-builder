# Pre-hook of create_promise: normalise product id, amount and date; stop a request that breaks the policy
# (amount outside floor..full, date not a business day, in the past or after the product's deadline, a second
# promise for the same product, any promise after a transfer or for a disputed product, a further try after the
# allowed retries).
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
# ISO first, then spoken forms
dd = parse_spoken_date(body.get(dfld), today, nm("next_weekday", ""))
dd = fix_year(dd, today)
if dd is not None:
    body[dfld] = iso(dd)
pid = str(body.get(pf) or "")
vset(vs, "w_cp_pending", pid)
vset(vs, "w_cp_last", json.dumps(dict(amount=body.get(af), date=body.get(dfld))))
vset(vs, "w_cp_block", "")
pm = RT.get("promise") or dict()
retry = int(num(pm.get("retry_on_failure"), 1))
tries = cnt(vs, "w_cp_tries_" + pid)
chk = dict(ok=True)
g = nm("guard", "on") == "on"
if g and str(vget(vs, "w_account_state")) == "failed":
    # the account could not be loaded (prepare_account wrote 'failed'): nothing to check a promise against
    chk = dict(ok=False, code="account-failed", message="The account could not be loaded: no promise can be recorded.")
elif g and prods:
    chk = check_promise(prod, amt, dd, today, cal, promised_of(vs), nm("next_weekday", ""))
    if chk.get("ok") and transfers_of(vs):
        chk = dict(ok=False, code="after-transfer", message="No promise may be recorded after a transfer.")
    if chk.get("ok") and str(vget(vs, key_of("w_dispute_", pid))) == "yes":
        chk = dict(ok=False, code="disputed", message="This product is disputed: no promise may be recorded for it.")
    if chk.get("ok") and tries > retry:
        chk = dict(ok=False, code="no-more-retries", message="create_promise failed for this product as often as the rules allow.")
    # FX11: a missing or unreadable product, amount or date is no rule violation (twice in a row, then the strict text)
    if unclear(vs, "cp" + pid, dict(unknown_product="product", bad_date="date", bad_amount="amount" if amt is None else "").get(chk.get("code"), "")):
        chk = dict(ok=False, code="unclear", message="Product, amount or date unclear.", next=UNC)
if not chk.get("ok") and chk.get("code") != "unclear":
    c = chk.get("code")
    nxt = "Do not say it was recorded."
    if chk.get("suggest_date"):
        nxt += " The date you may offer instead is " + date_label(to_date(chk.get("suggest_date"))) + "."
    if chk.get("suggest_amount") is not None:
        nxt += " The amount you may offer instead is " + money(chk.get("suggest_amount")) + "."
    if c == "account-failed":
        nxt += " " + SVC
    elif c == "no-more-retries":
        nxt += " Do not try again; tell the customer you could not record it, as the rules say."
        sn = (RT.get("enforce") or dict()).get("service_note")
        if (RT.get("notes") or dict()).get("enabled") and pid and sn:
            mark(vs, "w_intent_", pid, sn)
    elif c == "disputed":
        nxt += " Do not argue and take no promise for it; follow the rules for a dispute."
    else:
        nxt += " Explain it in one sentence, follow the rules for this case, and call create_promise again only after a new read-back and a clear yes."
    chk["next"] = nxt
if not chk.get("ok"):
    block_request(config, vs, "w_cp_block", chk, "create_promise")
if chk.get("ok"):
    tries += 1
    vset(vs, "w_cp_tries_" + pid, str(tries))
    if pid:
        # per product, '1' = in flight: survives parallel calls (see sent_ok / in_flight)
        mark(vs, "w_cp_sent_", pid, "1")
    if tries > 1:
        vset(vs, "w_last_event", "Retry " + str(tries - 1) + " of create_promise for " + pid + " is being sent.")
    else:
        request_sent(vs)
