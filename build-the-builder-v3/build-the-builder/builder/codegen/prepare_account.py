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
vset(vs, "w_account_facts", account_facts_text(prods, cal, today))
# the hooks need only these fields; the offer texts are in w_account_facts already (keeps the variable table small)
slim = []
for p in prods:
    slim.append(dict(index=p.get("index"), id=p.get("id"), type=p.get("type"), name=p.get("name"), full=p.get("full"),
                     floor=p.get("floor"), deadline=p.get("deadline"), band=p.get("band"),
                     offers=[dict(n=o.get("n"), amount=o.get("amount"), date=o.get("date")) for o in (p.get("offers") or [])]))
vset(vs, "w_products_json", json.dumps(slim, separators=(",", ":")))
vset(vs, "w_product_count", str(len(prods)))
vset(vs, "w_product_ids", ",".join([str(p.get("id")) for p in prods]))
vset(vs, "w_account_state", "ok" if prods else "failed")
vset(vs, "w_notes_rule", ("Decide a note reason for each product without a promise as you go, but do not call add_note yet. "
     "Only at the end, when every product has a promise or a reason, or when the call ends early (a refusal, a customer who will not talk), "
     "call add_note once per such product, one per message, before you say goodbye; then say goodbye and close.")
     if notes_apply(vs) else "Notes are not used for this customer: do not call add_note.")
