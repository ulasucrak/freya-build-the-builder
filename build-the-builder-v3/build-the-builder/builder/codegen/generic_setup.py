# Code node at call start of the generic (service agent) workflow: date facts from the call's own "now" variable,
# the call language, and a value for every variable a prompt shows (an unset one would show as a literal placeholder).
# The sandbox clock is used only when the call passed no readable "now" (w_clock_ok "no" then).
today, now_time = parse_now(vget(vs, nm("now_var", "now")))
clock_ok = "yes"
if today is None:
    clock_ok = "no"
    today = datetime.date.today()
    now_time = datetime.datetime.now().strftime("%H:%M")
vset(vs, "w_today", iso(today))
vset(vs, "w_now_time", now_time)
vset(vs, "w_weekday", WEEKDAY_NAMES[today.weekday()].capitalize())
vset(vs, "w_clock_ok", clock_ok)
vset(vs, "w_now_text", date_label(today) + (", " + now_time if now_time else ""))
vset(vs, "w_lang", nm("language", "English"))
for k, v in [("w_end_reason", ""), ("w_last_event", "none")]:
    if vget(vs, k, None) is None:
        vset(vs, k, v)
for k in RT.get("call_vars") or []:
    if vget(vs, k, None) is None:
        vset(vs, k, "")
