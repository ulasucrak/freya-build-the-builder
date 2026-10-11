# Post-hook of verify_identity: tell the model what the result means and what to do next.
result = extra
idn = RT.get("identity") or dict()
qs = idn.get("questions") or []
d = resp_data(result)
field = str(vget(vs, "w_last_verify_field") or vget(vs, nm("verify_tool", "verify_identity") + "_args." + nm("verify_field", "field")) or "")
fail = idn.get("fail") or dict()
fail_msg = ("The identity check has FAILED: no attempts are left. Ask nothing more and say nothing about the account. "
            + (str(fail.get("say")) + " " if fail.get("say") else "") + "Then "
            + close_instruction(fail.get("outcome"), fail.get("reason")))
ev, act = "", ""
if d is None or not isinstance(result, dict) or str(result.get("status") or "success").lower() not in ("success", "ok"):
    set_next(result, "The security check service did not answer, nothing was counted. Say there was a short technical problem, "
             "ask for the same answer again and call verify_identity again.")
    ev, act = "Security check service did not answer (nothing counted)", "ask for the same answer again and call verify_identity again"
else:
    okf = nm("verify_ok", "ok")
    allv = truthy(d.get(nm("verify_all", "all_verified")))
    ver = truthy(d.get(nm("verify_one", "verified")))
    left = d.get(nm("verify_left", "wrong_answers_left"))
    hint = d.get(nm("verify_hint", "hint"))
    if allv:
        vset(vs, "w_id_state", "verified")
        set_next(result, "All security answers are correct. The customer is verified.")
    elif hint:
        rules = [r for r in (idn.get("wrong_counts_rules") or []) if str(hint) in r]
        set_next(result, "Not counted as a wrong answer. The service returned the hint '" + str(hint) + "'. "
                 + (" ".join(rules) if rules else "Ask the customer to give the answer again in a clearer form.")
                 + " Then call verify_identity again with the new answer.")
        ev, act = "Answer not counted (hint '" + str(hint) + "')", "ask for the answer again as the rules say and call verify_identity again"
    elif okf in d and not truthy(d.get(okf)):
        set_next(result, "The answer could not be read (" + api_error(result) + "). This does not count as a wrong answer. "
                 "Ask the customer to say it again, then call verify_identity again.")
        ev, act = "Answer could not be read (not counted)", "ask for it again and call verify_identity again"
    elif ver:
        done = csv_list(vget(vs, "w_id_done"))
        if field and field not in done:
            done.append(field)
        vset(vs, "w_id_done", ",".join(done))
        nxt = None
        for q in qs:
            if str(q.get("tool_field")) not in done:
                nxt = q
                break
        if nxt is not None:
            set_next(result, "Correct. Now ask the next security question: " + str(nxt.get("ask") or nxt.get("tool_field"))
                     + " (field '" + str(nxt.get("tool_field")) + "').")
        elif nm("verify_all", "all_verified") in d and not allv:
            # the bank says explicitly that the check is not complete although every listed question was answered
            first = qs[0] if qs else dict()
            set_next(result, "Correct, but the security service has not confirmed the identity yet. Ask the security question "
                     + str(first.get("ask") or first.get("tool_field") or "again") + " once more and call verify_identity again.")
            ev, act = "Identity not confirmed by the service yet", "ask the first security question again and call verify_identity"
        else:
            # every question answered correctly and nothing pending: verified even when the bank omits all_verified
            # (e.g. the customer was already verified in an earlier, lost call and the bank answers only verified=true)
            vset(vs, "w_id_state", "verified")
            set_next(result, "All security answers are correct. The customer is verified.")
    else:
        wrong = int(num(vget(vs, "w_id_wrong", 0))) + 1
        vset(vs, "w_id_wrong", str(wrong))
        mx = idn.get("max_wrong")
        n_left = None
        if left is not None and to_number(left) is not None:
            n_left = int(to_number(left))
        elif mx:
            n_left = int(mx) - wrong
        if n_left is not None and n_left <= 0:
            vset(vs, "w_id_state", "failed")
            set_next(result, "Wrong answer. " + fail_msg)
        else:
            set_next(result, "Wrong answer" + (" (" + str(n_left) + " wrong answer(s) left in total)" if n_left is not None else "")
                     + ". Do not say what the right answer is. Ask the same question once more, following the rules.")
set_action(vs, ev, act)
