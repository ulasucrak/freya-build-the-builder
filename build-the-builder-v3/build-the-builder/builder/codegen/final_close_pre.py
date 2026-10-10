# Pre-hook of the end-of-call safety net: send close_call only when no close_call went out during the call and
# the facts decide the outcome. Otherwise the request is stopped.
config = extra
body = body_of(config)
done = str(vget(vs, "w_close_done")) == "yes"
for k in (nm("close_tool", "close_call") + "_success_count", nm("close_node", "close_api") + "_success_count"):
    if num(vget(vs, k, 0)) > 0:
        done = True
res = compute_closing(collect_facts(vs), RT) if not done else dict()
if done or not res.get("outcome") or res.get("source") in ("llm", ""):
    block_request(config, vs, "w_final_block", dict(code="skipped"))
else:
    body[nm("close_outcome", "outcome")] = res.get("outcome")
    if res.get("reason"):
        body[nm("close_reason", "reason")] = res.get("reason")
    else:
        body.pop(nm("close_reason", "reason"), None)
