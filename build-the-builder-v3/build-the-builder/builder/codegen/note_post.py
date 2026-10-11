# Post-hook of add_note (runs when the bank answered).
result = extra
pend = str(vget(vs, "w_note_pending") or "|").split("|")
pid = pend[0] if pend else ""
reason = pend[1] if len(pend) > 1 else ""
# parallel calls: w_note_pending is the last pre-hook's write; credit here only the one note in flight
fl = in_flight(vs, "w_note_sent_", "w_note_")
if fl and fl != [pid]:
    pid = fl[0] if len(fl) == 1 else ""
    reason = str(vget(vs, key_of("w_note_sent_", pid))) if pid else ""
tool = nm("note_tool", "add_note")
ok = call_ok(result, nm("note_ok", "ok"))
kind = "" if ok else err_kind(result)
block = load_json(vget(vs, "w_note_block"), dict())
if isinstance(block, dict) and block.get("code"):
    vset(vs, "w_note_block", "")
    set_next(result, "Not sent: " + str(block.get("message")) + " " + str(block.get("next") or ""))
    set_action(vs, "Note request was stopped (nothing recorded)")
elif ok or kind == "done":
    # 'done': the bank already has this note
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
elif fails(vs, tool, 2 if kind else 1) >= 2:
    set_next(result, "The note was NOT sent (" + api_error(result) + ") and add_note has failed twice. " + svc_close())
    set_action(vs, "add_note failed twice", "close the call as the rules say for a service error")
else:
    if pid:
        mark(vs, "w_note_sent_", pid, "0")
    set_next(result, "The note was NOT sent (" + api_error(result) + "). Call add_note once more with the same values.")
    set_action(vs, "Note for " + (pid or "this product") + " NOT sent (service failed)", "call add_note once more with the same values")
