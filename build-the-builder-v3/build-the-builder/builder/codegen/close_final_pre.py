# Pre-hook of the engine's close_call (close_api): no empty or unresolved reason field.
config = extra
body = body_of(config)
rf = nm("close_reason", "reason")
r = str(body.get(rf) or "").strip()
if not r or r.startswith("{"):
    body.pop(rf, None)
of = nm("close_outcome", "outcome")
if not str(body.get(of) or "").strip() and vget(vs, "w_close_outcome"):
    body[of] = vget(vs, "w_close_outcome")
request_sent(vs)
