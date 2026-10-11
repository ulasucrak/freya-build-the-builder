# Pre-hook of the end-of-call safety net: send close_call only when no close_call went through during the call.
# The engine's close (finish -> close_api, w_close_sent) counts unless the bank's answer says it failed: a failure
# count beyond the model's close requests (all stopped, counted in w_close_reqs) or an answer that is not ok. Then
# the safety net sends it again with the facts' outcome, else the one finish chose. Otherwise the request is stopped.
config = extra
body = body_of(config)
tool = nm("close_tool", "close_call")
n_ok = num(vget(vs, tool + "_success_count", 0))
bad = str(vget(vs, "w_close_sent")) == "yes" and (num(vget(vs, tool + "_failure_count", 0)) > num(vget(vs, "w_close_reqs", 0))
                                                  or (n_ok > 0 and not var_ok(vs, tool, nm("close_ok", "ok"))))
done = not bad and (str(vget(vs, "w_close_done")) == "yes" or n_ok > 0 or num(vget(vs, nm("close_node", "close_api") + "_success_count", 0)) > 0)
res = compute_closing(collect_facts(vs), RT) if not done else dict()
if bad and (not res.get("outcome") or res.get("source") in ("llm", "")):
    res = dict(outcome=vget(vs, "w_close_outcome"), reason=vget(vs, "w_close_reason"), source="finish")
if done or not res.get("outcome") or res.get("source") in ("llm", ""):
    block_request(config, vs, "w_final_block", dict(code="skipped"))
else:
    body[nm("close_outcome", "outcome")] = res.get("outcome")
    if res.get("reason"):
        body[nm("close_reason", "reason")] = res.get("reason")
    else:
        body.pop(nm("close_reason", "reason"), None)
