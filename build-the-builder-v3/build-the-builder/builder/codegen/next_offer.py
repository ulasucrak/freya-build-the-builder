# Tool next_offer (code_exec, called by the model before every persuasion proposal): counts the proposals per
# product (one variable per product, parallel-safe) and returns the exact next offer from the computed facts, or
# that no attempt is left. The model may not improvise offers, so the band's attempt limit holds in code.
args = extra if isinstance(extra, dict) else dict()
prods = products_of(vs)
prod = find_product(prods, args.get("product_id"))
said = str(args.get("customer_said") or "").lower()
if prod is None:
    return dict(ok=False, what_to_do_next="Unknown product id. Use one of: " + ", ".join([str(p.get("id")) for p in prods]) + ".")
pid = str(prod.get("id"))
offers = prod.get("offers") or []
used = int(num(vget(vs, key_of("w_att_", pid), 0)))
if "much" in said:
    mark(vs, "w_toomuch_", pid, "yes")
if used >= len(offers):
    return dict(ok=True, product_id=pid, attempts_used=used, attempts_allowed=len(offers), offer="none",
                what_to_do_next="No persuasion attempt is left for " + pid + " (" + str(len(offers)) + " allowed, all used). "
                "Make no further proposal for it. Unless the customer accepts a proposal already made, treat it as a refusal, as the rules say, and go on with the next product or the closing.")
o = offers[used]
amt = o.get("amount")
if str(vget(vs, key_of("w_toomuch_", pid))) == "yes" and o.get("alt") is not None and amt == prod.get("full"):
    amt = o.get("alt")
used += 1
mark(vs, "w_att_", pid, str(used))
t = ("Attempt " + str(used) + " of " + str(len(offers)) + " for " + pid + ": propose " + money(amt) + " on "
     + date_label(to_date(o.get("date"))) + ", following that attempt's rule in the account facts.")
if used >= len(offers):
    t += " This is the last attempt for this product: if the customer refuses it, make no further proposal."
return dict(ok=True, product_id=pid, attempt=used, attempts_allowed=len(offers), amount=amt, date=o.get("date"), what_to_do_next=t)
