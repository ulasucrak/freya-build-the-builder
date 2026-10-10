# Pre-hook of add_note. Notes are deferred by the prompt: the model decides a reason per product as it goes and
# sends all notes at the end, just before close_call, and only when no transfer happened. This hook stops a note
# after a transfer, for an unknown product, for a product with a promise, and a second note for the same product.
# (No "hold until every product is handled" here: parallel tool calls see a stale variable snapshot.)
config = extra
body = body_of(config)
pf = nm("note_product", "product_id")
rf = nm("note_reason", "reason")
prods = products_of(vs)
prod = find_product(prods, body.get(pf))
if prod is not None:
    body[pf] = prod.get("id")
pid = str(body.get(pf) or "")
reason = str(body.get(rf) or "")
vset(vs, "w_note_pending", pid + "|" + reason)
vset(vs, "w_note_block", "")
info = None
if prods and not notes_apply(vs):
    info = dict(code="notes-not-used", message="Notes are not used for this customer.", next="Do not call add_note in this call.")
elif (RT.get("notes") or dict()).get("forbidden_on_transfer") and transfers_of(vs):
    info = dict(code="after-transfer", message="The call was transferred: no notes may be added.", next="Do not call add_note again.")
elif prods and prod is None:
    info = dict(code="unknown-product", message="Unknown product id.", next="Use a product id exactly as listed in the account facts.")
elif pid in promised_of(vs):
    info = dict(code="has-promise", message="This product has a recorded promise; it needs no note.", next="Continue with the call.")
elif pid in notes_of(vs):
    info = dict(code="already-noted", message="This product already has a note.", next="Continue with the call.")
if info is not None and nm("guard", "on") == "on":
    block_request(config, vs, "w_note_block", info, "add_note")
else:
    request_sent(vs)
