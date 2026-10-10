# Pre-hook of close_call (model tool and the forced close): outcome and reason follow from the recorded facts
# where they decide it (transfer, failed identity, promise, callback, notes); otherwise the model's choice stays.
# When notes are used, the account is loaded, no transfer happened and a product has neither a promise nor a
# note, the first close is held back once and the model is told which notes to send first.
config = extra
body = body_of(config)
of = nm("close_outcome", "outcome")
rf = nm("close_reason", "reason")
facts = collect_facts(vs)
missing = []
if (RT.get("notes") or dict()).get("enabled") and facts.get("verified") and not facts.get("transfers"):
    sent = notes_of(vs)
    intents = note_intents(vs)
    for p in products_of(vs):
        pid = p.get("id")
        if pid not in facts.get("promises") and pid not in sent:
            missing.append(str(pid) + (" (reason '" + str(intents[pid]) + "')" if pid in intents else ""))
# The model's outcome needs a fact the bank has not confirmed yet (a transfer, a promise, a callback): either the
# tool was never called, or it was called in the same message as close_call (parallel calls: its result is not
# in this copy of the variables, and the bank could get the close first). Hold the close back.
cl = RT.get("closing") or dict()
want = pair(dict(outcome=body.get(of), reason=body.get(rf)))
need = ""
tr_pairs = [pair(v) for v in (cl.get("transfer") or dict()).values()]
if want["outcome"] and any(want["outcome"] == t["outcome"] and (not t["reason"] or want["reason"] == t["reason"]) for t in tr_pairs) \
        and not facts.get("transfers"):
    need = "No transfer is recorded. If a rule requires a transfer, call transfer_to_agent first and wait for its result."
elif want["outcome"] and want["outcome"] in (pair(cl.get("promise"))["outcome"], pair(cl.get("promise_mixed"))["outcome"]) \
        and not facts.get("promises"):
    need = "No promise is recorded. Call create_promise first (after the read-back and a clear yes) and wait for its result."
elif want["outcome"] and want == pair(cl.get("callback")) and not facts.get("callback_done"):
    need = "No callback is booked. Call schedule_callback first and wait for its result."
holds = int(num(vget(vs, "w_close_holds", 0)))
if need and holds < 2 and nm("guard", "on") == "on":
    vset(vs, "w_close_holds", str(holds + 1))
    block_request(config, vs, "w_close_block", dict(code="close-too-early", message="The call is not closed yet. " + need,
                  next="Call close_call again afterwards, alone in its own message."), "close_call")
elif missing and str(vget(vs, "w_close_held")) != "yes" and nm("guard", "on") == "on":
    vset(vs, "w_close_held", "yes")
    block_request(config, vs, "w_close_block", dict(code="notes-first", message="The call is not closed yet: these products have no promise and no note: " + ", ".join(missing) + ".",
                  next="First call add_note for each of them with its reason, one per message, then call close_call again."), "close_call")
else:
    facts["llm"] = dict(outcome=body.get(of), reason=body.get(rf))
    res = compute_closing(facts, RT)
    if res.get("outcome"):
        body[of] = res.get("outcome")
        if res.get("reason"):
            body[rf] = res.get("reason")
        else:
            body.pop(rf, None)
    elif not str(body.get(rf) or "").strip() or str(body.get(rf)).startswith("{"):
        body.pop(rf, None)
    vset(vs, "w_close_done", "yes")
    vset(vs, "w_close_sends", str(int(num(vget(vs, "w_close_sends", 0))) + 1))
    request_sent(vs)
