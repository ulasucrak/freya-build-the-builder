# Post-hook of every api tool of the generic workflow (tools with no collection role). Generic: it reads only the
# shape of the answer, never a service name or a field name of one bank.
#
# LANG_PLACEHOLDER (call language). The generator sets it by replacing the token CALL_LANGUAGE_HERE in this source
# with policy.language (one plain word, e.g. Turkish) before build_code("generic_post", rt, "post", source=src):
#     src = (CODEGEN / "generic_post.py").read_text().replace("CALL_LANGUAGE_HERE", "Turkish")
# Left as is, the hook takes RT["language"], then the call variable w_lang (or w_language), then English.
# Turkish ("tr", "Turkish", "Türkçe" ...) gets Turkish words; any other language gets the English forms.
#
# What it does with `result` (the api_call envelope: status, status_code, data = the API's JSON body, message):
#  * success = HTTP 2xx, the body's status code (result.returnCode / code ...) absent or a success code (0 / 1 / OK /
#    SUCCESS), and no error flag in the body. The fields of the body's `data` object (when it has one) become the
#    tool result, with ok = true; the `result` envelope (trace ids, "Success" texts) is dropped.
#  * error: the tool result is only ok = false, code, type, http_status and what_to_do_next. The service's message
#    text never reaches the model.
#  * spoken forms: a sibling '<field>_spoken' for every date (dd.MM.yyyy, yyyy-MM-dd, ISO date-time) and for every
#    amount (a number in a block or field named after a currency, next to a currency field, or in a field named
#    like an amount), in number words + currency words of the call language. Missing or null values get nothing.
LANG_PLACEHOLDER = "CALL_LANGUAGE_HERE"
result = extra
gp_lang = ""
for gp_c in (LANG_PLACEHOLDER, RT.get("language"), RT.get("lang"), vget(vs, "w_lang"), vget(vs, "w_language")):
    gp_c = str(gp_c or "").strip().lower()
    if gp_c and "language_here" not in gp_c and not gp_lang:
        gp_lang = "tr" if re.match(r"t(r|ur|ür)", gp_c) else "en"
TR = gp_lang == "tr"
TR_ONES = "sıfır bir iki üç dört beş altı yedi sekiz dokuz".split(" ")
TR_TENS = " on yirmi otuz kırk elli altmış yetmiş seksen doksan".split(" ")
EN_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen"
            " seventeen eighteen nineteen").split(" ")
EN_TENS = "  twenty thirty forty fifty sixty seventy eighty ninety".split(" ")
TR_MONTHS = "Ocak Şubat Mart Nisan Mayıs Haziran Temmuz Ağustos Eylül Ekim Kasım Aralık".split(" ")
TR_DAYS = "Pazartesi Salı Çarşamba Perşembe Cuma Cumartesi Pazar".split(" ")
EN_MONTHS = "January February March April May June July August September October November December".split(" ")
EN_DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split(" ")
# currency -> (tr major, tr minor, en major one, en major many, en minor one, en minor many)
CUR_WORDS = dict(TRY=("lira", "kuruş", "Turkish lira", "Turkish lira", "kuruş", "kuruş"),
                 USD=("dolar", "sent", "dollar", "dollars", "cent", "cents"),
                 EUR=("avro", "sent", "euro", "euros", "cent", "cents"),
                 GBP=("sterlin", "peni", "pound", "pounds", "penny", "pence"))
CUR_OF = dict(tl="TRY", ytl="TRY", lira="TRY", usd="USD", dollar="USD", dolar="USD", eur="EUR", euro="EUR",
              avro="EUR", gbp="GBP", sterling="GBP", sterlin="GBP", pound="GBP")
CUR_OF["try"] = "TRY"
CUR_SYM = (("₺", "TRY"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"))
# name parts that make a number NOT an amount (counts, ids, codes, calendar numbers, rates)
NOT_MONEY = set(("count cnt number no num nr id ids week weeks day days month months year years code codes index idx"
                 " rate rates percent percentage pct ratio flag qty quantity status type digits phone page pages size"
                 " score age hour hours minute minutes second seconds version times last4 sayi sayısı adet").split(" "))
# name parts (prefixes) that make a number an amount even with no currency nearby
MONEY_PARTS = ("amount", "amt", "balance", "debt", "total", "minimum", "payment", "expense", "spending", "price",
               "fee", "cost", "sum", "limit", "due", "owed", "outstanding", "principal", "charge", "installment",
               "instalment", "overdue", "arrears", "tutar", "borc", "borç", "bakiye", "toplam", "odeme", "ödeme",
               "ucret", "ücret", "fiyat")
CODE_KEYS = ("returnCode", "resultCode", "errorCode", "statusCode")
DATE_RX = re.compile(r"(?:(\d{1,2})\.(\d{1,2})\.(\d{4})|(\d{4})-(\d{2})-(\d{2}))(?:[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
AMT_RX = re.compile(r"([₺$€£]|[A-Za-z]{2,3})?\s*(-?\d+(?:\.\d+)?|-?\d+,\d{1,2})\s*([₺$€£]|[A-Za-z]{2,3})?$")


def gp_parts(key):
    return [p.lower() for p in re.findall(r"[A-ZÇĞİÖŞÜ]+(?![a-zçğıöşü])|[A-ZÇĞİÖŞÜ]?[a-zçğıöşü]+|\d+", str(key))]


def gp_cur(text):
    t = str(text or "").strip().lower()
    for sym, code in CUR_SYM:
        if sym in t:
            return code
    for w in re.findall(r"[a-zçğıöşü]+", t):
        if CUR_OF.get(w):
            return CUR_OF.get(w)
    return ""


def gp_words(n, tr):
    n = int(n)
    if n == 0:
        return TR_ONES[0] if tr else EN_SMALL[0]
    out = []
    for v, wtr, wen in ((10 ** 12, "trilyon", "trillion"), (10 ** 9, "milyar", "billion"), (10 ** 6, "milyon", "million"),
                        (1000, "bin", "thousand"), (1, "", "")):
        q = n // v
        n = n % v
        if not q:
            continue
        if q >= 1000:
            out.append(gp_words(q, tr))
        elif not (tr and v == 1000 and q == 1):
            h = q // 100
            r = q % 100
            if h:
                out.append(((TR_ONES[h] + " " if h > 1 else "") + "yüz") if tr else EN_SMALL[h] + " hundred")
            if r and tr:
                out.append((TR_TENS[r // 10] + " " + (TR_ONES[r % 10] if r % 10 else "")).strip())
            elif r:
                out.append(EN_SMALL[r] if r < 20 else EN_TENS[r // 10] + ("-" + EN_SMALL[r % 10] if r % 10 else ""))
        out.append(wtr if tr else wen)
    return " ".join([w for w in out if w])


def gp_amount(x, cur, tr):
    cents = int(round(abs(x) * 100))
    major = cents // 100
    minor = cents % 100
    w = CUR_WORDS.get(cur)
    if w:
        parts = []
        if major or not minor:
            parts.append(gp_words(major, tr) + " " + (w[0] if tr else w[2 if major == 1 else 3]))
        if minor:
            parts.append(gp_words(minor, tr) + " " + (w[1] if tr else w[4 if minor == 1 else 5]))
        s = (" " if tr else " and ").join(parts)
    else:
        s = gp_words(major, tr)
        frac = ("%.2f" % abs(x)).split(".")[1].rstrip("0")
        if frac and tr:
            s += " virgül " + ("sıfır " if frac[0] == "0" else "") + gp_words(int(frac), tr)
        elif frac:
            s += " point " + " ".join([EN_SMALL[int(c)] for c in frac])
    return (("eksi " if tr else "minus ") + s) if x < 0 and cents else s


def gp_date(text, tr):
    m = DATE_RX.match(text.strip())
    if not m:
        return ""
    g = m.groups()
    try:
        d = datetime.date(int(g[2]), int(g[1]), int(g[0])) if g[0] else datetime.date(int(g[3]), int(g[4]), int(g[5]))
    except Exception:
        return ""
    if tr:
        s = str(d.day) + " " + TR_MONTHS[d.month - 1] + " " + str(d.year) + " " + TR_DAYS[d.weekday()]
    else:
        s = EN_DAYS[d.weekday()] + " " + str(d.day) + " " + EN_MONTHS[d.month - 1] + " " + str(d.year)
    if g[6] and (int(g[6]) or int(g[7])):
        s += (", saat " if tr else " at ") + g[6] + ":" + g[7]
    return s


def gp_number(v):
    # (number, currency written in the value) or (None, "")
    if v is None or isinstance(v, bool):
        return None, ""
    if isinstance(v, (int, float)):
        return float(v), ""
    m = AMT_RX.match(str(v).strip())
    if not m:
        return None, ""
    sign = m.group(1) or m.group(3) or ""
    c = gp_cur(sign)
    if sign and not c:
        return None, ""
    try:
        return float(m.group(2).replace(",", ".")), c
    except Exception:
        return None, ""


def gp_walk(node, cur, depth, tr):
    # adds '<field>_spoken' siblings in place; cur = currency of the enclosing block ("" when none)
    if depth > 8:
        return 0
    if isinstance(node, list):
        n = 0
        for x in node[:60]:
            n += gp_walk(x, cur, depth + 1, tr)
        return n
    if not isinstance(node, dict):
        return 0
    here = cur
    for k, v in node.items():
        ps = gp_parts(k)
        if isinstance(v, str) and ("currency" in ps or "ccy" in ps or "curr" in ps or "doviz" in ps or "döviz" in ps) and gp_cur(v):
            here = gp_cur(v)
    add = dict()
    n = 0
    for k, v in list(node.items()):
        sk = str(k)
        if sk.endswith("_spoken") or (sk + "_spoken") in node or v is None or isinstance(v, bool):
            continue
        ps = gp_parts(sk)
        kc = ""
        for p in ps:
            kc = kc or CUR_OF.get(p, "")
        if isinstance(v, (dict, list)):
            n += gp_walk(v, kc or here, depth + 1, tr)
            continue
        try:
            s = gp_date(v, tr) if isinstance(v, str) else ""
            if not s and not [p for p in ps if p in NOT_MONEY]:
                x, vc = gp_number(v)
                c = vc or kc or here
                # a zero amount gets no spoken form: nothing to verbalise, and it keeps the answer short
                if x is not None and x == x and 0.005 <= abs(x) < 10 ** 15 and (c or [p for p in ps if p.startswith(MONEY_PARTS)]):
                    s = gp_amount(x, c, tr)
            if s:
                add[sk + "_spoken"] = s
        except Exception:
            pass
    node.update(add)
    return n + len(add)


def gp_success_code(c):
    t = str(c).strip().upper()
    if t.endswith(".0"):
        t = t[:-2]
    return t in ("", "0", "00", "000", "1", "01", "OK", "SUCCESS", "SUCCEEDED", "SUCCESSFUL", "S", "TRUE", "NONE", "200")


def gp_flag_false(v):
    return v is False or str(v).strip().lower() in ("false", "no", "0", "n")


if isinstance(result, dict):
    result.pop("header", None)
    body = result.get("data")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            pass
    msg = str(result.get("message") or "")
    if not isinstance(body, dict) and "{" in msg:
        try:
            body = json.loads(msg[msg.index("{"):])
        except Exception:
            pass
    body = body if isinstance(body, dict) else dict()
    # the status envelope: a 'result' object that carries a status code or a message (else 'result' is data)
    env = body.get("result") if isinstance(body.get("result"), dict) else dict()
    if not [k for k in CODE_KEYS + ("code", "message", "messageText") if k in env]:
        env = dict()
    try:
        http = int(float(str(result.get("status_code"))))
    except Exception:
        http = 0
    st = str(result.get("status") or "").strip().lower()
    http_ok = 200 <= http < 300 if http else st in ("", "success", "ok")
    rc = None
    for k in CODE_KEYS + ("code",):
        if rc is None and env.get(k) not in (None, ""):
            rc = env.get(k)
    for k in CODE_KEYS:
        if rc is None and body.get(k) not in (None, ""):
            rc = body.get(k)
    typ = env.get("type") or env.get("severity") or ""
    err = body.get("error") or env.get("error") or body.get("errors")
    flag = [v for v in (body.get("ok"), body.get("success"), env.get("ok"), env.get("success")) if v is not None and gp_flag_false(v)]
    if rc is not None:
        code_ok = gp_success_code(rc)
    else:
        code_ok = str(typ).strip().upper() not in ("ERROR", "WARNING", "FATAL", "FAIL", "FAILED", "FAILURE")
    if http_ok and code_ok and not err and not flag:
        inner = body.get("data")
        if isinstance(inner, dict):
            out = dict(inner)
        elif inner is not None:
            out = dict(items=inner)
        else:
            out = dict((k, v) for k, v in body.items() if not (env and k == "result"))
        try:
            spoken = gp_walk(out, "", 0, TR)
        except Exception:
            spoken = 0
        out["ok"] = True
        if spoken and "what_to_do_next" not in out:
            out["what_to_do_next"] = ("To say a date or an amount, use the words of its _spoken field"
                                      + (" (Turkish)" if TR else "") + "; keep the rule's sentence and do not say the currency twice.")
        result["data"] = out
    else:
        code = rc if rc is not None and not gp_success_code(rc) else ""
        if not code:
            for k in ("code", "errorCode"):
                if not code and isinstance(body.get(k), (str, int)) and not isinstance(body.get(k), bool):
                    code = body.get(k)
        if not code and isinstance(err, str) and re.match(r"[A-Za-z0-9_.:-]{1,40}$", err):
            code = err
        if not code and isinstance(err, dict):
            code = err.get("code") or err.get("type") or ""
        code = str(code or "").strip()
        typ = str(typ or "").strip()
        lab = "/".join([x for x in (code, typ) if x])
        if lab or http:
            t = "The service answered " + (lab or "an error") + (" (HTTP " + str(http) + ")" if http else "") + "."
        else:
            t = "The service did not answer."
        out = dict(ok=False)
        if code:
            out["code"] = code
        if typ:
            out["type"] = typ
        if http:
            out["http_status"] = http
        out["what_to_do_next"] = t + " Follow the rule for this situation; never read the service's message text aloud."
        result["data"] = out
        result["message"] = t
