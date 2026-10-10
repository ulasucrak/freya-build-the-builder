# Post-hook of close_call.
result = extra
if call_ok(result, nm("close_ok", "ok")):
    vset(vs, "w_close_done", "yes")
    set_next(result, "The call is closed. Say nothing more.")
else:
    vset(vs, "w_close_done", "no")
    set_next(result, "close_call did not go through (" + api_error(result) + "). Call close_call once more with the same values.")
