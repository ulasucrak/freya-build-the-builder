# Code node reached when a step ran out of turns: compute the outcome from the facts for the engine-run close.
res = compute_closing(collect_facts(vs), RT)
if not res.get("outcome"):
    st = id_state_of(vs)
    c = pair((RT.get("closing") or dict()).get("refused" if st == "verified" else "fallback_unverified"))
    res = dict(outcome=c["outcome"], reason=c["reason"])
vset(vs, "w_close_outcome", str(res.get("outcome") or ""))
vset(vs, "w_close_reason", str(res.get("reason") or ""))
