# Code node of document mode: calendar facts from the call's date-time variable, so the model never computes a
# weekday or a date itself. Three weeks ahead, each day with its weekday and ISO date.
today, now_t = parse_now(vget(vs, nm("now_var", "now")))
lines = []
if today is not None:
    lines.append("Today: " + date_label(today) + ((", time " + now_t) if now_t else "") + ".")
    for k in range(1, 29):
        d = today + datetime.timedelta(days=k)
        lines.append(("Tomorrow: " if k == 1 else "") + date_label(d))
vset(vs, "w_calendar", "\n".join(lines) if lines else "The call date is not known.")
vset(vs, "w_end", "no")
