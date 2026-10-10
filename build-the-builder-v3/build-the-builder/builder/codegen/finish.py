# Code node before the engine sends close_call: outcome and reason from the recorded facts where they decide it,
# otherwise the model's requested outcome.
facts = collect_facts(vs)
facts["llm"] = dict(outcome=vget(vs, "w_close_req_outcome"), reason=vget(vs, "w_close_req_reason"))
res = compute_closing(facts, RT)
if not res.get("outcome"):
    st = id_state_of(vs)
    c = pair((RT.get("closing") or dict()).get("refused")) if st == "verified" else dict(outcome="", reason="")
    res = dict(outcome=c["outcome"], reason=c["reason"])
vset(vs, "w_close_outcome", str(res.get("outcome") or ""))
vset(vs, "w_close_reason", str(res.get("reason") or ""))
vset(vs, "w_close_done", "yes")
