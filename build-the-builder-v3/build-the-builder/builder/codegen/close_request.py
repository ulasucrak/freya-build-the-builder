# Pre-hook of the model's close_call. The model never closes the call itself: its close_call is a close REQUEST.
# The chosen outcome and reason are recorded, the request is stopped, and routing leaves the step for close_check
# (a code node). close_check runs after every tool call of the turn, on the merged variables, so a close never
# goes out before, or together with, another write; the engine sends close_call from close_api as the last call.
config = extra
body = body_of(config)
vset(vs, "w_close_req_outcome", str(body.get(nm("close_outcome", "outcome")) or ""))
r = str(body.get(nm("close_reason", "reason")) or "")
vset(vs, "w_close_req_reason", "" if r.startswith("{") else r)
vset(vs, "w_close_requested", "yes")
block_request(config, vs, "w_close_block", dict(code="closing", message="The call is being closed by the system.",
              next="Say nothing more unless you are asked to do something before the close."), "close_call")
