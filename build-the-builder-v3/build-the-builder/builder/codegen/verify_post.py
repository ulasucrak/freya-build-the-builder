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
okf = nm("verify_ok", "ok")
dd = d if isinstance(d, dict) else dict()
allv = truthy(dd.get(nm("verify_all", "all_verified")))
err = api_error(result).lower()
alr = "already" in err and "verif" in err
ver = truthy(dd.get(nm("verify_one", "verified"))) or alr
left = dd.get(nm("verify_left", "wrong_answers_left"))
hint = dd.get(nm("verify_hint", "hint"))
n_left = int(to_number(left)) if left is not None and to_number(left) is not None else None
bad = d is None or not isinstance(result, dict) or str(result.get("status") or "success").lower() not in ("success", "ok")
# locked / no attempts left: the check is over (rule-based failed close), not a retry and not a service failure
dead = not ver and not allv and (bad or (okf in dd and not truthy(dd.get(okf)))) and (
    n_left is not None and n_left <= 0 or re.search(r"lock|too many|no (more )?(attempts|tries)|\b0 (attempts|tries)|exhaust|blocked", err) is not None)
fields = [str(q.get("tool_field")) for q in qs]
# FX11: an answer the service could not read is neither a match nor a mismatch (twice in a row, then the strict text)
rd = not dead and not bad and not allv and not hint and okf in dd and not truthy(dd.get(okf)) and not alr
uc = unclear(vs, "vr" + field, "answer" if rd else "")
ask_why = " If the customer asks why instead of answering, that is not a wrong answer: explain once, as the rules say, and ask again."
if dead:
    vset(vs, "w_id_state", "failed")
    set_next(result, "The security service says the check is over (" + (err or "no attempts left") + "). " + fail_msg)
    ev, act = "Identity check failed (no attempts left)", "say nothing about the account and close_call as the rules say"
elif bad:
    set_next(result, "The security check service did not answer, nothing was counted. Say there was a short technical problem, "
             "ask for the same answer again and call verify_identity again.")
    ev, act = "Security check service did not answer (nothing counted)", "ask for the same answer again and call verify_identity again"
else:
    if allv:
        vset(vs, "w_id_state", "verified")
        set_next(result, "All security answers are correct. The customer is verified.")
    elif hint:
        rules = [r for r in (idn.get("wrong_counts_rules") or []) if str(hint) in r]
        set_next(result, "Not counted as a wrong answer. The service returned the hint '" + str(hint) + "'. "
                 + (" ".join(rules) if rules else "Ask the customer to give the answer again in a clearer form.")
                 + " Then call verify_identity again with the new answer.")
        ev, act = "Answer not counted (hint '" + str(hint) + "')", "ask for the answer again as the rules say and call verify_identity again"
    elif rd:
        set_next(result, "The answer could not be read (not counted). " + UNC if uc else "The answer could not be read (" + api_error(result) + "). This does not count as a wrong answer. "
                 "Ask the customer to say it again, then call verify_identity again.")
        ev, act = "Answer could not be read (not counted)", "ask for it again and call verify_identity again"
    elif ver:
        if field:
            mark(vs, "w_id_ok_", field, "yes")
        done = [f for f in fields if str(vget(vs, key_of("w_id_ok_", f))) == "yes"]
        done += [f for f in csv_list(vget(vs, "w_id_done")) + [field] if f and f not in done]
        vset(vs, "w_id_done", ",".join(done))
        nxt = None
        for q in qs:
            if str(q.get("tool_field")) not in done:
                nxt = q
                break
        if nxt is not None:
            set_next(result, "Correct. Now ask the next security question: " + str(nxt.get("ask") or nxt.get("tool_field"))
                     + " (field '" + str(nxt.get("tool_field")) + "')." + ask_why)
        elif nm("verify_all", "all_verified") in d and not allv:
            # the bank says explicitly that the check is not complete although every listed question was answered
            first = qs[0] if qs else dict()
            mark(vs, "w_id_ok_", first.get("tool_field"), "")       # asked again on purpose: not blocked as already correct
            set_next(result, "Correct, but the security service has not confirmed the identity yet. Ask the security question "
                     + str(first.get("ask") or first.get("tool_field") or "again") + " once more and call verify_identity again.")
            ev, act = "Identity not confirmed by the service yet", "ask the first security question again and call verify_identity"
        else:
            # every question answered correctly and nothing pending: verified even when the bank omits all_verified
            # (e.g. the customer was already verified in an earlier, lost call and the bank answers only verified=true)
            vset(vs, "w_id_state", "verified")
            set_next(result, "All security answers are correct. The customer is verified.")
    else:
        # one wrong counter per field: parallel wrong answers in one message are all counted
        mark(vs, "w_id_bad_", field, str(int(num(vget(vs, key_of("w_id_bad_", field), 0))) + 1))
        wrong = sum(int(num(vget(vs, key_of("w_id_bad_", f), 0))) for f in set(fields + [field]))
        vset(vs, "w_id_wrong", str(wrong))
        mx = idn.get("max_wrong")
        if n_left is None and mx:
            n_left = int(mx) - wrong
        if n_left is not None and n_left <= 0:
            vset(vs, "w_id_state", "failed")
            set_next(result, "Wrong answer. " + fail_msg)
        else:
            set_next(result, "Wrong answer" + (" (" + str(n_left) + " wrong answer(s) left in total)" if n_left is not None else "")
                     + ". Do not say what the right answer is. Ask the same question once more, following the rules." + ask_why)
set_action(vs, ev, act)
