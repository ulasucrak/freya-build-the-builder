# Pre-hook of schedule_callback: normalise date and time; stop a callback outside the allowed window and count
# the stopped tries here (the post-hook does not run for a request that was stopped).
config = extra
body = body_of(config)
dfld = nm("callback_date", "date")
tfld = nm("callback_time", "time")
cal = make_calendar(RT)
today = today_of(vs)
now_time = now_time_of(vs)
dd = to_date(body.get(dfld)) or said_date(body.get(dfld), today, nm("next_weekday", ""), cal)
dd = fix_year(dd, today)
if dd is not None:
    body[dfld] = iso(dd)
tt = parse_time(body.get(tfld))
if tt:
    body[tfld] = tt
vset(vs, "w_cb_block", "")
chk = dict(ok=True)
bad = cnt(vs, "w_cb_bad")
mx = int((RT.get("callback") or dict()).get("max_asks") or 2)
if nm("guard", "on") == "on":
    # max_asks counts the stopped tries: once used up, a callback booked or two service failures, nothing goes out
    if "yes" in (vget(vs, "w_callback_done"), vget(vs, "w_cb_over")) or bad >= mx or fails(vs, nm("callback_tool", "schedule_callback")) >= 2:
        chk = dict(ok=False, code="no-callback", message="No further callback may be scheduled (one is booked, the tries are used up or the service failed twice).",
                   next="Do not ask for another time. Close the call as the rules say.")
    else:
        chk = check_callback(dd, tt, today, now_time, RT, cal, nm("next_weekday", ""))
if chk.get("ok"):
    request_sent(vs)
elif chk.get("code") == "no-callback":
    block_request(config, vs, "w_cb_block", chk, "schedule_callback")
else:
    bad += 1
    vset(vs, "w_cb_bad", str(bad))
    if bad < mx:
        chk["next"] = "Do not say it was scheduled. Ask once for another day and time inside the allowed window."
    else:
        ef = RT.get("enforce") or dict()
        nc = pair(ef.get("no_callback"))
        chk["next"] = "That was the last try: do not schedule a callback. " + (
            "Then " + close_instruction(nc["outcome"], nc["reason"]) if nc["outcome"] else "Close the call as the rules say for this case.")
        if ef.get("callback_close") and nc["outcome"]:
            # the engine closes from here (greet_verify routes on w_cb_over after this result): no close_call turn
            vset(vs, "w_close_req_outcome", nc["outcome"])
            vset(vs, "w_close_req_reason", nc["reason"])
            vset(vs, "w_end_say", "yes")
            vset(vs, "w_cb_over", "yes")
    block_request(config, vs, "w_cb_block", chk, "schedule_callback")
