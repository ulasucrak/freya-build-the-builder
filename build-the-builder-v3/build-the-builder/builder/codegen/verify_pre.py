# Pre-hook of verify_identity: normalise the answer the way the brief says (dates to YYYY-MM-DD, digits only), and
# put together an answer the customer gives in pieces over several turns ("14 March" ... "1988"; "44" ... "71"):
# a piece that is not a whole answer yet is kept and the request is stopped (nothing is counted); the next piece
# is joined to it. A stopped check (no attempts left) is not called again.
config = extra
body = body_of(config)
ff = nm("verify_field", "field")
af = nm("verify_answer", "answer")
field = str(body.get(ff) or "")
vset(vs, "w_last_verify_field", field)
q = None
for x in (RT.get("identity") or dict()).get("questions") or []:
    if str(x.get("tool_field")) == field:
        q = x
info = None
if id_state_of(vs) == "failed":
    info = dict(code="check-over", message="The identity check is already over (no attempts left).",
                next="Do not ask again. Follow the rule for a failed identity check and close the call.")
elif q is not None and af in body:
    ans = str(body.get(af) or "").strip()
    pkey = key_of("w_id_part_", field)
    part = str(vget(vs, pkey, "") or "")
    is_date = q.get("answer_format") == "date" or str(q.get("normalise") or "").endswith("_to_iso")
    n_digits = int(num(q.get("digits"), 0))
    if is_date:
        cand = ans
        norm = normalise_answer(ans, q.get("normalise"), q.get("answer_format"))
        if to_date(norm) is None and part:          # not a whole answer by itself: join it to the kept piece
            cand = (part + " " + ans).strip()
            norm = normalise_answer(cand, q.get("normalise"), q.get("answer_format"))
        d = to_date(norm)
        today = today_of(vs)
        if d is not None and today is not None and d.year >= today.year - 1 \
                and "birth" in (field + " " + str(q.get("ask") or "")).lower():
            # a birth date in this or last year is a year the model filled in for "the 23rd of February": keep the
            # day and month as a piece and ask for the year; nothing is sent, so nothing is counted as wrong
            cand = str(d.day) + " " + MONTH_NAMES[d.month - 1]
            vset(vs, pkey, cand)
            info = dict(code="partial-answer", message="Only part of the answer so far: '" + cand + "' (day and month, no year).",
                        next="This is not a wrong answer. Ask only for the year of birth, then call verify_identity again with just the year; the pieces are joined automatically.")
        elif d is not None:
            body[af] = norm
            vset(vs, pkey, "")
        elif date_piece(cand):
            vset(vs, pkey, cand)
            info = dict(code="partial-answer", message="Only part of the answer so far: '" + cand + "' (" + date_piece(cand) + ").",
                        next="This is not a wrong answer. Ask only for the missing part, then call verify_identity again with just that part; the pieces are joined automatically.")
        else:
            body[af] = norm
    elif n_digits > 0:
        digits = re.sub(r"\D", "", part + spoken_digits(ans))
        if 0 < len(digits) < n_digits:
            vset(vs, pkey, digits)
            info = dict(code="partial-answer", message="Only " + str(len(digits)) + " of " + str(n_digits) + " digits so far: " + digits + ".",
                        next="This is not a wrong answer. Ask only for the remaining digits, then call verify_identity again with just those; they are joined automatically.")
        else:
            # too many digits with a kept piece: the customer said the whole answer again ('sorry, 4471')
            alone = re.sub(r"\D", "", spoken_digits(ans))
            body[af] = digits if len(digits) == n_digits else (alone if len(alone) == n_digits else normalise_answer(ans, q.get("normalise"), q.get("answer_format")))
            vset(vs, pkey, "")
    else:
        body[af] = normalise_answer(ans, q.get("normalise"), q.get("answer_format"))
if info is not None and nm("guard", "on") == "on":
    block_request(config, vs, "w_verify_block", info, "verify_identity")
else:
    request_sent(vs)
