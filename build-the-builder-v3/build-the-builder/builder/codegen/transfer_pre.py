# Pre-hook of transfer_to_agent: the queue must be one the closing table knows (case-insensitive), a call is
# transferred once, and after two failed transfers no further try goes out. Remembers the queue for the closing logic.
config = extra
body = body_of(config)
qf = nm("queue_field", "queue")
tool = nm("transfer_tool", "transfer_to_agent")
qs = [str(x) for x in ((RT.get("closing") or dict()).get("transfer") or dict())]
q = str(body.get(qf) or "").strip()
for x in qs:
    if x.lower() == q.lower().replace(" ", "_"):
        q = body[qf] = x
info = None
if transfers_of(vs):
    info = dict(code="already-transferred", message="The call is already transferred.", next="Do not call transfer_to_agent again. Close the call as the rules say.")
elif qs and q not in qs:
    info = dict(code="unknown-queue", message="'" + q + "' is not a queue.", next="Use the queue the rules give: one of " + ", ".join(qs) + ".")
elif fails(vs, tool) >= 2:
    info = dict(code="service-down", message="transfer_to_agent already failed twice.", next=SVC)
if info is not None and nm("guard", "on") == "on":
    block_request(config, vs, "w_tr_block", info, "transfer_to_agent")
else:
    vset(vs, "w_transfer_pending", q)
    request_sent(vs)
