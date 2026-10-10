# Post-hook of create_promise (runs when the bank answered): honest result, retry rule, what comes next.
result = extra
pid = str(vget(vs, "w_cp_pending") or vget(vs, nm("promise_tool", "create_promise") + "_args." + nm("promise_product", "product_id")) or "")
block = load_json(vget(vs, "w_cp_block"), dict())
pm = RT.get("promise") or dict()
cl = RT.get("closing") or dict()
notes_on = (RT.get("notes") or dict()).get("enabled")
if isinstance(block, dict) and block.get("code"):
    vset(vs, "w_cp_block", "")
    set_next(result, "NOT recorded. The request was stopped before it reached the bank: " + str(block.get("message") or block.get("code")) + " " + str(block.get("next") or ""))
elif call_ok(result, nm("promise_ok", "ok")):
    if pid:
        mark(vs, "w_promised_", pid, "yes")
    vset(vs, "w_cp_tries_" + pid, "0")
    request_sent(vs)
    rest = remaining_products(vs)
    args = load_json(vget(vs, "w_cp_last"), dict())
    t = "Recorded: the promise for " + (pid or "this product") + " is saved"
    if args.get("date"):
        t += " (" + money(args.get("amount")) + " on " + date_label(to_date(args.get("date"))) + "; use this weekday and date when you speak)"
    t += "."
    if pm.get("success_say"):
        t += " Tell the customer it is recorded and say: \"" + str(pm.get("success_say")) + "\""
    if rest:
        t += " Products still open: " + ", ".join([str(p.get("id")) + " (" + str(p.get("name")) + ")" for p in rest]) + ". If the customer already confirmed a promise for one of them, call create_promise for it now; otherwise continue with the next open product."
    else:
        ni = notes_instruction(vs)
        if ni:
            t += " All products are handled. " + ni
        else:
            t += " All products are handled. Then " + close_instruction(pair(cl.get("promise"))["outcome"], pair(cl.get("promise"))["reason"])
    set_next(result, t)
else:
    tries = int(num(vget(vs, "w_cp_tries_" + pid, 1)))
    retry = int(num(pm.get("retry_on_failure"), 1))
    if tries <= retry:
        set_next(result, "NOT recorded: the bank's service failed (" + api_error(result) + "). Do not say it was recorded and do not ask the customer again. Call create_promise again now with exactly the same values.")
    else:
        t = "NOT recorded: the service failed again, so this promise cannot be recorded. Do not say it was recorded. "
        if pm.get("failure_say"):
            t += "Rule: " + str(pm.get("failure_say")) + ". "
        se = pair(cl.get("service_error"))
        if notes_on:
            nr = ""
            for k, v in (cl.get("note_map") or dict()).items():
                if pair(v) == se:
                    nr = k
            if nr and pid:
                mark(vs, "w_intent_", pid, nr)
            t += "Keep the promises that were recorded and continue with any other open product. The note for this product is sent at the end with the others."
        elif promised_of(vs):
            t += "Then " + close_instruction(pair(cl.get("promise"))["outcome"], pair(cl.get("promise"))["reason"])
        else:
            t += "Tell the customer you could not record it. Then " + close_instruction(se["outcome"], se["reason"])
        set_next(result, t)
