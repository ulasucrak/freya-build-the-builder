# Post-hook of close_call (not wired: close_api has no post-hook; final_close_pre reads close_call_response).
result = extra
if call_ok(result, nm("close_ok", "ok")):
    vset(vs, "w_close_done", "yes")
    set_next(result, "The call is closed. Say nothing more.")
    set_action(vs)
else:
    vset(vs, "w_close_done", "no")
    set_next(result, "close_call did not go through (" + api_error(result) + "). Call close_call once more with the same values.")
    set_action(vs, "close_call did not go through", "call close_call once more with the same values")
