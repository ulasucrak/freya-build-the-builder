# Code node at call start: date facts from the call's own "now" variable, and every variable a prompt or hook
# reads later gets a value (an unset variable would show as a literal placeholder).
cal = make_calendar(RT)
today, now_time = parse_now(vget(vs, nm("now_var", "now")))
clock_ok = "yes"
if today is None:
    clock_ok = "no"
    today = datetime.date.today()
    now_time = datetime.datetime.now().strftime("%H:%M")
vset(vs, "w_today", iso(today))
vset(vs, "w_now_time", now_time)
vset(vs, "w_clock_ok", clock_ok)
vset(vs, "w_date_facts", date_facts_text(RT, today, now_time, cal))
wins = callback_windows(today, now_time, RT, cal)
tomorrow = today + datetime.timedelta(days=1)
tm_note = ""
if iso(tomorrow) not in [w["date"] for w in wins]:
    tm_note = (" Tomorrow, " + date_label(tomorrow) + ", is NOT allowed for a callback" + (" (" + day_kind(tomorrow, cal) + ")" if not is_business_day(tomorrow, cal) else "")
               + ": if the person names it, say so and ask once for another time; never move the day yourself.")
vset(vs, "w_callback_windows", "Today is " + date_label(today) + (", time " + now_time if now_time else "") + ". Allowed: "
     + ("; ".join([w["label"] for w in wins]) if wins else "none") + "." + tm_note)
for k, v in [("w_account_facts", "Not loaded yet. Nothing about the account is known."), ("w_products_json", "[]"),
             ("w_product_count", "0"), ("w_account_state", "pending"), ("w_promised", ""), ("w_notes", json.dumps(dict())), ("w_note_intents", json.dumps(dict())), ("w_last_event", "none"), ("w_close_sends", "0"), ("w_close_held", "no"), ("w_close_requested", "no"), ("w_close_req_outcome", ""), ("w_close_req_reason", ""), ("w_wrap_rounds", "0"), ("w_wrap_todo", ""), ("w_close_route", ""), ("w_forced", "no"), ("w_notes_rule", "Do not call add_note yet."), ("w_next_action", ""),
             ("w_transfers", ""), ("w_id_state", "pending"), ("w_id_wrong", "0"), ("w_id_done", ""),
             ("w_callback_done", "no"), ("w_cb_bad", "0"), ("w_close_done", "no"), ("w_close_outcome", ""),
             ("w_close_reason", ""), ("w_cb_over", "no"), ("w_end_say", "no"), ("w_turn_route", "stay"), ("w_no_talk", "0"),
             ("w_enter_prompt", "")]:
    if vget(vs, k, None) is None:
        vset(vs, k, v)
