# Code node after a close request (or a turn cap): is anything missing before the call may be closed? Once per
# call the model gets a short to-do list in the wrap_up step (notes not sent, a transfer or a promise its chosen
# outcome needs but that is not recorded); otherwise, or the second time, the call goes to finish.
facts = collect_facts(vs)
cl = RT.get("closing") or dict()
want = pair(dict(outcome=vget(vs, "w_close_req_outcome"), reason=vget(vs, "w_close_req_reason")))
todo = []
need_transfer = False
if want["outcome"] and not facts.get("transfers"):
    for q, v in (cl.get("transfer") or dict()).items():
        v = pair(v)
        if v["outcome"] == want["outcome"] and (v["reason"] == want["reason"] or not want["reason"]):
            need_transfer = True
            todo.append("The call is closing as " + want["outcome"] + (" / " + want["reason"] if want["reason"] else "")
                        + " but no transfer is recorded. If the rules require notes before this transfer, send them first; then call transfer_to_agent with queue '" + str(q) + "'.")
            break
if not need_transfer and want["outcome"] and not facts.get("promises") and want["outcome"] in (
        pair(cl.get("promise"))["outcome"], pair(cl.get("promise_mixed"))["outcome"]):
    todo.append("The call is closing as " + want["outcome"] + " but no promise is recorded. If the customer confirmed a promise, call create_promise for it now; otherwise close with the outcome that applies.")
if not need_transfer:
    missing = missing_notes(vs, facts)
    if missing:
        todo.append("These products have no recorded promise and no note yet: " + ", ".join(missing)
                    + ". Call add_note for each of them, one per message, with the reason that applies from the conversation.")
rounds = int(num(vget(vs, "w_wrap_rounds", 0)))
if todo and rounds < 1:
    vset(vs, "w_wrap_rounds", str(rounds + 1))
    vset(vs, "w_wrap_todo", " ".join(todo) + " When this is done, call close_call again.")
    vset(vs, "w_close_route", "wrap")
else:
    vset(vs, "w_close_route", "finish")
vset(vs, "w_close_requested", "no")
