# Code node entered from negotiate when the per-turn extraction labelled the customer's last message (x_reply):
# refusals, 'too much', 'does not want to talk' and disputes are counted per product here, so the offer ladder, the
# N-th 'do not want to talk' and a disputed product never depend on the dialogue model's memory. Writes the next
# action (shown in the prompt and in the re-entry instruction), or sends the call to the close.
# Per product: w_offered_<pid> = the last attempt made (0: only the first payment question so far), w_refusals_<pid>
# = refusals counted. A product the call moves on to gets its attempt 1 at once (w_offered 1), so its first no is
# refusal 1 and leads to attempt 2.
ef = RT.get("enforce") or dict()
said = str(vget(vs, "x_reply") or "").strip().lower()
vset(vs, "x_reply", "none")
pname = str(vget(vs, "x_product") or "")
vset(vs, "x_product", "")
vset(vs, "w_turn_route", "stay")
# a re-entry resets negotiate's own turn counter: the turns are summed here so the turn cap still holds
turns = int(num(vget(vs, "w_neg_turns", 0))) + max(1, int(num(vget(vs, "negotiate.current_turns", 0))))
vset(vs, "w_neg_turns", str(turns))
prods = products_of(vs)
openp = remaining_products(vs)
promised = promised_of(vs)
target = match_product(openp, pname) or (openp[0] if openp else None)
state, act = "", ""
end = None


def offer_of(p, n):
    """'<amount> on <day>' of attempt n of product p ('' when it has none)."""
    offers = p.get("offers") or []
    if n < 1 or n > len(offers):
        return ""
    o = offers[n - 1]
    amt = o.get("alt") if str(vget(vs, key_of("w_too_much_", p.get("id")))) == "yes" and o.get("alt") is not None else o.get("amount")
    return (money(amt) if amt is not None else "the amount in the account facts") + (
        " on " + date_label(to_date(o.get("date"))) if to_date(o.get("date")) else "")


def move_on():
    """The next open product: its attempt 1 is made now (the first payment question was already asked)."""
    rest = remaining_products(vs)
    if not rest:
        return None
    nx = rest[0]
    m = len(nx.get("offers") or [])
    if m and int(num(vget(vs, key_of("w_offered_", nx.get("id")), 0))) < 1:
        mark(vs, "w_offered_", nx.get("id"), "1")
        return ("continue with " + str(nx.get("id")) + " (" + str(nx.get("name")) + "): make its attempt 1 of " + str(m)
                + " only: " + offer_of(nx, 1))
    return "continue with " + str(nx.get("id")) + " (" + str(nx.get("name")) + ") as the rules say"


if said == "dispute" and ef.get("dispute_on"):
    # the product the customer named (one with a recorded promise keeps it); unnamed: the only open one, else ask
    dp = match_product(prods, pname)
    if dp is None and len(prods) == 1:
        dp = prods[0]
    elif dp is None and len(openp) == 1:
        dp = openp[0]
    if dp is None and len(openp) > 1:
        state = "The customer disputes a product but it is not clear which one"
        act = "ask which product they mean; take no promise for it and do not argue"
    elif dp is not None and dp.get("id") not in promised:
        pid = str(dp.get("id"))
        mark(vs, "w_dispute_", pid, "yes")
        mark(vs, "w_intent_", pid, ef.get("dispute_note") or pair(ef.get("dispute"))["reason"] or "dispute")
        state = "The customer disputes " + pid + " (" + str(dp.get("name")) + "): take no promise for it and do not argue"
        act = move_on()
        if act is None:
            d = pair(ef.get("dispute"))
            act = close_instruction(d["outcome"], d["reason"]).rstrip(".") if d["outcome"] else "close the call as the rules say"
elif said in ("refusal", "no_talk", "too_much") and target is not None:
    pid = str(target.get("id"))
    m = len(target.get("offers") or [])
    full = to_number(target.get("full"))
    floor = to_number(target.get("floor"))
    if said == "too_much" and floor is not None and full is not None and floor < full - 0.004 \
            and str(vget(vs, key_of("w_too_much_", pid))) != "yes":
        mark(vs, "w_too_much_", pid, "yes")
        state = "The customer says the amount for " + pid + " is too much"
        act = "offer the floor, " + money(floor) + ", once, for the day being discussed, as the rules say; a no to it is a refusal"
    else:
        r = int(num(vget(vs, key_of("w_refusals_", pid), 0))) + 1
        mark(vs, "w_refusals_", pid, str(r))
        nxt = int(num(vget(vs, key_of("w_offered_", pid), 0))) + 1
        if nxt <= m:
            mark(vs, "w_offered_", pid, str(nxt))
            state = "Refusal " + str(r) + " for " + pid + " (" + str(target.get("name")) + ")"
            act = "make attempt " + str(nxt) + " of " + str(m) + " only: " + offer_of(target, nxt) + "; never repeat an offer the customer refused"
        else:
            mark(vs, "w_intent_", pid, ef.get("refused_note") or pair(ef.get("refused"))["reason"] or "refused")
            state = "All " + str(m) + " offer(s) for " + pid + " were refused: make no other offer for it"
            act = move_on()
            if act is None and ef.get("close_on_ladder_end"):
                end = pair(ef.get("refused"))
            elif act is None:
                act = "close the call as the rules say"
if said == "no_talk" and end is None:
    n = int(num(vget(vs, "w_no_talk", 0))) + 1
    vset(vs, "w_no_talk", str(n))
    lim = int(num(ef.get("talk_count"), 0))
    if lim and n >= lim:
        for p in remaining_products(vs):
            mark(vs, "w_intent_", p.get("id"), ef.get("talk_note") or pair(ef.get("talk"))["reason"] or "refused")
        end = pair(ef.get("talk"))
    elif lim:
        state = "The customer said " + str(n) + " time(s) that they do not want to talk (the call ends at " + str(lim) + ")"
        act = "acknowledge it in one sentence" + ("; then " + act if act else "")
if end is None and turns >= int(num(ef.get("max_turns"), 0)) > 0:
    vset(vs, "w_turn_route", "cap")
elif end is not None:
    vset(vs, "w_close_req_outcome", end["outcome"])
    vset(vs, "w_close_req_reason", end["reason"])
    vset(vs, "w_end_say", "yes")
    vset(vs, "w_turn_route", "close")
    vset(vs, "w_next_action", "The call is being closed - next: say nothing more.")
elif act:
    vset(vs, "w_next_action", state + " - next: " + act + ".")
else:
    set_action(vs)
vset(vs, "w_enter_prompt", "Reply to the customer's last message now, in one to three short sentences. State and next step: "
     + str(vget(vs, "w_next_action") or "continue as the rules say."))
