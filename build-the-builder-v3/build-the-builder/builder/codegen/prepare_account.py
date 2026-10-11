# Code node after get_account: per product band, deadline, floor, full amount and offers, as facts for the prompt
# and as JSON for the hooks.
cal = make_calendar(RT)
today = today_of(vs)
raw = products_from_vars(vs, ["w_account_products", nm("account_node", "load_account") + "_products"],
                         nm("list_field", "products"))
prods = []
for i, p in enumerate(raw):
    b = build_product(p, i, RT, today, cal)
    b.pop("raw", None)
    prods.append(b)
vset(vs, "w_account_facts", account_facts_text(prods, cal, today, "refusal" in ((RT.get("enforce") or dict()).get("signals") or [])))
# negotiate's entry instruction: the first entry states the debt; a re-entry from turn_state answers the last message
vset(vs, "w_enter_prompt", str(RT.get("enter") or "State the debt as the rules require, using the account facts."))
# the hooks need only these fields; the offer texts are in w_account_facts already (keeps the variable table small)
slim = []
for p in prods:
    slim.append(dict(index=p.get("index"), id=p.get("id"), type=p.get("type"), name=p.get("name"), full=p.get("full"),
                     floor=p.get("floor"), deadline=p.get("deadline"), band=p.get("band"),
                     offers=[dict(n=o.get("n"), amount=o.get("amount"), alt=o.get("alt_amount"), date=o.get("date")) for o in (p.get("offers") or [])]))
vset(vs, "w_products_json", json.dumps(slim, separators=(",", ":")))
vset(vs, "w_product_count", str(len(prods)))
vset(vs, "w_product_ids", ",".join([str(p.get("id")) for p in prods]))
vset(vs, "w_account_state", "ok" if prods else "failed")
vset(vs, "w_notes_rule", ("Decide a note reason for each product without a promise as you go, but do not call add_note yet. "
     "Only at the end, when every product has a promise or a reason, or when the call ends early (a refusal, a customer who will not talk), "
     "call add_note once per such product, one per message, before you say goodbye; then say goodbye and close.")
     if notes_apply(vs) else "Notes are not used for this customer: do not call add_note.")
# negotiate's first message, when the policy fixes it (RT "entry"): one sentence per product, then the first payment
# question; spoken as a fixed line (no model call). Only from the account data; never a placeholder.
E = RT.get("entry") or dict()
line = ""
hit = ""
if E:
    if prods:
        parts = [str(E.get("intro") or "")]
        for p in prods:
            nm_ = str(p.get("name") or "").strip()
            # a template field the account lacks ('credit card ending {card_last4}'): drop the dangling words
            while re.search(r"\s(ending|in|with|at|of|for|number|no\.?|#)$", nm_, re.I):
                nm_ = re.sub(r"\s+\S+$", "", nm_)
            nm_ = nm_ or ("product " + str(p.get("id") or p.get("index")))
            nm_ = nm_ if nm_.lower().startswith("your ") else "your " + nm_
            d = p.get("days_late")
            a = money(p.get("full"))
            a = a[:-3] if a.endswith(".00") else a
            s = nm_[0].upper() + nm_[1:]
            if d is not None:
                s += " is " + str(d) + (" day " if d == 1 else " days ") + str(E.get("late") or "late")
            if a:
                s += (", and the " if d is not None else ": the ") + str(E.get("noun") or "past-due amount") + " is " + a
            if d is None and not a:
                s += " has a late payment"
            parts.append(s + ".")
        # a data-decided trigger that matches (e.g. total past due over a limit -> transfer): no payment question
        for c in E.get("conds") or []:
            vals = [to_number(r.get(c.get("field"))) for r in raw if isinstance(r, dict)]
            vals = [v for v in vals if v is not None]
            if c.get("over") == "all_products_sum":
                vals = [sum(vals)] if vals else []
            ok = [v for v in vals if {">": v > c["value"], ">=": v >= c["value"], "<": v < c["value"], "<=": v <= c["value"],
                                      "==": v == c["value"], "!=": v != c["value"]}.get(c.get("op"))]
            if ok and (c.get("over") not in ("each_product", "all_products") or len(ok) == len(vals)):
                hit = hit or str(c.get("name"))
        if not hit:
            parts.append(str((E.get("many") if len(prods) > 1 else E.get("one")) or ""))
        line = " ".join([x for x in parts if x])
    else:
        line = "Thank you. I am sorry, I cannot see your account details at the moment."
    line = re.sub(r"[{}]", "", line)
vset(vs, "w_entry_line", line)
ids = ", ".join([str(p.get("id")) for p in prods])
if prods and line:
    vset(vs, "w_next_action", "Verified; " + str(len(prods)) + " product(s): " + ids + ". Debt already said; "
         + (("rule '" + hit + "' applies: follow it now.") if hit else "first question asked: answer the reply."))
elif prods:
    vset(vs, "w_next_action", "Verified; " + str(len(prods)) + " product(s): " + ids + ". Next: state the debt, then handle each product.")
else:
    vset(vs, "w_next_action", "")
