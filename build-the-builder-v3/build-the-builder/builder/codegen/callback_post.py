# Post-hook of schedule_callback (runs when the bank answered).
result = extra
cl = RT.get("closing") or dict()
block = load_json(vget(vs, "w_cb_block"), dict())
if isinstance(block, dict) and block.get("code"):
    vset(vs, "w_cb_block", "")
    set_next(result, "NOT scheduled: " + str(block.get("message")) + " " + str(block.get("next") or ""))
    set_action(vs, "Callback request was stopped (nothing scheduled)", str(block.get("next") or "ask for an allowed day and time and call schedule_callback").rstrip(". "))
elif call_ok(result, nm("callback_ok", "ok")):
    vset(vs, "w_callback_done", "yes")
    c = pair(cl.get("callback"))
    set_next(result, "The callback is scheduled. Tell the person when the customer will be called, without saying why. Then " + close_instruction(c["outcome"], c["reason"]))
    set_action(vs)
else:
    set_next(result, "NOT scheduled: the service failed (" + api_error(result) + "). Do not say it was scheduled. Call schedule_callback once more with the same date and time.")
    set_action(vs, "Callback NOT scheduled (service failed)", "call schedule_callback once more with the same date and time")
