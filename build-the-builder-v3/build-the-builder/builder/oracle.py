"""Reference logic computed from a policy (policy.json contract v1, see policy_schema.py).

Pure Python, standard library only (datetime, json, re), Python 3.9 compatible.

The same source is used in three places:
  * by the unit tests (import builder.oracle),
  * by the generated workflow: generate.py copies the functions it needs (between the EMBED markers)
    into code nodes and tool hooks, so the phone agent runs exactly this logic,
  * by the synthetic-case generator (expected deadlines, offers, closing outcomes).

Nothing here knows a brief. Every number, date, name and outcome comes from the policy dict.

Rules for the embedded region (enforced by tests/test_generate.py):
  * no double opening or closing braces anywhere (the platform may template them),
  * no names that start with an underscore (some sandboxes forbid them),
  * no f-strings, no annotations, no classes, no global/nonlocal,
  * every function tolerates bad input and returns a neutral value instead of raising.
"""
# EMBED-BEGIN
import datetime
import json
import re

MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"]
MONTH_ABBR = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ORDINAL_WORDS = dict(first=1, second=2, third=3, fourth=4, fifth=5, sixth=6, seventh=7, eighth=8, ninth=9,
                     tenth=10, eleventh=11, twelfth=12, thirteenth=13, fourteenth=14, fifteenth=15,
                     sixteenth=16, seventeenth=17, eighteenth=18, nineteenth=19, twentieth=20,
                     thirtieth=30)
UNIT_WORDS = "one two three four five six seven eight nine first second third fourth fifth sixth seventh eighth ninth".split()
NUM_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen"
             " eighteen nineteen").split()
TENS_WORDS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


# ----------------------------------------------------------------------------------------------- basics

def to_date(value):
    """date, datetime or a string containing YYYY-MM-DD -> date, else None."""
    try:
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if value is None:
            return None
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(value))
        if not m:
            return None
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def iso(d):
    """date -> 'YYYY-MM-DD' ('' for None)."""
    try:
        return d.isoformat() if d else ""
    except Exception:
        return ""


def hhmm(h, m):
    return "%02d:%02d" % (int(h), int(m))


def parse_now(value):
    """The call's 'now' variable -> (date, 'HH:MM'). Accepts ISO (2030-01-15T09:30, 2030-01-15 09:30:00Z)
    and day-first numeric forms (15.01.2030 09:30, 15/01/2030). Returns (None, '') when unreadable."""
    try:
        s = str(value or "").strip()
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]+(\d{1,2}):(\d{2}))?", s)
        if m:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            t = hhmm(m.group(4), m.group(5)) if m.group(4) else ""
            return d, t
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})(?:[ ,T]+(\d{1,2}):(\d{2}))?", s)
        if m:
            d = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            t = hhmm(m.group(4), m.group(5)) if m.group(4) else ""
            return d, t
    except Exception:
        pass
    return None, ""


def time_to_minutes(value):
    """'HH:MM' -> minutes since midnight, or None."""
    try:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(value))
        if not m:
            return None
        return int(m.group(1)) * 60 + int(m.group(2))
    except Exception:
        return None


def parse_time(value):
    """Spoken or written time -> 'HH:MM' (24 h), or '' when unreadable. '11', '11:30', '11.30', '2 pm', '12am',
    'noon', 'seven in the evening', 'half past ten', 'quarter to five in the afternoon', 'six thirty pm'."""
    try:
        # 'three, sorry, four pm' -> the last stated time; am/pm said before the correction carries over ('four pm,
        # no, four thirty'); a corrected part without either keeps the old reading of the whole text
        parts = said_parts(value, False)
        s = parts[-1].strip()
        ap = r"\b(?:afternoon|evening|tonight|night|morning|noon|midday|midnight)\b|(?<![a-z])[ap]\.?m\b"
        if len(parts) > 1 and not re.search(ap, s):
            m = re.search(r"\b(?:afternoon|evening|tonight|night|morning)\b|(?<![a-z])[ap]\.?m\b", " ".join(parts[:-1]))
            s = s + " " + m.group(0) if m else str(value).lower()
        if not s:
            return ""
        # a word: 'afternoon' holds 'noon'
        if re.search(r"\b(noon|midday)\b", s):
            return "12:00"
        if "midnight" in s:
            return "00:00"
        s = day_words_to_digits(s.replace("o'clock", " ").replace("oclock", " "))
        pm = re.search(r"\b(afternoon|evening|tonight|night)\b|(?<![a-z])p\.?m\b", s)
        am = re.search(r"\bmorning\b|(?<![a-z])a\.?m\b", s)
        m = re.search(r"\b(half|quarter)\s+(past|to)\s+(\d{1,2})\b", s)
        if m:
            h, mi = int(m.group(3)), (30 if m.group(1) == "half" else 15)
            if m.group(2) == "to":
                h, mi = h - 1, 60 - mi
        else:
            m = re.search(r"(\d{1,2})(?:\s*[:.h]\s*(\d{2})|\s+(\d{2})\b)?", s)
            if not m:
                return ""
            h, mi = int(m.group(1)), int(m.group(2) or m.group(3) or 0)
        if pm and h < 12:
            h += 12
        if am and h == 12:
            h = 0
        if h > 23 or mi > 59:
            return ""
        return hhmm(h, mi)
    except Exception:
        return ""


def to_number(value):
    """'1,250.00', '420', 420.0, '€ 420' -> float, or None."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value or "").strip()
        s = re.sub(r"[^0-9.,\-]", "", s)
        if not s:
            return None
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            parts = s.split(",")
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                s = parts[0] + "." + parts[1]
            else:
                s = s.replace(",", "")
        return float(s)
    except Exception:
        return None


CORRECTION_RE = r"\b(?:sorry|i meant?|actually|wait|rather|make (?:it|that)|correction|scratch that)\b|\bno\b\s*[,.!;]|\bno no\b"
NEGATED_RE = r"\b(?:not|instead of|rather than)\s+[^,;!?]*?(?=[,;!?]|\bbut\b|$)"


def said_parts(text, neg=True):
    """What the customer said, split at corrections ('the 21st, sorry, I mean the 20th'), with negated values
    ('450, not 500') removed unless neg is False: list of lower-case parts, the last stated one last."""
    s = str(text or "").lower()
    s = re.sub(NEGATED_RE, " ", s) if neg else s
    return [p for p in re.split(CORRECTION_RE, s) if p.strip(" ,.;!?")] or [s]


def parse_amount(value):
    """An amount as written or said -> float, or None. Numbers (both separator conventions), words ('four hundred
    and fifty', 'two thousand ... five hundred', 'four fifty', 'a hundred', 'two grand', '1.5k'), mixed ('2 thousand
    500'), decimals ('point five', 'euros and twenty cents'), currency signs/words; with corrections or several
    numbers the last stated amount counts; days ('the 25th'), dates, times, periods and percentages are skipped."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        s = said_parts(value)[-1].replace("-", " ")
        # '1 250,00': a space as the thousands separator
        s = re.sub(r"(?<![\d.,])\d{1,3}(?: \d{3})+(?!\d)", lambda m: m.group(0).replace(" ", ""), s)
        s = re.sub(r"(?<=\d)\s*k\b|\s+k\b", " thousand", s.replace("€", " euro ").replace("$", " dollar ").replace("£", " pound "))
        toks = re.findall(r"\d[\d.,']*\d|\d|[a-z]+|[^\s\w]", s) + ["|"]
        mult = dict(hundred=100, thousand=1000, grand=1000, million=1000000)
        skip = ["st", "nd", "rd", "th", "percent", "%", "day", "days", "week", "weeks", "month", "months", "year", "years",
                "am", "pm", ":", "o", "oclock", "clock"] + list(ORDINAL_WORDS) + UNIT_WORDS[9:] + MONTH_NAMES + MONTH_ABBR
        groups = []
        g = None
        for i, t in enumerate(toks):
            n = None
            kind = "d"
            if t[0].isdigit():
                t = t.replace("'", "")
                # '1.250': thousands
                n = to_number(t.replace(".", "") if re.match(r"^[1-9]\d{0,2}(\.\d{3})+$", t) else t)
            elif t in NUM_WORDS:
                n = NUM_WORDS.index(t)
                kind = "u" if n < 10 else "t"
            elif t in TENS_WORDS:
                n, kind = (TENS_WORDS.index(t) + 2) * 10, "x"
            elif t == "a" and toks[i + 1] in mult:
                n, kind = 1, "u"
            if g is not None and n is not None:
                last = g["last"]
                if g["dec"] is not None:
                    dec = g["dec"][:-1] if last == "x" and kind == "u" else g["dec"]
                    g["dec"], g["last"], g["b"] = dec + (t if kind == "d" else str(n)), kind, i
                    continue
                if last == "x" and kind == "u":
                    g["cur"] += n
                elif last == "m" and n < g["m"]:
                    g["cur"] += n
                elif last in ("u", "t") and kind in ("t", "x") and g["tot"] == 0 and g["cur"] < 100:
                    # 'four fifty'
                    g["cur"] = g["cur"] * 100 + n
                else:
                    groups.append(g)
                    g = None
            if n is not None:
                if g is None:
                    g = dict(tot=0, cur=n, dec=None, last=kind, m=0, a=i, b=i)
                g["last"], g["b"] = kind, i
            elif t in mult:
                if g is None:
                    g = dict(tot=0, cur=0, dec=None, last="u", m=0, a=i, b=i)
                if g["dec"] is not None:
                    # 'one point two five thousand'
                    g["cur"] += float("0." + (g["dec"] or "0"))
                    g["dec"] = None
                if g["cur"] >= 100 and g["cur"] % 100:
                    # 'five hundred four hundred': a new amount starts at the last number
                    r = g["cur"] % 100
                    g["cur"] -= r
                    groups.append(g)
                    g = dict(tot=0, cur=r, dec=None, last="u", m=0, a=i - 1, b=i)
                if mult[t] == 100:
                    g["cur"] = (g["cur"] or 1) * 100 if g["cur"] < 100 else g["cur"]
                else:
                    g["tot"] += (g["cur"] or 1) * mult[t]
                    g["cur"] = 0
                g["last"], g["m"], g["b"] = "m", mult[t], i
            elif t == "point" and (g is None or g["dec"] is None):
                if g is None:
                    g = dict(tot=0, cur=0, dec=None, last="d", m=0, a=i, b=i)
                g["dec"] = ""
            elif g is not None and t in ("and", "uh", "um", "er", "erm", "ah", ",", ".", "…"):
                pass
            elif g is not None:
                groups.append(g)
                g = None
        vals = []
        cur = ("euro", "euros", "eur", "dollar", "dollars", "pound", "pounds")
        for g in groups:
            v = g["tot"] + g["cur"] + (float("0." + g["dec"]) if g["dec"] else 0)
            nxt = toks[g["b"] + 1]
            pv = [x for x in toks[:g["a"]] if x != "the"][-1:]
            # a day, a date, a period, a percentage or a clock time ('at 3', 'by the 25th') is not an amount
            if nxt in skip or pv and pv[0] in MONTH_NAMES + MONTH_ABBR + ["at", "by", "on", "until", "till", "before", "after"]:
                continue
            cents = nxt in ("cent", "cents", "c", "pence", "p")
            # 'twelve euros fifty', '12 euros and 50 cents': the euros, then the cents
            if vals and v < 100 and nxt not in cur and set(toks[vals[-1][1] + 1:g["a"]]) <= set(cur + ("and",)) \
                    and toks[vals[-1][1] + 1] in cur and (cents or v == int(v)):
                vals[-1] = (vals[-1][0] + v / 100, g["b"])
            else:
                vals.append((v / 100 if cents else float(v), g["b"]))
        return round(vals[-1][0], 2) if vals else (None if groups else to_number(value))
    except Exception:
        return None


def money(value):
    """Number -> '420.00' (two decimals)."""
    n = to_number(value)
    if n is None:
        return ""
    return "%.2f" % n


def speak_date(d):
    """date -> 'Tuesday 15 January 2030'."""
    try:
        return "%s %d %s %d" % (WEEKDAY_NAMES[d.weekday()].capitalize(), d.day,
                                MONTH_NAMES[d.month - 1].capitalize(), d.year)
    except Exception:
        return ""


def date_label(d):
    """date -> 'Tuesday 15 January 2030 (2030-01-15)'."""
    if not d:
        return ""
    return speak_date(d) + " (" + iso(d) + ")"


# ----------------------------------------------------------------------------------------------- calendar

def make_calendar(policy):
    """policy -> dict(weekend=[iso weekday ints], holidays=set of 'YYYY-MM-DD', roll_rule=str)."""
    cal = (policy or dict()).get("calendar") or dict()
    weekend = []
    for x in cal.get("weekend") or []:
        try:
            weekend.append(int(x))
        except Exception:
            pass
    if not cal.get("weekend") and cal.get("weekend") != []:
        weekend = [6, 7]
    holidays = set()
    for h in cal.get("holidays") or []:
        d = to_date(h)
        if d:
            holidays.add(iso(d))
    return dict(weekend=weekend, holidays=holidays,
                roll_rule=str(cal.get("roll_rule") or "next_business_day"))


def is_holiday(d, cal):
    return iso(d) in cal.get("holidays", set())


def is_business_day(d, cal):
    if d is None:
        return False
    return d.isoweekday() not in cal.get("weekend", []) and not is_holiday(d, cal)


def day_kind(d, cal):
    """'business day', 'holiday' or 'weekend'."""
    if is_holiday(d, cal):
        return "holiday"
    if d.isoweekday() in cal.get("weekend", []):
        return "weekend"
    return "business day"


def next_business_day(d, cal, include_self=False):
    cur = d if include_self else d + datetime.timedelta(days=1)
    for i in range(400):
        if is_business_day(cur, cal):
            return cur
        cur = cur + datetime.timedelta(days=1)
    return None


def previous_business_day(d, cal, include_self=False):
    cur = d if include_self else d - datetime.timedelta(days=1)
    for i in range(400):
        if is_business_day(cur, cal):
            return cur
        cur = cur - datetime.timedelta(days=1)
    return None


def nth_business_day_after(d, n, cal):
    """The n-th business day strictly after d (n >= 1). n <= 0 -> d itself if a business day, else the next one."""
    try:
        n = int(n)
    except Exception:
        n = 0
    if n <= 0:
        return next_business_day(d, cal, True)
    cur = d
    count = 0
    for i in range(2000):
        cur = cur + datetime.timedelta(days=1)
        if is_business_day(cur, cal):
            count += 1
            if count >= n:
                return cur
    return None


def roll_date(d, cal, rule=None):
    """Apply the roll rule to a non-business day. Returns d unchanged when it is a business day,
    None when the rule is 'none' and d is not a business day."""
    if d is None:
        return None
    if is_business_day(d, cal):
        return d
    rule = rule or cal.get("roll_rule") or "next_business_day"
    if rule.startswith("prev"):
        return previous_business_day(d, cal)
    if rule == "none":
        return None
    return next_business_day(d, cal)


def week_monday(d):
    return d - datetime.timedelta(days=d.weekday())


def last_business_day_of_week(d, cal):
    """Last business day of the calendar week (Monday-Sunday) containing d, or None."""
    mon = week_monday(d)
    for k in range(6, -1, -1):
        x = mon + datetime.timedelta(days=k)
        if is_business_day(x, cal):
            return x
    return None


def end_of_week_business_day(today, cal):
    """Last business day of this week; when today is that day (or it has passed), the last business day of
    next week."""
    d = last_business_day_of_week(today, cal)
    if d is None or d <= today:
        d = last_business_day_of_week(today + datetime.timedelta(days=7), cal)
    return d


def last_business_day_of_month(d, cal):
    if d.month == 12:
        first_next = datetime.date(d.year + 1, 1, 1)
    else:
        first_next = datetime.date(d.year, d.month + 1, 1)
    return previous_business_day(first_next, cal)


# ----------------------------------------------------------------------------------------------- bands, deadlines, offers

def band_for(days_late, policy):
    """The band whose [min_days_late, max_days_late] contains days_late, or None."""
    try:
        dl = int(float(days_late))
    except Exception:
        return None
    for b in (policy or dict()).get("bands") or []:
        lo = b.get("min_days_late")
        hi = b.get("max_days_late")
        try:
            if lo is not None and lo != "" and dl < int(lo):
                continue
            if hi is not None and hi != "" and dl > int(hi):
                continue
        except Exception:
            continue
        return b
    return None


def deadline_for(band, today, cal):
    """Latest allowed promise date for a band, counted from today.

    deadline.kind:
      nth_business_day  (n, counted_from): counted_from 'today' or 'tomorrow' -> the n-th business day strictly
                        after today; 'including_today' -> today counts as day 1 when it is a business day.
      calendar_days     (n, roll): today + n calendar days, rolled with 'roll' (default previous_business_day).
      fixed_date        (date)
      end_of_month      last business day of the current month.
    """
    try:
        if not band:
            return None
        dl = band.get("deadline") or dict()
        kind = str(dl.get("kind") or "nth_business_day")
        n = dl.get("n")
        try:
            n = int(n)
        except Exception:
            n = 0
        cf = str(dl.get("counted_from") or "today")
        if kind == "nth_business_day":
            if cf == "including_today" and is_business_day(today, cal):
                if n <= 1:
                    return today
                return nth_business_day_after(today, n - 1, cal)
            return nth_business_day_after(today, n, cal)
        if kind == "calendar_days":
            d = today + datetime.timedelta(days=n)
            if is_business_day(d, cal):
                return d
            return roll_date(d, cal, str(dl.get("roll") or "previous_business_day"))
        if kind == "fixed_date":
            return to_date(dl.get("date"))
        if kind == "end_of_month":
            return last_business_day_of_month(today, cal)
    except Exception:
        pass
    return None


def resolve_offer_date(expr, today, deadline, cal):
    """Attempt date expression -> date (capped at the deadline), or None when no part of it is understood.

    Supported keywords: deadline, today, tomorrow, next_business_day, end_of_week_business_day (the last business
    day of this week, or of next week when today is that day), end_of_next_week_business_day,
    end_of_month_business_day, nth_business_day:N, +N_business_days, a literal YYYY-MM-DD.
    A compound expression ('end_of_week_business_day; if today is that day, end_of_next_week_business_day') is
    resolved by its first understood part: the main case comes first, the exceptions after it."""
    try:
        raw = str(expr or "").strip().lower()
        d = offer_date_keyword(raw.replace(" ", "_"), today, deadline, cal)
        if d is None:
            for part in re.split(r"[;,()]|\bif\b|\bor\b|\botherwise\b|\bunless\b|\belse\b", raw):
                part = part.strip()
                if not part:
                    continue
                d = offer_date_keyword(part.replace(" ", "_"), today, deadline, cal)
                if d is None:
                    m = re.match(r"^[a-z0-9_:+\-]+", part)
                    if m:
                        d = offer_date_keyword(m.group(0), today, deadline, cal)
                if d is not None:
                    break
        if d is not None and deadline is not None and d > deadline:
            d = deadline
        return d
    except Exception:
        return None


def offer_date_keyword(e, today, deadline, cal):
    """One date keyword (see resolve_offer_date) -> date or None."""
    try:
        d = None
        if e in ("deadline", "the_deadline", "latest", "latest_date"):
            d = deadline
        elif e == "today":
            d = today if is_business_day(today, cal) else next_business_day(today, cal)
        elif e in ("tomorrow", "next_business_day"):
            d = next_business_day(today, cal)
        elif e in ("end_of_week_business_day", "end_of_week", "last_business_day_of_week",
                   "last_business_day_this_week"):
            d = end_of_week_business_day(today, cal)
        elif e in ("end_of_next_week_business_day", "end_of_next_week", "last_business_day_of_next_week"):
            d = last_business_day_of_week(today + datetime.timedelta(days=7), cal)
        elif e in ("end_of_month_business_day", "end_of_month", "last_business_day_of_month"):
            d = last_business_day_of_month(today, cal)
        else:
            m = re.match(r"^(?:nth_business_day[:_]?|\+)?(\d+)(?:st|nd|rd|th)?_?(?:business_days?)?$", e)
            if m and ("business" in e or e.startswith("+") or e.startswith("nth")):
                d = nth_business_day_after(today, int(m.group(1)), cal)
            elif to_date(e):
                d = to_date(e)
        return d
    except Exception:
        return None


def resolve_offer_amount(expr, full, floor):
    """Attempt amount expression -> number, or None. Supported: full, floor, a percentage of full
    ('50%', 'pct:50', never below the floor), a literal number."""
    try:
        e = str(expr or "").strip().lower()
        if e in ("full", "max", "full_amount"):
            return full
        if e in ("floor", "min", "minimum", "floor_amount"):
            return floor
        m = re.match(r"^(?:pct:|percent:)?\s*(\d+(?:\.\d+)?)\s*%?$", e)
        if m and ("%" in e or e.startswith("pct") or e.startswith("percent")):
            if full is None:
                return None
            v = round(float(full) * float(m.group(1)) / 100.0, 2)
            if floor is not None and v < floor:
                v = floor
            return v
        if re.match(r"^\d+(?:\.\d+)?$", e):
            return float(e)
        m = re.match(r"^(?:the\s+)?(full|floor|minimum|max|min)\b", e)
        if m:
            return full if m.group(1) in ("full", "max") else floor
    except Exception:
        pass
    return None


def fill_template(text, fields):
    """'card ending {last4}' + product fields -> 'card ending 1234'."""
    try:
        def rep(m):
            v = fields.get(m.group(1))
            return "" if v is None else str(v)
        return re.sub(r"\{(\w+)\}", rep, str(text or "")).strip()
    except Exception:
        return str(text or "")


def product_id_of(raw, id_field):
    if not isinstance(raw, dict):
        return ""
    if raw.get(id_field) not in (None, ""):
        return str(raw.get(id_field))
    for k in raw:
        if k.lower().endswith("id") and raw.get(k) not in (None, ""):
            return str(raw.get(k))
    return ""


def round_to(x, n, mode):
    """x rounded to n decimals: 'half_up', 'up' (ceiling) or 'down' (floor). Amounts are positive; a tiny epsilon
    absorbs binary noise such as 0.1 * 1845.3 = 184.53000000000003."""
    f = 10 ** int(n)
    v = x * f
    if mode == "half_up":
        v = int(v + 0.5 + 1e-9)
    elif mode == "up":
        v = int(v - 1e-9) + (1 if v - int(v - 1e-9) > 1e-9 else 0)
    else:
        v = int(v + 1e-9)
    return v / f


def eval_amount_expr(expr, fields):
    """A floor rule over product fields -> number, or None. Grammar: numbers, field names, + - * /, parentheses,
    min, max, abs, round (half up), round_half_up, round_up / ceil, round_down / floor; round functions take an
    optional number of decimals. No eval: a small recursive-descent parser over re tokens."""
    try:
        toks = re.findall(r"\d+(?:\.\d+)?|[A-Za-z_]\w*|[-+*/(),]", str(expr or ""))
        pos = [0]

        def peek():
            return toks[pos[0]] if pos[0] < len(toks) else ""

        def take():
            pos[0] += 1
            return toks[pos[0] - 1]

        def expr_():
            v = term()
            while peek() in ("+", "-"):
                v = v + term() if take() == "+" else v - term()
            return v

        def term():
            v = factor()
            while peek() in ("*", "/"):
                v = v * factor() if take() == "*" else v / factor()
            return v

        def factor():
            t = take()
            if t == "-":
                return -factor()
            if t == "(":
                v = expr_()
                take()
                return v
            if re.match(r"\d", t):
                return float(t)
            if peek() == "(":
                take()
                args = [expr_()]
                while peek() == ",":
                    take()
                    args.append(expr_())
                take()
                f = t.lower()
                nd = args[1] if len(args) > 1 else 0
                if f == "min":
                    return min(args)
                if f == "max":
                    return max(args)
                if f == "abs":
                    return abs(args[0])
                if f in ("round", "round_half_up"):
                    return round_to(args[0], nd, "half_up")
                if f in ("round_up", "ceil", "ceiling"):
                    return round_to(args[0], nd, "up")
                if f in ("round_down", "floor", "trunc"):
                    return round_to(args[0], nd, "down")
                raise ValueError(f)
            v = to_number(fields.get(t))
            if v is None:
                raise ValueError(t)
            return v
        v = expr_()
        return round(v, 2) if pos[0] == len(toks) else None
    except Exception:
        return None


def build_product(raw, index, policy, today, cal):
    """One product of get_account -> dict with id, name, full, floor, days_late, band, deadline, offers."""
    prod = policy.get("products") or dict()
    types = prod.get("types") or dict()
    id_field = str(prod.get("id_field") or "product_id")
    type_field = str(prod.get("type_field") or "type")
    days_field = str(prod.get("days_late_field") or "days_late")
    ptype = str(raw.get(type_field) or "")
    tinfo = types.get(ptype) or dict()
    full_field = tinfo.get("full_field") or prod.get("full_field") or ""
    floor_field = tinfo.get("floor_field") or prod.get("floor_field") or ""
    full = to_number(raw.get(full_field)) if full_field else None
    floor = to_number(raw.get(floor_field)) if floor_field else None
    floor_expr = tinfo.get("floor_expr") or prod.get("floor_expr") or ""
    if floor is None and floor_expr:
        floor = eval_amount_expr(floor_expr, raw)
    if floor is None and full is not None:
        floor = full
    name = fill_template(tinfo.get("speak_as") or ptype.replace("_", " "), raw)
    dl_raw = raw.get(days_field)
    band = band_for(dl_raw, policy)
    deadline = deadline_for(band, today, cal) if band else None
    offers = []
    if band:
        for a in band.get("attempts") or []:
            amt = resolve_offer_amount(a.get("amount"), full, floor)
            d = resolve_offer_date(a.get("date"), today, deadline, cal)
            ae = str(a.get("amount") or "").lower()
            alt = None
            if amt is not None and amt == full and floor is not None and floor != full and "floor" in ae:
                alt = floor
            elif amt is not None and amt == floor and full is not None and floor != full and "full" in ae and not ae.startswith("floor"):
                alt = full
            offers.append(dict(n=a.get("n"), amount=amt, alt_amount=alt, date=iso(d), amount_expr=str(a.get("amount") or ""),
                               date_expr=str(a.get("date") or ""), text=str(a.get("text") or "")))
    try:
        days_late = int(float(dl_raw))
    except Exception:
        days_late = None
    return dict(index=index + 1, id=product_id_of(raw, id_field), type=ptype, name=name, full=full, floor=floor,
                full_field=full_field, floor_field=floor_field or floor_expr, days_late=days_late,
                band=str((band or dict()).get("name") or ""), deadline=iso(deadline),
                attempts=len(offers), offers=offers, raw=raw)


def products_from_value(value):
    """A mapped products value (list, JSON string, or python-repr string) -> list of dicts."""
    try:
        if isinstance(value, list):
            return [p for p in value if isinstance(p, dict)]
        if isinstance(value, dict):
            for k in value:
                if isinstance(value[k], list):
                    return products_from_value(value[k])
            return []
        if isinstance(value, str) and value.strip():
            s = value.strip()
            try:
                return products_from_value(json.loads(s))
            except Exception:
                pass
            s2 = s.replace("'", '"').replace("True", "true").replace("False", "false").replace("None", "null")
            try:
                return products_from_value(json.loads(s2))
            except Exception:
                return []
    except Exception:
        pass
    return []


def products_from_vars(vs, names, list_field):
    """Find the product list in the variable table: first under each name in `names`, then in flattened keys
    such as '<x>.data.<list_field>.0.<field>' or '<x>.<list_field>[0].<field>'."""
    try:
        for n in names:
            if n in vs:
                got = products_from_value(vs.get(n))
                if got:
                    return got
        rows = dict()
        pat = re.compile(r"(?:^|\.)" + re.escape(list_field) + r"(?:\.|\[)(\d+)\]?\.(\w+)$")
        for k in list(vs.keys()):
            m = pat.search(str(k))
            if m:
                i = int(m.group(1))
                if i not in rows:
                    rows[i] = dict()
                rows[i][m.group(2)] = vs.get(k)
        return [rows[i] for i in sorted(rows)]
    except Exception:
        return []


# ----------------------------------------------------------------------------------------------- spoken dates

def month_from_word(w):
    w = str(w or "").lower().strip(".")
    if w in MONTH_NAMES:
        return MONTH_NAMES.index(w) + 1
    if w[:3] in MONTH_ABBR and len(w) >= 3:
        return MONTH_ABBR.index(w[:3]) + 1
    return None


def weekday_from_word(w):
    w = str(w or "").lower()
    if w in WEEKDAY_NAMES:
        return WEEKDAY_NAMES.index(w)
    if w[:3] in WEEKDAY_ABBR and len(w) >= 3:
        return WEEKDAY_ABBR.index(w[:3])
    return None


def safe_date(y, m, d):
    try:
        return datetime.date(int(y), int(m), int(d))
    except Exception:
        return None


def full_year(y):
    y = int(y)
    if y < 100:
        y += 2000 if y < 50 else 1900
    return y


def resolve_weekday(wd, today, mode, next_rule):
    """wd: 0=Monday. mode 'this' -> that weekday in the current week; 'next' -> following calendar week when
    next_rule is 'following_week', else the next occurrence strictly after today; '' -> next occurrence on or
    after today."""
    mon = week_monday(today)
    if mode == "this":
        d = mon + datetime.timedelta(days=wd)
        return d if d >= today else d + datetime.timedelta(days=7)
    if mode == "next":
        if next_rule == "following_week":
            return mon + datetime.timedelta(days=7 + wd)
        delta = (wd - today.weekday()) % 7
        return today + datetime.timedelta(days=delta or 7)
    delta = (wd - today.weekday()) % 7
    return today + datetime.timedelta(days=delta)


def parse_spoken_date(text, today, next_rule="", future=True, order=""):
    """A date as a customer or model says it -> date, or None.

    Handles ISO dates, numeric dates, '14 March 1988', 'March 14th, 1988', 'the 26th', 'Monday the 26th', 'today',
    'tomorrow', 'this/next Friday'. Numeric dates follow `order` ('dmy' or 'mdy', the other reading only when the
    first is impossible); with no order an ambiguous numeric date (both readings valid and different) gives None.
    'next <weekday>' is resolved only when the policy says what it means (next_rule).
    Without a year, `future=True` picks the next occurrence on or after today. 'a week from today', 'in a fortnight',
    'Friday next week'. Corrections and more relative forms: said_date."""
    try:
        s = str(text or "").strip().lower()
        if not s:
            return None
        d = to_date(s)
        if d:
            return d
        s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s)
        s = day_words_to_digits(s)
        s = re.sub(r"\bthe\b", " ", s).replace(",", " ").replace(" of ", " ")
        if today is not None:
            if re.search(r"\btomorrow\b", s):
                return today + datetime.timedelta(days=2 if re.search(r"\bday after tomorrow", s) else 1)
            # 'a week from today', 'in a fortnight'
            s = re.sub(r"\b(\d+|an?)\s+(day|week|month)s?\b(?:\s+(?:from\s+)?(?:today|now)\b)?", r"in \1 \2", re.sub(r"\b(an?\s+)?fortnight", "2 weeks", s))
            # 'in about five weeks'
            m = re.search(r"\bin\s+(?:about\s+|around\s+|roughly\s+)?(\d+|an?)\s+(day|week|month)s?\b", s)
            if m:
                k = 1 if m.group(1) in ("a", "an") else int(m.group(1))
                return today + datetime.timedelta(days=k * dict(day=1, week=7, month=30)[m.group(2)])
            if re.search(r"\btoday\b", s):
                return today
        m = re.search(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})\b", s)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), full_year(m.group(3))
            dmy = safe_date(y, b, a)
            mdy = safe_date(y, a, b)
            if order == "mdy":
                return mdy or dmy
            if order == "dmy":
                return dmy or mdy
            if dmy and mdy and dmy != mdy:
                return None
            return dmy or mdy
        m = re.search(r"\b(\d{1,2})\s+([a-z]+)\.?(?:\s+(\d{2,4}))?\b", s)
        if m and month_from_word(m.group(2)):
            day, mon = int(m.group(1)), month_from_word(m.group(2))
            if m.group(3):
                return safe_date(full_year(m.group(3)), mon, day)
            return pick_year(day, mon, today, future)
        m = re.search(r"\b([a-z]+)\.?\s+(\d{1,2})(?:\s+(\d{4}))?\b", s)
        if m and month_from_word(m.group(1)):
            day, mon = int(m.group(2)), month_from_word(m.group(1))
            if m.group(3):
                return safe_date(int(m.group(3)), mon, day)
            return pick_year(day, mon, today, future)
        if today is None:
            return None
        wd = None
        mode = ""
        m = re.search(r"\b(this|next|coming)?\s*([a-z]+day)\b", s)
        if m and weekday_from_word(m.group(2)) is not None:
            wd = weekday_from_word(m.group(2))
            mode = "next" if m.group(1) == "next" else ("this" if m.group(1) == "this" else "")
            # 'Friday next week': the following calendar week
            if re.search(r"\bnext\s+week\b", s):
                mode, next_rule = "next", "following_week"
        m2 = re.search(r"\b(?:the\s+)?(\d{1,2})\b", s)
        if m2:
            day = int(m2.group(1))
            cand = None
            for k in range(0, 13):
                y = today.year + (today.month - 1 + k) // 12
                mo = (today.month - 1 + k) % 12 + 1
                c = safe_date(y, mo, day)
                if c and c >= today and (wd is None or c.weekday() == wd):
                    cand = c
                    break
            if cand:
                return cand
        if wd is not None:
            if mode == "next" and next_rule not in ("following_week", "next_occurrence"):
                return None
            return resolve_weekday(wd, today, mode, next_rule)
    except Exception:
        pass
    return None


def relative_date(text, today, cal=None):
    """Relative days parse_spoken_date leaves out (kept out of it for the create_promise hook-size budget) -> date,
    or None: 'end of the month' / 'month end' / 'end of (next) week' (the last business day when `cal` is given,
    else the last day / Friday), 'this time next week', 'the 5th of next month'. '' for a day that does not exist
    ('the 31st of next month' in a 30-day month)."""
    try:
        s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", day_words_to_digits(str(text or "").lower()))
        s = " ".join(re.sub(r"\b(the|of)\b|,", " ", s).split())
        s = re.sub(r"\b(this |next )?(week|month)[ -]end\b", r"end \1\2", s)
        if re.search(r"\b(this time|same day) next week\b", s):
            return today + datetime.timedelta(days=7)
        m = re.search(r"\bend (this |next )?(week|month)\b", s)
        if m:
            k = int(m.group(1) == "next ")
            for j in (k, k + 1):
                # said after the period's last business day (a weekend): the next one
                y, mo = divmod(today.month + j, 12)
                e = week_monday(today) + datetime.timedelta(days=7 * j + 7) if m.group(2) == "week" else safe_date(today.year + y, mo + 1, 1)
                d = previous_business_day(e, cal) if cal else e - datetime.timedelta(days=3 if m.group(2) == "week" else 1)
                if d >= today:
                    return d
        m = re.search(r"\b(\d{1,2}) (this|next) month\b|\b(this|next) month (?:on )?(\d{1,2})\b", s)
        if m:
            y, mo = divmod(today.month - 1 + int("next" in (m.group(2), m.group(3))), 12)
            return safe_date(today.year + y, mo + 1, int(m.group(1) or m.group(4))) or ""
    except Exception:
        pass
    return None


def said_date(text, today, next_rule="", cal=None, future=True, order=""):
    """A day as the customer said it, with corrections ('Friday, sorry, I mean Monday', 'the 20th, not the 21st':
    the last stated day that can be read) and every relative form (relative_date, then parse_spoken_date)."""
    try:
        parts = said_parts(text)
        ds = []
        for p in parts:
            d = relative_date(p, today, cal) if today else None
            ds.append(parse_spoken_date(p, today, next_rule, future, order) if d is None else d)
        prev = [d for d in ds[:-1] if d]
        if ds[-1] or not prev:
            return ds[-1] or None
        # only a piece was corrected ('14 March 1988, no, 1989'; '3rd of March, sorry, the 4th'): put it into the
        # day said before; anything else that cannot be read gives None
        p = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", day_words_to_digits(parts[-1]))
        y = re.search(r"\b(1[89]\d\d|20\d\d)\b", p)
        dd = re.search(r"\b([12]?\d|3[01])\b", re.sub(r"\b(1[89]\d\d|20\d\d)\b", " ", p))
        mo = [month_from_word(w) for w in re.findall(r"[a-z]+", p) if month_from_word(w)]
        if not (y or dd or mo):
            return None
        d = prev[-1]
        return safe_date(int(y.group(1)) if y else d.year, mo[0] if mo else d.month, int(dd.group(1)) if dd else d.day)
    except Exception:
        return None


def pick_year(day, mon, today, future):
    if today is None:
        return None
    d = safe_date(today.year, mon, day)
    if d and future and d < today:
        d = safe_date(today.year + 1, mon, day)
    return d


def day_words_to_digits(s):
    """Number words in a date -> digits (lower-case text): 'nineteen eighty-eight' -> '1988', 'twenty-sixth' / 'twenty six'
    -> '26', 'sixth' -> '6', 'fourteen' -> '14'. Years first ('twenty twenty-six' is a year, not a day), then compounds
    ('sixth' alone would leave 'twenty-6', read as the 6th), then single words."""
    u = "|".join(UNIT_WORDS)

    def unit(w):
        return UNIT_WORDS.index(w) % 9 + 1 if w else 0

    def two(w):
        return NUM_WORDS.index(w) if w in NUM_WORDS else (TENS_WORDS.index(w) + 2) * 10
    s = re.sub(r"\btwo thousand(?:\s+and)?(?:\s+(" + "|".join(NUM_WORDS[1:]) + r"))?\b",
               lambda m: str(2000 + (NUM_WORDS.index(m.group(1)) if m.group(1) else 0)), s)
    s = re.sub(r"\b(nineteen|twenty)[\s-]+(" + "|".join(NUM_WORDS[10:] + TENS_WORDS) + r")(?:[\s-]+(" + u + r"))?\b",
               lambda m: str((19 if m.group(1) == "nineteen" else 20) * 100 + two(m.group(2)) + unit(m.group(3))), s)
    s = re.sub(r"\b(twenty|thirty)[\s-]+(" + u + r")\b", lambda m: str(two(m.group(1)) + unit(m.group(2))), s)
    for w in ORDINAL_WORDS:
        s = re.sub(r"\b" + w + r"\b", str(ORDINAL_WORDS[w]), s)
    for w in NUM_WORDS[1:] + TENS_WORDS[:2]:
        s = re.sub(r"\b" + w + r"\b", str(two(w)), s)
    return s


def date_piece(text):
    """What part of a date the text holds when it is not a whole date: 'day and month', 'year', 'day', 'month',
    'day and year', 'month and year'; '' when it holds no date part."""
    try:
        s = str(text or "").lower()
        s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s)
        s = day_words_to_digits(s)
        has_year = bool(re.search(r"\b(1[89]\d\d|20\d\d)\b", s))
        has_month = any(month_from_word(w) for w in re.findall(r"[a-z]+", s))
        rest = re.sub(r"\b(1[89]\d\d|20\d\d)\b", " ", s)
        has_day = bool(re.search(r"\b([12]?\d|3[01])\b", rest))
        nums = re.findall(r"\b\d{1,2}\b", rest)
        if not has_month and len(nums) >= 2:
            has_month = True
        parts = [x for x, ok in (("day", has_day), ("month", has_month), ("year", has_year)) if ok]
        return " and ".join(parts)
    except Exception:
        return ""


def expected_digits(text):
    """'the last four digits' / '4 digits' in a question text -> 4, else 0."""
    words = dict(one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10)
    m = re.search(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b[\s-]+(?:\w+\s+)?digits?\b", str(text or "").lower())
    if not m:
        return 0
    g = m.group(1)
    return int(g) if g.isdigit() else words.get(g, 0)


def fix_year(d, today):
    """A date the model wrote with a wrong year (in the past, or more than a year ahead) -> the same day and month
    on or after today; any other date unchanged."""
    try:
        if d is None or today is None:
            return d
        if d >= today and d.year <= today.year + 1:
            return d
        if d.year == today.year:
            return d
        for y in (today.year, today.year + 1):
            c = safe_date(y, d.month, d.day)
            if c is not None and c >= today:
                return c
    except Exception:
        pass
    return d


def spoken_digits(text):
    """Digits said as words -> digits: 'four four seven one', 'double four seven one', 'oh three five eight',
    'forty-four seventy-one' -> '4471' / '0358'. Text that already holds digits keeps them."""
    # 'the last four digits are ...'
    s = re.sub(r"\blast\s+\w+\s+digits?\b|\bdigits?\b", " ", str(text or "").lower())
    s = re.sub(r"\b(double|triple)\s+(\w+)", lambda m: " ".join([m.group(2)] * (2 if m.group(1) == "double" else 3)), s)
    s = day_words_to_digits(re.sub(r"\b(oh|o|nought|nil)\b", "zero", s).replace("zero", "0"))
    # forty .. ninety, alone or with a unit ('forty-four')
    for w in TENS_WORDS[2:]:
        s = re.sub(r"\b" + w + r"(?:[\s-]+(\d))?\b", lambda m: str((TENS_WORDS.index(w) + 2) * 10 + int(m.group(1) or 0)), s)
    # 'twelve hundred', 'four hundred seventy one', 'four hundred and five'; 'one hundred two three' stays 123
    return re.sub(r"(\d+)\s+hundred\b(?:\s+and\s+(\d\d?)\b|\s+(\d\d)\b(?!\s*\d)|(?!\s*(?:and\s+)?\d))",
                  lambda m: str(int(m.group(1)) * 100 + int(m.group(2) or m.group(3) or 0)), s)


def normalise_answer(answer, how, answer_format=""):
    """Identity answer -> the form the tool expects, as the policy says.
    dmy_to_iso / mdy_to_iso: any spoken or numeric date -> YYYY-MM-DD, numbers read in that order;
    digits_only: keep the digits; otherwise trimmed text, except that a date answer written unambiguously
    (words, ISO, or numbers with only one possible reading) is still sent as YYYY-MM-DD."""
    s = str(answer if answer is not None else "").strip()
    how = str(how or "")
    if how in ("dmy_to_iso", "mdy_to_iso"):
        d = said_date(s, None, future=False, order=how[:3])
        return iso(d) if d else s
    if how == "digits_only":
        digits = re.sub(r"\D", "", spoken_digits(s))
        return digits or s
    if answer_format == "date":
        d = said_date(s, None, future=False)
        return iso(d) if d else s
    return s


# ----------------------------------------------------------------------------------------------- checks

def check_promise(product, amount, date_value, today, cal, already=None, next_rule=""):
    """Check one promise against the policy facts of its product.
    Returns dict(ok, code, message, date, amount, suggest_date, suggest_amount)."""
    out = dict(ok=False, code="", message="", date="", amount=None, suggest_date="", suggest_amount=None)
    try:
        if not product:
            out["code"] = "unknown_product"
            out["message"] = "Unknown product id. Use a product_id exactly as listed in the account facts."
            return out
        if already and product.get("id") in already:
            out["code"] = "already_promised"
            out["message"] = "A promise is already recorded for this product. It cannot be changed in this call."
            return out
        amt = to_number(amount)
        d = parse_spoken_date(date_value, today, next_rule)
        out["amount"] = amt
        out["date"] = iso(d)
        full = product.get("full")
        floor = product.get("floor")
        if amt is None or amt <= 0:
            out["code"] = "bad_amount"
            out["message"] = "The amount is missing or not a number."
            return out
        if floor is not None and amt < floor - 0.004:
            out["code"] = "below_floor"
            out["suggest_amount"] = floor
            out["message"] = "The amount " + money(amt) + " is below the lowest amount that can be recorded (" + money(floor) + ")."
            return out
        if full is not None and amt > full + 0.004:
            out["code"] = "above_full"
            out["suggest_amount"] = full
            out["message"] = "The amount " + money(amt) + " is more than the full past-due amount (" + money(full) + ")."
            return out
        if d is None:
            out["code"] = "bad_date"
            out["message"] = "The date is missing or unreadable. Send it as YYYY-MM-DD."
            return out
        deadline = to_date(product.get("deadline"))
        if today is not None and d < today:
            out["code"] = "before_today"
            out["suggest_date"] = iso(today if is_business_day(today, cal) else next_business_day(today, cal))
            out["message"] = "The date " + date_label(d) + " is in the past."
            return out
        if deadline is not None and d > deadline:
            out["code"] = "after_deadline"
            out["suggest_date"] = iso(deadline)
            out["message"] = ("The date " + date_label(d) + " is after the latest date that can be recorded for this product, "
                              + date_label(deadline) + ".")
            return out
        if not is_business_day(d, cal):
            rolled = roll_date(d, cal)
            if rolled is not None and deadline is not None and rolled > deadline:
                rolled = deadline if is_business_day(deadline, cal) else previous_business_day(deadline, cal)
            out["code"] = "not_business_day"
            out["suggest_date"] = iso(rolled)
            out["message"] = "The date " + date_label(d) + " is a " + day_kind(d, cal) + ", not a business day."
            if rolled is not None:
                out["message"] += " The next possible date is " + date_label(rolled) + "."
            return out
        out["ok"] = True
        out["code"] = "ok"
        return out
    except Exception:
        out["code"] = "error"
        return out


def callback_windows(today, now_time, policy, cal):
    """Allowed callback windows -> list of dict(date, start, end, label). Uses policy.callback:
    days (today, tomorrow, next_business_day, business_days:N), start, end, end_inclusive, must_be_after_now."""
    out = []
    try:
        cb = (policy or dict()).get("callback") or dict()
        start = str(cb.get("start") or "")
        end = str(cb.get("end") or "")
        seen = set()
        for day in cb.get("days") or []:
            day = str(day)
            ds = []
            if day == "today":
                ds = [today]
            elif day == "tomorrow":
                ds = [today + datetime.timedelta(days=1)]
            elif day == "next_business_day":
                ds = [next_business_day(today, cal)]
            elif day.startswith("business_days:"):
                try:
                    k = int(day.split(":")[1])
                except Exception:
                    k = 0
                cur = today
                for i in range(k):
                    cur = next_business_day(cur, cal)
                    ds.append(cur)
            elif to_date(day):
                ds = [to_date(day)]
            for d in ds:
                if d is None or iso(d) in seen:
                    continue
                seen.add(iso(d))
                s = start
                if cb.get("must_be_after_now", True) and d == today and now_time:
                    if time_to_minutes(now_time) is not None and (time_to_minutes(s) is None or time_to_minutes(now_time) >= time_to_minutes(s)):
                        s = "after " + now_time
                out.append(dict(date=iso(d), start=start, end=end, label=date_label(d) + ": " + s + " to " + end
                                + (" (inclusive)" if cb.get("end_inclusive", True) else " (exclusive)")))
    except Exception:
        pass
    return out


def check_callback(date_value, time_value, today, now_time, policy, cal, next_rule=""):
    """Is a callback at this date and time allowed? -> dict(ok, code, message, date, time)."""
    out = dict(ok=False, code="", message="", date="", time="")
    try:
        cb = (policy or dict()).get("callback") or dict()
        d = to_date(date_value) or said_date(date_value, today, next_rule, cal)
        t = parse_time(time_value)
        out["date"] = iso(d)
        out["time"] = t
        wins = callback_windows(today, now_time, policy, cal)
        allowed = "; ".join([w["label"] for w in wins])
        if d is None or not t:
            out["code"] = "unreadable"
            out["message"] = "Date or time missing or unreadable. Send the date as YYYY-MM-DD and the time as HH:MM."
            return out
        match = [w for w in wins if w["date"] == iso(d)]
        if not match:
            out["code"] = "day_not_allowed"
            out["message"] = "A callback on " + date_label(d) + " is not allowed. Allowed: " + allowed + "."
            return out
        tm = time_to_minutes(t)
        st = time_to_minutes(cb.get("start"))
        en = time_to_minutes(cb.get("end"))
        if st is not None and tm < st:
            out["code"] = "too_early"
        elif en is not None and (tm > en or (tm == en and not cb.get("end_inclusive", True))):
            out["code"] = "too_late"
        elif cb.get("must_be_after_now", True) and today is not None and d == today and now_time and \
                time_to_minutes(now_time) is not None and tm <= time_to_minutes(now_time):
            out["code"] = "not_after_now"
        if out["code"]:
            out["message"] = "A callback at " + t + " on " + date_label(d) + " is outside the allowed window. Allowed: " + allowed + "."
            return out
        out["ok"] = True
        out["code"] = "ok"
        return out
    except Exception:
        out["code"] = "error"
        return out


# ----------------------------------------------------------------------------------------------- closing

def pair(x):
    if isinstance(x, dict):
        o = x.get("outcome")
        r = x.get("reason")
        return dict(outcome=str(o) if o else "", reason=str(r) if r else "")
    return dict(outcome="", reason="")


def pre_verification_outcomes(cl):
    """Outcomes that only close a call before identity is verified (failed check, someone else answered), minus
    any outcome the closing table also uses after verification."""
    pre = set([pair(cl.get(k))["outcome"] for k in ("id_fail", "callback", "no_callback")]
              + [str(o) for o in (cl.get("pre_verification_outcomes") or [])])
    post = [pair(cl.get(k))["outcome"] for k in ("promise", "promise_mixed", "refused", "service_error")]
    post += [pair(v)["outcome"] for v in list((cl.get("transfer") or dict()).values()) + list((cl.get("note_map") or dict()).values())]
    return set([o for o in pre if o and o not in post])


def compute_closing(facts, policy):
    """Outcome and reason for close_call from what happened in the call.

    facts: verified (bool), id_failed (bool), transfers ([queue] in call order), promises ([product id]),
           notes ({product id: note reason}), products ([product id] in get_account order),
           callback_done (bool), llm (dict(outcome, reason) chosen by the model, optional).
    policy['closing'] (normalised by policy_schema): promise, id_fail, callback, refused, service_error,
           transfer {queue: pair}, note_map {note reason: pair}, transfer_overrides_promise, no_reason_outcomes.
    Returns dict(outcome, reason, source). source 'llm' means the facts did not decide."""
    cl = (policy or dict()).get("closing") or dict()
    res = dict(outcome="", reason="", source="")
    try:
        transfers = [q for q in (facts.get("transfers") or []) if q]
        promises = [p for p in (facts.get("promises") or []) if p]
        notes = facts.get("notes") or dict()
        products = [p for p in (facts.get("products") or []) if p]
        tmap = cl.get("transfer") or dict()
        if transfers and (not promises or cl.get("transfer_overrides_promise", True)):
            p = pair(tmap.get(transfers[-1]))
            if p["outcome"]:
                res.update(p)
                res["source"] = "transfer"
        if not res["source"] and facts.get("id_failed") and not facts.get("verified"):
            p = pair(cl.get("id_fail"))
            if p["outcome"]:
                res.update(p)
                res["source"] = "id_fail"
        if not res["source"] and promises:
            p = pair(cl.get("promise"))
            mixed = pair(cl.get("promise_mixed"))
            other = [r for pid, r in notes.items() if pid not in promises and r not in (cl.get("promise_mixed_except") or [])]
            if mixed["outcome"] and other:
                p = mixed
            if p["outcome"]:
                res.update(p)
                res["source"] = "promise"
        if not res["source"] and facts.get("callback_done") and not facts.get("verified"):
            p = pair(cl.get("callback"))
            if p["outcome"]:
                res.update(p)
                res["source"] = "callback"
        if not res["source"] and notes and products and all(p in notes for p in products):
            nmap = cl.get("note_map") or dict()
            mapped = [pair(nmap.get(notes[p])) for p in products]
            if all(m["outcome"] for m in mapped):
                keys = [m["outcome"] + "/" + m["reason"] for m in mapped]
                if len(set(keys)) == 1:
                    res.update(mapped[0])
                else:
                    default_outcome = pair(cl.get("refused"))["outcome"]
                    cands = [(i, m, notes[products[i]]) for i, m in enumerate(mapped)
                             if default_outcome and m["outcome"] == default_outcome]
                    order = cl.get("note_severity") or []
                    ranked = [c for c in cands if c[2] in order]
                    if ranked:
                        ranked.sort(key=lambda c: order.index(c[2]))
                        pick = ranked[0][1]
                    else:
                        pick = cands[0][1] if cands else None
                    res.update(pick or mapped[0])
                res["source"] = "notes"
        if not res["source"]:
            llm = pair(facts.get("llm"))
            if llm["outcome"] and facts.get("verified") and llm["outcome"] in pre_verification_outcomes(cl):
                # identity passed: an outcome of a failed check or of someone else answering cannot apply
                llm = pair(None)
            if llm["outcome"]:
                res.update(llm)
                res["source"] = "llm"
            elif facts.get("verified"):
                res.update(pair(cl.get("refused")))
                res["source"] = "default"
        if res["outcome"] in (cl.get("no_reason_outcomes") or []):
            res["reason"] = ""
    except Exception:
        pass
    return res


# ----------------------------------------------------------------------------------------------- fact texts

def date_facts_text(policy, today, now_time, cal):
    """Multi-line block of date facts for the prompts."""
    try:
        lines = []
        nr = str(((policy or dict()).get("dates") or dict()).get("next_weekday") or "")
        lines.append("Today is " + date_label(today) + (", time " + now_time if now_time else "") + ". "
                     + ("Today is a business day." if is_business_day(today, cal) else "Today is NOT a business day (" + day_kind(today, cal) + ")."))
        wk = [WEEKDAY_NAMES[i - 1].capitalize() for i in sorted(cal.get("weekend") or [])]
        hol = sorted(cal.get("holidays") or [])
        lines.append("Business days: every day except " + (", ".join(wk) if wk else "no weekend days")
                     + (" and these holidays: " + ", ".join([date_label(to_date(h)) for h in hol]) if hol else "") + ".")
        lines.append("Next business day after today: " + date_label(next_business_day(today, cal)) + ".")
        lines.append("Last business day of this week (or of next week if that is today): " + date_label(end_of_week_business_day(today, cal)) + ".")
        mon = week_monday(today)
        horizon = 42
        for b in (policy or dict()).get("bands") or []:
            dl = deadline_for(b, today, cal)
            if dl is not None:
                horizon = max(horizon, (dl - today).days + 7)
        horizon = min(horizon, 70)
        lines.append("Calendar by week (Monday to Sunday):")
        last = today + datetime.timedelta(days=horizon)
        wk = mon
        while wk <= last:
            off = (wk - mon).days // 7
            tag = "This week" if off == 0 else ("Next week" if off == 1 else "In " + str(off) + " weeks")
            busy = []
            off_days = []
            for k in range(7):
                d = wk + datetime.timedelta(days=k)
                if d < today:
                    continue
                lab = WEEKDAY_ABBR[d.weekday()].capitalize() + " " + str(d.day) + " " + MONTH_ABBR[d.month - 1].capitalize()
                if d.year != today.year:
                    lab += " " + str(d.year)
                if d == today:
                    lab += " (today)"
                if is_business_day(d, cal):
                    busy.append(lab)
                else:
                    off_days.append(lab + (" holiday" if is_holiday(d, cal) else ""))
            lines.append("  " + tag + ": business days " + (", ".join(busy) or "none")
                         + ("; not business days " + ", ".join(off_days) if off_days else "") + ".")
            wk = wk + datetime.timedelta(days=7)
        # the replacement day for a non-business day depends on the product's deadline: listed per product in the
        # account facts (a list here, without deadlines, led the agent to offer a day after the deadline)
        lines.append("A day the customer names that is not a business day is not a refusal: each product's facts list the day to offer instead,"
                     " and what to do for a day after its deadline.")
        if nr:
            lines.append("Weekday words: 'this <weekday>' is that day in the current week; 'next <weekday>' is "
                         + ("that day in the following calendar week." if nr == "following_week" else "the next such day after today."))
        return "\n".join(lines)
    except Exception:
        return "Today: " + iso(today)


def offer_text(o):
    amt = o.get("amount")
    parts = ["Attempt " + str(o.get("n")) + " (a persuasion attempt: only after a refusal or a non-answer):"]
    if amt is not None:
        parts.append("amount " + money(amt))
        if o.get("alt_amount") is not None:
            parts.append("(or " + money(o.get("alt_amount")) + "; amount rule: " + str(o.get("amount_expr")) + ")")
    elif o.get("amount_expr"):
        parts.append("amount: " + o.get("amount_expr"))
    if o.get("date"):
        parts.append("on " + date_label(to_date(o.get("date"))))
    elif o.get("date_expr"):
        parts.append("date: " + o.get("date_expr"))
    t = " ".join(parts)
    if o.get("text"):
        t += ". Rule: " + o.get("text")
    return t


def replacement_days_text(today, deadline, cal):
    """'Sat 6 Jan -> Monday 8 January 2030 (2030-01-08); ...' for every day from today to the deadline that is not
    a business day and whose replacement day is not after the deadline."""
    out = []
    if not today or not deadline:
        return ""
    for k in range(0, (deadline - today).days + 1):
        d = today + datetime.timedelta(days=k)
        if is_business_day(d, cal):
            continue
        r = roll_date(d, cal)
        if r is None or r > deadline:
            continue
        lab = WEEKDAY_ABBR[d.weekday()].capitalize() + " " + str(d.day) + " " + MONTH_ABBR[d.month - 1].capitalize()
        out.append(lab + (" (holiday)" if is_holiday(d, cal) else "") + " -> " + date_label(r))
    return "; ".join(out)


def account_facts_text(products, cal, today=None):
    """Multi-line block of per-product facts for the prompts."""
    try:
        if not products:
            return "The account could not be loaded. No product facts are available."
        lines = ["Late products, in the order to handle them (" + str(len(products)) + " in total):"]
        for p in products:
            lines.append(str(p.get("index")) + ". product_id " + str(p.get("id")) + ": " + str(p.get("name")) + ".")
            lines.append("   Past-due amount (full amount): " + money(p.get("full")) + ". Days late: " + str(p.get("days_late")) + ".")
            if p.get("floor") is not None:
                lines.append("   Floor (lowest amount that can be recorded, field " + str(p.get("floor_field")) + "): " + money(p.get("floor")) + "."
                             + " Offer the floor only when the customer says the amount itself is too much; a plain no or 'I can't' is a refusal,"
                             + " not that. Offering the floor for an amount that is too much does not change the day being discussed.")
            if p.get("band"):
                lines.append("   Band: " + p.get("band") + ". Persuasion attempts allowed: " + str(p.get("attempts")) + ".")
            if p.get("deadline"):
                lines.append("   Deadline (latest promise date): " + date_label(to_date(p.get("deadline"))) + ".")
            for o in p.get("offers") or []:
                lines.append("   " + offer_text(o))
            if p.get("offers"):
                lines.append("   After attempt " + str(len(p.get("offers"))) + " is refused, make no other offer for this product: no other amount"
                             + " (not the floor either), no other day.")
            if p.get("deadline"):
                lines.append("   A promise for " + str(p.get("id")) + " is valid with an amount from " + money(p.get("floor"))
                             + " to " + money(p.get("full")) + " and a business day from today to " + str(p.get("deadline")) + "."
                             + " A day the customer names on or before " + str(p.get("deadline")) + " is inside the deadline;"
                             + " if it is not a business day, offer the replacement day from the calendar facts.")
                rep = replacement_days_text(today, to_date(p.get("deadline")), cal)
                if rep:
                    lines.append("   Days up to the deadline that are not business days, and the one day to offer instead (offer exactly"
                                 + " that day; say only that the named day is not a business day, never call it a public or bank holiday): " + rep + ".")
                lines.append("   Any day after " + date_label(to_date(p.get("deadline"))) + ", a weekend day or holiday too, is after the deadline:"
                             + " do not move it to a nearby business day; say the latest date you can record is the deadline and offer it.")
        return "\n".join(lines)
    except Exception:
        return "The account facts could not be prepared."


def load_json(value, default):
    try:
        if isinstance(value, (list, dict)):
            return value
        if value is None or value == "":
            return default
        return json.loads(str(value))
    except Exception:
        return default


def truthy(value):
    return str(value).strip().lower() in ("true", "1", "yes")
# EMBED-END


def products_for_case(policy, products, now):
    """Convenience for tests and the synthetic-case generator: raw products + now -> built products."""
    today, _t = parse_now(now)
    cal = make_calendar(policy)
    return [build_product(p, i, policy, today, cal) for i, p in enumerate(products)]


def deadline(policy, days_late, now):
    """Convenience: deadline date (ISO) for a days_late value at `now`."""
    today, _t = parse_now(now)
    cal = make_calendar(policy)
    return iso(deadline_for(band_for(days_late, policy), today, cal))
