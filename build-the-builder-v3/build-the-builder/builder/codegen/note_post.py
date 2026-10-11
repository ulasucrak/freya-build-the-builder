# Post-hook of add_note (runs when the bank answered).
result = extra
pend = str(vget(vs, "w_note_pending") or "|").split("|")
pid = pend[0] if pend else ""
reason = pend[1] if len(pend) > 1 else ""
block = load_json(vget(vs, "w_note_block"), dict())
if isinstance(block, dict) and block.get("code"):
    vset(vs, "w_note_block", "")
    set_next(result, "Not sent: " + str(block.get("message")) + " " + str(block.get("next") or ""))
    set_action(vs, "Note request was stopped (nothing recorded)")
elif call_ok(result, nm("note_ok", "ok")):
    if pid:
        mark(vs, "w_note_", pid, reason or "noted")
    ni = notes_instruction(vs)
    if ni:
        set_next(result, "Note sent for " + pid + ". " + ni)
    else:
        res = compute_closing(collect_facts(vs), RT)
        t = "Note sent for " + pid + ". All notes are sent. Then "
        if res.get("outcome"):
            t += close_instruction(res.get("outcome"), res.get("reason"))
        else:
            t += "close the call as the rules say."
        set_next(result, t)
    set_action(vs, "Note sent for " + (pid or "this product"))
else:
    set_next(result, "The note was NOT sent (" + api_error(result) + "). Call add_note once more with the same values.")
    set_action(vs, "Note for " + (pid or "this product") + " NOT sent (service failed)", "call add_note once more with the same values")
