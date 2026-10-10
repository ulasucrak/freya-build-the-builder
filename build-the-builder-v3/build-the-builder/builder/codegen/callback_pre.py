# Pre-hook of schedule_callback: normalise date and time; stop a callback outside the allowed window and count
# the stopped tries here (the post-hook does not run for a request that was stopped).
config = extra
body = body_of(config)
dfld = nm("callback_date", "date")
tfld = nm("callback_time", "time")
cal = make_calendar(RT)
today = today_of(vs)
now_time = now_time_of(vs)
dd = to_date(body.get(dfld)) or parse_spoken_date(body.get(dfld), today, nm("next_weekday", ""))
dd = fix_year(dd, today)
if dd is not None:
    body[dfld] = iso(dd)
tt = parse_time(body.get(tfld))
if tt:
    body[tfld] = tt
vset(vs, "w_cb_block", "")
chk = dict(ok=True)
if nm("guard", "on") == "on":
    chk = check_callback(dd, tt, today, now_time, RT, cal, nm("next_weekday", ""))
if chk.get("ok"):
    request_sent(vs)
else:
    bad = int(num(vget(vs, "w_cb_bad", 0))) + 1
    vset(vs, "w_cb_bad", str(bad))
    if bad < int(num((RT.get("callback") or dict()).get("max_asks"), 2)):
        chk["next"] = "Do not say it was scheduled. Ask once for another day and time inside the allowed window."
    else:
        nc = pair((RT.get("closing") or dict()).get("no_callback"))
        chk["next"] = "That was the last try: do not schedule a callback. " + (
            "Then " + close_instruction(nc["outcome"], nc["reason"]) if nc["outcome"] else "Close the call as the rules say for this case.")
    block_request(config, vs, "w_cb_block", chk, "schedule_callback")
