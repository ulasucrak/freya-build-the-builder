# Post-hook of transfer_to_agent.
result = extra
q = str(vget(vs, "w_transfer_pending") or vget(vs, nm("transfer_tool", "transfer_to_agent") + "_args." + nm("queue_field", "queue")) or "")
cl = RT.get("closing") or dict()
if call_ok(result, nm("transfer_ok", "ok")):
    if q:
        mark(vs, "w_transfer_q_", q, q)
    c = pair((cl.get("transfer") or dict()).get(q))
    t = "The transfer is done. Record nothing else"
    if (RT.get("notes") or dict()).get("forbidden_on_transfer"):
        t += " and add no notes"
    t += ". Tell the customer a colleague will take over. Then "
    if c["outcome"]:
        t += close_instruction(c["outcome"], c["reason"])
    else:
        t += "close the call as the rules say."
    set_next(result, t)
else:
    set_next(result, "The transfer did NOT work (" + api_error(result) + "). Do not say the customer was transferred. Call transfer_to_agent once more with the same queue.")
