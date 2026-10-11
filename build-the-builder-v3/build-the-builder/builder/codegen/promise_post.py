# Post-hook of create_promise (runs when the bank answered): honest result, retry rule, what comes next.
result = extra
pid = str(vget(vs, "w_cp_pending") or vget(vs, nm("promise_tool", "create_promise") + "_args." + nm("promise_product", "product_id")) or "")
block = load_json(vget(vs, "w_cp_block"), dict())
pm = RT.get("promise") or dict()
cl = RT.get("closing") or dict()
notes_on = (RT.get("notes") or dict()).get("enabled")
if isinstance(block, dict) and block.get("code"):
    vset(vs, "w_cp_block", "")
    set_action(vs, "Promise request for " + (pid or "this product") + " was stopped (nothing recorded)")
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
            t += (" All products are handled. Do not say goodbye yet: ask whether there is anything else and wait for the answer."
                  + " If the customer asks to change or cancel a recorded promise, answer as the rules say."
                  + " When there is nothing else, " + close_instruction(pair(cl.get("promise"))["outcome"], pair(cl.get("promise"))["reason"]))
    set_next(result, t)
    set_action(vs, "Promise recorded for " + (pid or "this product") + ("; open: " + ", ".join([str(p.get("id")) for p in rest]) if rest else "; all products handled"))
else:
    tries = int(num(vget(vs, "w_cp_tries_" + pid, 1)))
    retry = int(num(pm.get("retry_on_failure"), 1))
    if tries <= retry:
        set_next(result, "NOT recorded: the bank's service failed (" + api_error(result) + "). Do not say it was recorded and do not ask the customer again. Call create_promise again now with exactly the same values.")
        set_action(vs, "Promise for " + (pid or "this product") + " NOT recorded (service failed)", "call create_promise again with exactly the same values")
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
        else:
            c = pair(cl.get("promise")) if promised_of(vs) else se
            ca = close_instruction(c["outcome"], c["reason"])
            t += ("Then " if promised_of(vs) else "Tell the customer you could not record it. Then ") + ca
        set_next(result, t)
        set_action(vs, "Promise for " + (pid or "this product") + " could not be recorded (service failed twice)", "" if notes_on else ca.rstrip("."))
