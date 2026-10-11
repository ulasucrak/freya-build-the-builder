# Tool end_call_now (document mode): the model ends the call after its goodbye; routing leaves for the end node.
args = extra if isinstance(extra, dict) else dict()
vset(vs, "w_end", "yes")
vset(vs, "w_ending", str(args.get("ending") or ""))
out = dict(ok=True, what_to_do_next="The call is ending. Say nothing more.")
