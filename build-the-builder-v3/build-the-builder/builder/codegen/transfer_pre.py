# Pre-hook of transfer_to_agent: remember the queue for the closing logic.
config = extra
body = body_of(config)
vset(vs, "w_transfer_pending", str(body.get(nm("queue_field", "queue")) or ""))
request_sent(vs)
