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
qs = (RT.get("identity") or dict()).get("questions") or []
for x in qs:
    if str(x.get("tool_field")) == field:
        q = x
# fields already answered correctly, from one mark per field (parallel calls of one message each write their own)
rest = [str(x.get("tool_field")) for x in qs if str(vget(vs, key_of("w_id_ok_", x.get("tool_field")))) != "yes"]
info = None
g = ""
if id_state_of(vs) == "failed":
    info = dict(code="check-over", message="The identity check is already over (no attempts left).",
                next="Do not ask again. Follow the rule for a failed identity check and close the call.")
elif q is not None and qs and not rest:
    # every question already answered correctly (e.g. in parallel calls whose bank answers omitted all_verified)
    vset(vs, "w_id_state", "verified")
    info = dict(code="already-correct", message="Every security answer was already checked and is correct.",
                next="Do not ask again. The customer is verified.")
elif q is not None and str(body.get(af) or "").strip():
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
            g = "answer"
    elif n_digits > 0:
        digits = re.sub(r"\D", "", part + spoken_digits(ans))
        if 0 < len(digits) < n_digits:
            vset(vs, pkey, digits)
            info = dict(code="partial-answer", message="Only " + str(len(digits)) + " of " + str(n_digits) + " digits so far: " + digits + ".",
                        next="This is not a wrong answer. Ask only for the remaining digits, then call verify_identity again with just those; they are joined automatically.")
        else:
            # too many digits: the whole answer said again after a kept piece ('sorry, 4471'), or a correction of the
            # whole answer ('1520, sorry, 1502'); a shorter 'correction' ('four four, no, seven one') is a piece
            ok = [x for x in (digits, re.sub(r"\D", "", spoken_digits(ans)), re.sub(r"\D", "", spoken_digits(said_parts(ans)[-1])))
                  if len(x) == n_digits]
            body[af] = ok[0] if ok else normalise_answer(ans, q.get("normalise"), q.get("answer_format"))
            g = "" if ok else "answer"
            vset(vs, pkey, "")
    else:
        body[af] = normalise_answer(ans, q.get("normalise"), q.get("answer_format"))
elif qs:
    g = "answer"        # an unknown field or no answer
# FX11: garbled, empty or for an unknown field: neither a match nor a clear mismatch (twice in a row, then sent)
if unclear(vs, "vq" + (field if q else "?"), "" if info else g):
    info = dict(code="unclear", message="Answer or field unclear; nothing counted.", next=UNC)
if info is not None and nm("guard", "on") == "on":
    block_request(config, vs, "w_verify_block", info, "verify_identity")
else:
    request_sent(vs)
