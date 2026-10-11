# Pre-hook of the model's close_call. The model never closes the call itself: its close_call is a close REQUEST.
# The chosen outcome and reason are recorded, the request is stopped, and routing leaves the step for close_check
# (a code node). close_check runs after every tool call of the turn, on the merged variables, so a close never
# goes out before, or together with, another write; the engine sends close_call from close_api as the last call.
config = extra
body = body_of(config)
# in the wrap-up step the to-do list is binding: while notes are missing the close is refused (twice at most, so a
# model that never sends them cannot keep the call open; the turn cap closes it in the end)
held = int(num(vget(vs, "w_close_holds", 0)))
missing = missing_notes(vs, collect_facts(vs)) if int(num(vget(vs, "w_wrap_rounds", 0))) >= 1 and held < 2 else []
if missing:
    vset(vs, "w_close_holds", str(held + 1))
    block_request(config, vs, "w_close_block", dict(code="notes-missing", message="The call is not closed yet: these products have no recorded promise and no note: " + ", ".join(missing) + ".",
                  next="Call add_note for each of them now, one per message, with the reason that applies from the conversation. Then call close_call again."), "close_call")
else:
    vset(vs, "w_close_req_outcome", str(body.get(nm("close_outcome", "outcome")) or ""))
    r = str(body.get(nm("close_reason", "reason")) or "")
    vset(vs, "w_close_req_reason", "" if r.startswith("{") else r)
    vset(vs, "w_close_requested", "yes")
    block_request(config, vs, "w_close_block", dict(code="closing", message="The call is being closed by the system.",
                  next="Say nothing more unless you are asked to do something before the close."), "close_call")
