"""EXTRACT step: brief file(s) + tool API file -> policy.json (contract v1).

    python -m builder.extract briefs/brief-a.md [briefs/brief-b.md] --openapi tools/openapi.json \
        -o policies/<slot>.policy.json [--trace traces/extract.jsonl]

How it works (generic: nothing from any particular brief lives in this file):
  1. read every input file raw (md, txt, eml, csv, json, html, docx...; anything else as text)
  2. parse the openapi file deterministically into the `tools` section (fields, types, enums, required)
  3. LLM passes, in parallel: policy part 1 and part 2, each twice, plus a verbatim rules pass
  4. diff the two replicates on key sections; a targeted "resolve" call settles disagreements
  5. normalise types (ISO dates, HH:MM, ints), cross-check enums against the tools, never crash:
     anything that does not fit goes to the nearest "notes"
Later documents override/extend earlier ones (Brief B over Brief A, a memo over a mail).

Policy extensions (keys beyond contract v1 that the extractor fills; the generator should honour them):
  calendar.week_starts       ISO weekday (1=Mon .. 7=Sun) the calendar week starts on. Always set: the stated
                             value, else the first business day after the weekend (Sat+Sun weekend -> 1).
                             "end of (this/next) week" and "this/next <weekday>" use this week, not Mon-Sun.
  calendar.non_holidays      ["YYYY-MM-DD"] dates the documents say are business days although they look like
                             holidays; never in calendar.holidays (informational, for prompts).
  products.types[t].floor_expr  minimum amount as an expression over response fields (when no single field
                             holds it): numbers, fields, + - * /, min, max, round_half_up(x, d), round_up(x, d),
                             round_down(x, d) (aliases round/ceil/floor). Evaluate with eval_amount_expr(expr,
                             product). floor_field is null then. A type with no lower amount gets
                             floor_field = full_field.
  callback.days              keywords only: "today" | "tomorrow" | "next_business_day" | "business_days:N".
  callback.today_requires_business_day  bool: a callback today only when today is a business day.
  callback.per_weekday       {"<ISO weekday>": {start, end, end_inclusive?}} window for that weekday
                             (end_inclusive falls back to callback.end_inclusive).
  callback.per_date          {"YYYY-MM-DD": {start, end, end_inclusive?}} window for one date (wins over
                             per_weekday and the general window).
  callback.max_asks, identity.max_wrong_scope, dates.next_weekday, products.list_field/type_field/id_field/
  days_late_field, bands[].attempts[].date keywords incl. "nth_business_day:N", triggers[].condition,
  closing{...}: as described in CONTRACT below.
"""
import argparse
import concurrent.futures as cf
import copy
import csv
import datetime as _dt
import email
import email.policy
import email.utils
import html
import io
import json
import os
import pathlib
import re
import sys
import time
import zipfile
import zlib

try:  # package import (python -m builder.extract) or direct
    from . import llm
except ImportError:  # pragma: no cover
    import llm  # type: ignore

# TODO(T04): builder/policy_schema.py implements the contract; use it when merged.
try:
    from .policy_schema import validate as _schema_validate  # type: ignore
except Exception:  # pragma: no cover - module not merged yet
    _schema_validate = None

TOP_KEYS = ["source", "language", "variables", "clock", "calendar", "opening", "third_party", "identity",
            "products", "bands", "amounts", "dates", "promise", "callback", "triggers", "notes", "outcomes",
            "phrases", "tools", "rules"]
PART1_KEYS = ["source", "language", "variables", "clock", "calendar", "opening", "third_party", "identity",
              "products", "bands", "callback"]
PART2_KEYS = ["amounts", "dates", "promise", "triggers", "notes", "outcomes", "phrases", "closing"]
DIFF_KEYS = ["calendar", "bands", "identity", "callback", "outcomes", "triggers", "closing"]
LIST_KEYS = {"variables", "bands", "triggers", "rules"}
TEXT_KEYS = {"language"}      # plain text (or null) sections
OPTIONAL_KEYS = ["closing"]   # contract extensions (policy_schema reads them when present)

TOTAL_BUDGET_S = int(os.environ.get("EXTRACT_BUDGET_S", "225"))   # whole extraction must stay < 4 min
ROUND1_SHARE = 0.62        # share of the budget for the parallel extraction round (incl. JSON repairs)
MIN_RESOLVE_S = 30         # skip the resolve call when less time than this is left
END_MARGIN_S = 5           # finish this long before the budget ends
RULES_CHUNK_CHARS = 60000  # bigger inputs: the verbatim rules list is asked in parallel parts (by document)
MAX_RULES_PARTS = 3


class ExtractError(RuntimeError):
    pass


def _log(msg):
    print("[extract] " + msg, file=sys.stderr, flush=True)


# =============================================================== 1. reading raw files

def _strip_html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(re.sub(r"[ \t]+", " ", text))


def _decode(data):
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _render_csv(text):
    """Keep the raw CSV, and add a pipe table so columns are unambiguous."""
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        rows = list(csv.reader(io.StringIO(text), dialect))
    except Exception:
        return text
    if not rows:
        return text
    return "\n".join(" | ".join(c.strip() for c in r) for r in rows)


def _read_eml(data):
    msg = email.message_from_bytes(data, policy=email.policy.default)
    out = []
    for h in ("From", "To", "Cc", "Date", "Subject"):
        if msg.get(h):
            out.append("%s: %s" % (h, msg.get(h)))
    out.append("")
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        name = part.get_filename()
        try:
            payload = part.get_content()
        except Exception:
            raw = part.get_payload(decode=True) or b""
            payload = _decode(raw)
        if isinstance(payload, bytes):
            payload = _read_bytes(payload, name or "")
        if ctype == "text/html":
            payload = _strip_html(payload)
        elif name and name.lower().endswith(".csv"):
            payload = _render_csv(payload)
        if name:
            out.append("--- attachment: %s ---" % name)
        out.append(str(payload).strip())
    return "\n".join(out)


def _read_docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tc>", " | ", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def _read_bytes(data, name):
    low = name.lower()
    try:
        if low.endswith(".eml") or (not low.endswith((".md", ".txt", ".csv", ".json"))
                                    and re.match(rb"(?i)^(from|received|return-path|subject|date|to|mime-version):",
                                                 data[:200])):
            return _read_eml(data)
        if low.endswith(".docx"):
            return _read_docx(data)
        if low.endswith(".pdf") or data[:5] == b"%PDF-":
            return _read_pdf(data)
        text = _decode(data)
        if low.endswith((".html", ".htm")):
            return _strip_html(text)
        if low.endswith(".json"):
            try:
                return json.dumps(json.loads(text), indent=1, ensure_ascii=False)
            except ValueError:
                return text
        if low.endswith((".csv", ".tsv")):
            return _render_csv(text)
        return text
    except Exception as e:  # never crash on a file: degrade to raw text
        return "(could not parse %s: %s)\n%s" % (name, e, _decode(data))


# ---------------------------------------------------------------- PDF text (stdlib only)

_PDF_OBJ = re.compile(rb"(?<![0-9])(\d{1,10})[ \t\r\n]+(\d{1,5})[ \t\r\n]+obj\b")
_PDF_TOKEN = re.compile(r"\s+|%[^\r\n]*|/[^\s/<>\[\]{}()%]*|<<|>>|<[0-9A-Fa-f\s]*>|\[|\]"
                        r"|[+-]?(?:\d+\.?\d*|\.\d+)(?![^\s/<>\[\]{}()%])|[^\s/<>\[\]{}()%]+")
_PDF_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}


def _pdf_inflate(head, raw):
    """Decoded stream bytes: FlateDecode or unfiltered; None for other filters (images, LZW...)."""
    if re.search(rb"/(FlateDecode|Fl)\b", head):
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None
    return None if b"/Filter" in head else raw


def _pdf_objects(data):
    """{object number: (dictionary bytes, decoded stream bytes or None)}, incl. objects in object streams."""
    starts = [(m.start(), m.end(), int(m.group(1))) for m in _PDF_OBJ.finditer(data)]
    raw = {}
    for i, (_s, e, num) in enumerate(starts):
        raw[num] = data[e:starts[i + 1][0] if i + 1 < len(starts) else len(data)]

    def length(head):
        m = re.search(rb"/Length[ \t\r\n]+(\d+)(?:[ \t\r\n]+\d+[ \t\r\n]+R)?", head)
        if not m:
            return None
        if m.group(0).endswith(b"R"):
            ref = re.match(rb"\s*(\d+)", raw.get(int(m.group(1)), b""))
            return int(ref.group(1)) if ref else None
        return int(m.group(1))

    objs = {}
    for num, body in raw.items():
        m = re.search(rb"\bstream\r?\n", body)
        if not m:
            objs[num] = (body.split(b"endobj")[0], None)
            continue
        head, n, stream = body[:m.start()], length(body[:m.start()]), None
        if n is not None and b"endstream" in body[m.end() + n:m.end() + n + 30]:
            stream = body[m.end():m.end() + n]
        if stream is None:
            k = body.rfind(b"endstream")
            stream = body[m.end():k if k > 0 else len(body)].rstrip(b"\r\n")
        objs[num] = (head, _pdf_inflate(head, stream))
    for num, (head, stream) in list(objs.items()):
        first = re.search(rb"/First[ \t\r\n]+(\d+)", head)
        if stream and first and re.search(rb"/Type\s*/ObjStm", head):
            first = int(first.group(1))
            nums = [int(x) for x in re.findall(rb"\d+", stream[:first])]
            pairs = list(zip(nums[0::2], nums[1::2]))
            for j, (onum, off) in enumerate(pairs):
                stop = pairs[j + 1][1] if j + 1 < len(pairs) else len(stream) - first
                objs.setdefault(onum, (stream[first + off:first + stop], None))
    return objs


def _pdf_balanced(b):
    """The << ... >> dictionary at the start of b."""
    depth, i = 0, 0
    while i < len(b):
        if b.startswith(b"<<", i):
            depth, i = depth + 1, i + 2
        elif b.startswith(b">>", i):
            depth, i = depth - 1, i + 2
            if depth <= 0:
                return b[:i]
        else:
            i += 1
    return b


def _pdf_get(objs, body, key):
    """Value of /key in a dictionary: nested << >> or [ ] as bytes, or the body of the object it refers to."""
    m = re.search(rb"/" + key + rb"(?![A-Za-z0-9])\s*", body or b"")
    if not m:
        return None
    rest = body[m.end():]
    ref = re.match(rb"(\d+)\s+\d+\s+R\b", rest)
    if ref:
        return objs.get(int(ref.group(1)), (b"", None))[0]
    if rest.startswith(b"<<"):
        return _pdf_balanced(rest)
    if rest.startswith(b"["):
        return rest[:rest.find(b"]") + 1]
    return None


def _pdf_cmap(stream):
    """ToUnicode CMap -> ({code: text}, code width in bytes or None)."""
    text = stream.decode("latin-1")
    m = re.search(r"begincodespacerange\s*<([0-9A-Fa-f]+)>", text)
    width = max(1, len(m.group(1)) // 2) if m else None

    def uni(h):
        h = h if len(h) % 4 == 0 else h.rjust(len(h) + (-len(h)) % 4, "0")
        try:
            return bytes.fromhex(h).decode("utf-16-be", errors="ignore")
        except ValueError:
            return ""
    cmap = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            cmap[int(src, 16)] = uni(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if not 0 <= hi - lo <= 65535:
                continue
            if dst.startswith("["):
                for k, d in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst)):
                    cmap[lo + k] = uni(d)
            elif len(dst) > 2:
                base, w = int(dst[1:-1], 16), len(dst) - 2
                for k in range(hi - lo + 1):
                    cmap[lo + k] = uni("%0*X" % (w, base + k))
    return cmap, width


def _pdf_font(objs, num):
    head = objs.get(num, (b"", None))[0]
    font = {"width": 2 if re.search(rb"/Subtype\s*/Type0", head) else 1, "cmap": {},
            "glyphs": bool(re.search(rb"/Subtype\s*/Type3", head))}
    m = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", head)
    stream = objs.get(int(m.group(1)), (b"", None))[1] if m else None
    if stream:
        font["cmap"], width = _pdf_cmap(stream)
        font["width"] = width or font["width"]
        font["glyphs"] = False
    return font


def _pdf_show(raw, font):
    """Text of a shown string under the current font."""
    if not font:
        return raw.decode("cp1252", errors="replace")
    if font["glyphs"]:  # drawing-only font (Type3 without ToUnicode): a separator, not text
        return " "
    w, cmap, out = font["width"], font["cmap"], []
    for i in range(0, len(raw) - w + 1, w):
        code = int.from_bytes(raw[i:i + w], "big")
        if code in cmap:
            out.append(cmap[code])
        elif w == 1:
            out.append(bytes([code]).decode("cp1252", errors="replace"))
    return "".join(out)


def _pdf_literal(s, i):
    """(bytes, end index) of the literal string starting at s[i] == '('."""
    out, depth, i, n = [], 0, i + 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            nx = s[i + 1:i + 2]
            if nx in _PDF_ESC:
                out.append(_PDF_ESC[nx])
                i += 2
            elif nx and nx in "01234567":
                m = re.match(r"[0-7]{1,3}", s[i + 1:i + 4])
                out.append(chr(int(m.group(0), 8) & 0xFF))
                i += 1 + len(m.group(0))
            elif nx in ("\r", "\n"):
                i += 3 if s[i + 1:i + 3] == "\r\n" else 2
            else:
                out.append(nx)
                i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return "".join(out).encode("latin-1", errors="replace"), i + 1
            depth -= 1
        out.append(c)
        i += 1
    return "".join(out).encode("latin-1", errors="replace"), i


def _pdf_content_text(content, fonts):
    """Text shown by a content stream (BT/ET, Tf, Tj, TJ, ', ")."""
    s = content.decode("latin-1")
    out, stack, font, i, n = [], [], None, 0, len(s)

    def newline():
        if out and not out[-1].endswith("\n"):
            out.append("\n")
    while i < n:
        if s[i] == "(":
            raw, i = _pdf_literal(s, i)
            stack.append(raw)
            continue
        m = _PDF_TOKEN.match(s, i)
        if not m:
            i += 1
            continue
        tok, i = m.group(0), m.end()
        if tok[0].isspace() or tok[0] == "%" or tok in ("<<", ">>"):
            continue
        if tok[0] == "<":
            h = re.sub(r"\s", "", tok[1:-1])
            stack.append(bytes.fromhex(h + "0" * (len(h) % 2)))
        elif tok == "[":
            stack.append("[")
        elif tok == "]":
            arr = []
            while stack and not (isinstance(stack[-1], str) and stack[-1] == "["):
                arr.append(stack.pop())
            if stack:
                stack.pop()
            stack.append(arr[::-1])
        elif tok[0] == "/":
            stack.append(tok)
        elif re.match(r"[+-]?(\d|\.\d)", tok):
            try:
                stack.append(float(tok))
            except ValueError:
                pass
        else:  # operator
            if tok == "BT":
                newline()
            elif tok == "Tf" and len(stack) >= 2 and isinstance(stack[-2], str):
                font = fonts.get(stack[-2][1:])
            elif tok in ("Tj", "'", '"') and stack and isinstance(stack[-1], bytes):
                if tok != "Tj":
                    newline()
                out.append(_pdf_show(stack[-1], font))
            elif tok == "TJ" and stack and isinstance(stack[-1], list):
                for el in stack[-1]:
                    if isinstance(el, bytes):
                        out.append(_pdf_show(el, font))
                    elif isinstance(el, float) and el < -200:  # a wide gap is a word space
                        out.append(" ")
            elif tok in ("Td", "TD") and len(stack) >= 2 and isinstance(stack[-1], float):
                if abs(stack[-1]) > 0.01:
                    newline()
                else:
                    out.append(" ")
            elif tok == "T*":
                newline()
            elif tok == "ID":  # inline image data: skip to EI
                k = re.search(r"\sEI(?=\s|$)", s[i:])
                i = i + k.end() if k else n
            stack = []
    return "".join(out)


def _pdf_pages(objs):
    """Page object numbers in document order (page tree walk; all page objects when there is no tree)."""
    pages, seen = [], set()

    def walk(num, depth):
        if num in seen or depth > 30:
            return
        seen.add(num)
        head = objs.get(num, (b"", None))[0]
        if re.search(rb"/Type\s*/Pages\b", head):
            for r in re.findall(rb"(\d+)\s+\d+\s+R\b", _pdf_get(objs, head, b"Kids") or b""):
                walk(int(r), depth + 1)
        elif re.search(rb"/Type\s*/Page\b", head):
            pages.append(num)
    for num, (head, _st) in sorted(objs.items()):
        if re.search(rb"/Type\s*/Pages\b", head) and not re.search(rb"/Parent\s", head):
            walk(num, 0)
    return pages or [num for num, (head, _st) in sorted(objs.items()) if re.search(rb"/Type\s*/Page\b", head)]


def _pdf_text(data):
    """Text of a PDF: FlateDecode content streams, fonts mapped through their ToUnicode CMaps."""
    objs = _pdf_objects(data)
    font_cache = {}

    def fonts_of(head, depth=0):
        res = _pdf_get(objs, head, b"Resources")
        parent = re.search(rb"/Parent\s+(\d+)\s+\d+\s+R", head)
        if res is None and parent and depth < 30:
            return fonts_of(objs.get(int(parent.group(1)), (b"", None))[0], depth + 1)
        fonts = {}
        for name, ref in re.findall(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R\b", _pdf_get(objs, res, b"Font") or b""):
            if int(ref) not in font_cache:
                font_cache[int(ref)] = _pdf_font(objs, int(ref))
            fonts[name.decode("latin-1")] = font_cache[int(ref)]
        return fonts

    parts = []
    for page in _pdf_pages(objs):
        head = objs[page][0]
        m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R\b)", head)
        refs = [int(r) for r in re.findall(rb"(\d+)\s+\d+\s+R\b", m.group(1))] if m else []
        if len(refs) == 1 and objs.get(refs[0], (b"", None))[1] is None:  # indirect array of streams
            refs = [int(r) for r in re.findall(rb"(\d+)\s+\d+\s+R\b", objs.get(refs[0], (b"", None))[0])]
        content = b"\n".join(objs.get(r, (b"", None))[1] or b"" for r in refs)
        parts.append(_pdf_content_text(content, fonts_of(head)))
    if len(re.findall(r"\w", "".join(parts))) < 20:  # no page tree found: every text stream, fonts by name
        fonts = {}
        for head, _st in objs.values():
            fonts.update(fonts_of(head))
        parts = [_pdf_content_text(st, fonts) for head, st in objs.values()
                 if st and b"BT" in st and not re.search(rb"/(Type\s*/ObjStm|Length1|Subtype\s*/Image)", head)]
    text = "\n".join(parts)
    text = "".join(c if c.isprintable() or c == "\n" else " " for c in text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _read_pdf(data):
    """PDF text from its content streams; the printable strings of the raw file only when that finds no text."""
    try:
        text = _pdf_text(data)
    except Exception:  # never crash on a file
        text = ""
    if len(re.findall(r"[^\W\d_]", text)) >= 20:
        return "(PDF text; the page layout is not kept)\n" + text
    strings = re.findall(rb"[\x20-\x7e]{4,}", data)
    return "(PDF, printable strings only)\n" + "\n".join(s.decode("ascii") for s in strings[:5000])


# ---------------------------------------------------------------- big data files, size cap

DATA_SUMMARY_BYTES = 32 * 1024   # json/csv data files bigger than this are summarised, never sent whole
MAX_DOC_CHARS = 40000            # per-file cap on the text sent to the model; the rest is cut ("[cut]")
MAX_TOTAL_CHARS = 250000         # whole input (safety net): the longest documents are cut further above this
MIN_CUT_CHARS = 8000             # ... but never below this
DATA_EXT = (".json", ".jsonl", ".ndjson", ".csv", ".tsv")


def _cap(text, limit=MAX_DOC_CHARS):
    """text cut to about `limit` characters: the head and the tail (often a conclusion or a change) are kept,
    the middle is replaced by a "[cut: ...]" line."""
    if len(text) <= limit:
        return text
    head_n, tail_n = int(limit * 0.75), int(limit * 0.25)
    a = text.rfind("\n", 0, head_n)
    a = a if a > head_n * 0.8 else head_n
    b = text.find("\n", len(text) - tail_n)
    b = b + 1 if 0 <= b < len(text) - tail_n * 0.8 else len(text) - tail_n
    return (text[:a].rstrip() + "\n[cut: %d characters of this file not shown]\n" % (b - a)
            + text[b:].lstrip("\n"))


def _fit_total(docs, budget):
    """Cut the longest documents (never below MIN_CUT_CHARS) so the whole input stays within `budget`."""
    lens = [len(t) for _, t in docs]
    if sum(lens) <= budget:
        return
    lo, hi = MIN_CUT_CHARS, max(lens)
    while lo < hi:  # the largest per-file limit that fits
        mid = (lo + hi + 1) // 2
        if sum(min(n, mid) + (80 if n > mid else 0) for n in lens) <= budget:
            lo = mid
        else:
            hi = mid - 1
    for i, (label, text) in enumerate(docs):
        if len(text) > lo:
            docs[i] = (label, _cap(text, lo))


def _records(value, depth=0):
    """(path, list) of the largest list in a JSON value (the value itself when it is a list)."""
    if isinstance(value, list):
        return "", value
    best = ("", None)
    if isinstance(value, dict) and depth < 4:
        for k, v in value.items():
            path, lst = _records(v, depth + 1)
            if lst is not None and (best[1] is None or len(lst) > len(best[1])):
                best = (str(k) + ("." + path if path else ""), lst)
    return best


def _outline(value, depth=0):
    """Compact structure of a JSON value: keys and value types, list lengths."""
    if isinstance(value, dict):
        if depth >= 3:
            return "{%d keys}" % len(value)
        items = ["%s: %s" % (json.dumps(k, ensure_ascii=False), _outline(v, depth + 1))
                 for k, v in list(value.items())[:40]]
        return "{" + ", ".join(items) + (", ..." if len(value) > 40 else "") + "}"
    if isinstance(value, list):
        return "[%d x %s]" % (len(value), _outline(value[0], depth + 1) if value else "nothing")
    return {bool: "bool", int: "number", float: "number", str: "text"}.get(type(value), "null")


def _data_summary(data, name):
    """Short description of a big data file (structure, record count, first record), or None."""
    low, text = name.lower(), _decode(data)
    if low.endswith((".csv", ".tsv")):
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
            rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
        except Exception:
            return None
        if len(rows) < 3:
            return None
        head = rows[0]
        first = {h.strip(): v.strip() for h, v in zip(head, rows[1])}
        return ("(data file, %d records, not shown: summary only)\nColumns: %s\nFirst record:\n%s"
                % (len(rows) - 1, " | ".join(c.strip() for c in head),
                   _cap(json.dumps(first, ensure_ascii=False), 3000)))
    try:
        if low.endswith((".jsonl", ".ndjson")):
            value = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        else:
            value = json.loads(text)
    except ValueError:
        return None
    path, recs = _records(value)
    if not recs or len(recs) < 2 or len(json.dumps(recs, ensure_ascii=False)) < 0.5 * len(text):
        return None  # not a record file (e.g. a spec or a config): read as text, capped
    keys = []
    for r in recs[:50]:
        for k in (r if isinstance(r, dict) else {}):
            if k not in keys:
                keys.append(k)
    scalars = ["%s = %s" % (k, _cap(json.dumps(v, ensure_ascii=False), 600)) for k, v in
               (value.items() if isinstance(value, dict) else []) if not isinstance(v, (dict, list))][:20]
    return ("(data file, %d records, not shown: summary only)\nStructure: %s\n%sRecords: %d at %s\n%s"
            "First record:\n%s"
            % (len(recs), _cap(_outline(value), 4000), "".join("Top-level value: %s\n" % x for x in scalars),
               len(recs), path or "top level",
               ("Record fields: %s\n" % ", ".join(map(str, keys[:80]))) if keys else "",
               _cap(json.dumps(recs[0], ensure_ascii=False), 3000)))


def _read_doc(data, name):
    """Text of one input file for the model: big data files summarised, every file capped."""
    if len(data) > DATA_SUMMARY_BYTES and name.lower().endswith(DATA_EXT):
        try:
            summary = _data_summary(data, name)
        except Exception:  # never crash on a file
            summary = None
        if summary:
            return summary
    return _cap(_read_bytes(data, name))


class DocList(list):
    """[(name, text)] plus `dates` {name: ISO datetime or None} and `order_known` (False when a folder's
    files could not be put in a reliable order)."""
    order_known = True

    def __init__(self, *a):
        super().__init__(*a)
        self.dates = {}


def _doc_date(data, text, name=""):
    """Date of a document from its Date header (mail or a 'Date:' line near the top), else from a leading
    YYYY-MM-DD in its file name; naive UTC or None."""
    d = _header_date(data, text)
    if d is None:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?!\d)", pathlib.Path(name or "").name)
        if m:
            try:
                d = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                d = None
    return d


def _header_date(data, text):
    raw = None
    m = re.search(rb"(?im)^date:[ \t]*(.+)$", data[:4000])
    if m:
        raw = m.group(1).decode("utf-8", errors="replace").strip()
    else:
        m = re.search(r"(?im)^\W{0,3}date\W{0,3}:\W{0,3}(.+)$", "\n".join(text.splitlines()[:30]))
        raw = m.group(1).strip(" *_") if m else None
    if not raw:
        return None
    try:
        d = email.utils.parsedate_to_datetime(raw)
        if d is not None:
            if d.tzinfo is not None:
                d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            return d
    except Exception:
        pass
    iso = norm_date(raw)
    return _dt.datetime.strptime(iso, "%Y-%m-%d") if iso else None


def _hidden(path, root):
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = (path.name,)
    return any(p.startswith(".") or p.startswith("~$") or p == "__MACOSX" for p in parts)


def read_documents(paths):
    """DocList of (name, text). Files keep the order they were given in. A folder expands to its files
    (dotfiles / hidden folders skipped; named by their path inside the folder), sorted by date (Date header,
    else a leading YYYY-MM-DD in the file name) when every file has one, else by path (then order_known is
    False). Big data files are summarised and every text is capped (MAX_DOC_CHARS, MAX_TOTAL_CHARS)."""
    docs = DocList()
    for p in paths:
        p = pathlib.Path(p)
        if p.is_dir():
            files = [x for x in p.rglob("*") if x.is_file() and not _hidden(x, p)]
            entries = []
            for f in files:
                data = f.read_bytes()
                text = _read_doc(data, f.name)
                entries.append((f, text, _doc_date(data, text, f.name)))
            if entries and all(e[2] is not None for e in entries):
                entries.sort(key=lambda e: (e[2], e[0].name))
            else:
                entries.sort(key=lambda e: str(e[0].relative_to(p)))
                if len(entries) > 1:
                    docs.order_known = False
            for f, text, d in entries:
                label = f.relative_to(p).as_posix()  # sub-folder names tell what a file is (mail, mock, test data)
                docs.append((label, text))
                docs.dates[label] = d.isoformat(sep=" ") if d else None
        elif p.name == ".DS_Store":
            continue
        else:
            data = p.read_bytes()
            text = _read_doc(data, p.name)
            d = _doc_date(data, text, p.name)
            docs.append((p.name, text))
            docs.dates[p.name] = d.isoformat(sep=" ") if d else None
    _fit_total(docs, MAX_TOTAL_CHARS)
    return docs


def documents_block(docs):
    n = len(docs)
    order_known = getattr(docs, "order_known", True)
    dates = getattr(docs, "dates", {}) or {}
    parts = []
    if n > 1 and not order_known:
        parts.append("NOTE: the order of these documents is not known. Where they conflict, the latest dated "
                     "document (its date attribute, Date header or a date in its text) wins; an undated memo or "
                     "correction that says it changes another document wins over that document.")
    for i, (label, text) in enumerate(docs, 1):
        attrs = ""
        if n > 1 and order_known:
            attrs += ' order="%s"' % ("earliest" if i == 1 else "latest" if i == n else "middle")
        if dates.get(label):
            attrs += ' date="%s"' % dates[label]
        parts.append('<document index="%d" of="%d" name="%s"%s>\n%s\n</document>'
                     % (i, n, label, attrs, text.strip()))
    return "\n\n".join(parts)


# =============================================================== 2. openapi -> tools (deterministic)

def _resolve_ref(spec, node, depth=0):
    if not isinstance(node, dict) or depth > 20:
        return node if isinstance(node, dict) else {}
    if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
        target = spec
        for part in node["$ref"][2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part, {}) if isinstance(target, dict) else {}
        merged = dict(_resolve_ref(spec, target, depth + 1))
        merged.update({k: v for k, v in node.items() if k != "$ref"})
        return merged
    if "allOf" in node and isinstance(node["allOf"], list):
        merged = {"type": "object", "properties": {}, "required": []}
        # properties/required written next to allOf belong to the same object
        merged["properties"].update(node.get("properties") or {})
        merged["required"] += list(node.get("required") or [])
        for sub in node["allOf"]:
            sub = _resolve_ref(spec, sub, depth + 1)
            merged["properties"].update(sub.get("properties") or {})
            merged["required"] += [r for r in (sub.get("required") or []) if r not in merged["required"]]
            for k, v in sub.items():
                if k not in ("properties", "required"):
                    merged.setdefault(k, v)
        for k, v in node.items():
            if k not in ("allOf", "properties", "required"):
                merged[k] = v
        if not merged["properties"]:
            del merged["properties"]
        return merged
    for key in ("oneOf", "anyOf"):
        if key in node and isinstance(node[key], list) and node[key]:
            branches = [_resolve_ref(spec, b, depth + 1) for b in node[key]]
            branches = [b for b in branches if b.get("type") != "null" and b != {}
                        and b.get("enum") != [None]] or branches
            merged = dict(branches[0])
            enums = []
            for b in branches:
                for e in (b.get("enum") or []):
                    if e is not None and e not in enums:
                        enums.append(e)
                if "const" in b and b["const"] is not None and b["const"] not in enums:
                    enums.append(b["const"])
            if enums:
                merged["enum"] = enums
            props = {}
            for b in branches:
                props.update(b.get("properties") or {})
            if props:
                merged["properties"] = props
            merged.update({k: v for k, v in node.items() if k != key})
            return merged
    return node


def _field_info(spec, schema, required, depth=0):
    schema = _resolve_ref(spec, schema or {})
    typ = schema.get("type")
    if isinstance(typ, list):  # OpenAPI 3.1: ["string", "null"]
        non_null = [t for t in typ if t != "null"]
        typ = non_null[0] if non_null else None
    info = {"type": typ or ("object" if "properties" in schema else "string"),
            "enum": list(schema["enum"]) if isinstance(schema.get("enum"), list) else None,
            "required": bool(required)}
    for k in ("format", "description", "default", "minimum", "maximum", "pattern"):
        if k in schema:
            info[k] = schema[k]
    if depth < 4:
        if info["type"] == "object" and isinstance(schema.get("properties"), dict):
            req = set(schema.get("required") or [])
            info["properties"] = {n: _field_info(spec, s, n in req, depth + 1)
                                  for n, s in schema["properties"].items()}
        if info["type"] == "array" and isinstance(schema.get("items"), dict):
            info["items"] = _field_info(spec, schema["items"], False, depth + 1)
    return info


def _object_fields(spec, schema):
    schema = _resolve_ref(spec, schema or {})
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}
    req = set(schema.get("required") or [])
    return {name: _field_info(spec, s, name in req) for name, s in props.items()}


def _json_schema(spec, content):
    if not isinstance(content, dict):
        return {}
    for ctype in ("application/json",) + tuple(content.keys()):
        c = content.get(ctype)
        if isinstance(c, dict) and isinstance(c.get("schema"), dict):
            return c["schema"]
    return {}


def parse_openapi(path_or_spec):
    """openapi (path or dict) -> {"tools": {...}, "servers": [...], "description": str}.

    tools[name] = {fields:{f:{type,enum,required,...}}, notes, method, path, url, summary, response_fields}
    """
    if isinstance(path_or_spec, dict):
        spec = path_or_spec
    else:
        spec = json.loads(_decode(pathlib.Path(path_or_spec).read_bytes()))
    servers = [s.get("url") for s in spec.get("servers") or [] if isinstance(s, dict) and s.get("url")]
    base = (servers[0] if servers else "").rstrip("/")
    tools = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            name = op.get("operationId") or path.strip("/").replace("/", "_") or method.lower()
            fields = {}
            for prm in (item.get("parameters") or []) + (op.get("parameters") or []):
                prm = _resolve_ref(spec, prm)
                if isinstance(prm, dict) and prm.get("name"):
                    fi = _field_info(spec, prm.get("schema") or {}, prm.get("required"))
                    fi["in"] = prm.get("in")
                    if prm.get("description") and "description" not in fi:
                        fi["description"] = prm["description"]
                    fields[prm["name"]] = fi
            body = _resolve_ref(spec, op.get("requestBody") or {})
            fields.update(_object_fields(spec, _json_schema(spec, body.get("content"))))
            responses = op.get("responses") or {}
            ok_resp = None
            for code in sorted(responses, key=str):
                if str(code).startswith("2"):
                    ok_resp = _resolve_ref(spec, responses[code])
                    break
            resp_fields = _object_fields(spec, _json_schema(spec, (ok_resp or {}).get("content")))
            notes = " ".join(x for x in (op.get("summary"), op.get("description")) if x)
            errs = ["%s: %s" % (c, _resolve_ref(spec, r).get("description", ""))
                    for c, r in responses.items() if not str(c).startswith("2")]
            if ok_resp and ok_resp.get("description"):
                errs.insert(0, "%s: %s" % ("2xx", ok_resp["description"]))
            tools[name] = {
                "fields": fields,
                "notes": notes,
                "method": method.upper(),
                "path": path,
                "url": base + path,
                "summary": op.get("summary") or "",
                "response_fields": resp_fields,
                "response_notes": errs,
            }
    return {"tools": tools, "servers": servers,
            "description": ((spec.get("info") or {}).get("description") or "")}


# =============================================================== 3. LLM prompts (generic contract)

SYSTEM = (
    "You are a meticulous extraction engine. You turn a bank's specification for a phone agent "
    "(clean spec or raw material: mails, policy extracts, memos, code lists, call-flow diagrams, transcripts) into "
    "a JSON policy that a code generator consumes. Extract only how the phone agent must behave on its calls. "
    "Ignore instructions addressed to the people who build, test or host the agent (how to build it, mock or "
    "deploy its services, submit the work, or how the work is scored or judged), setup steps, and test/sample data "
    "rows: they are context, never rules for the agent. "
    "Use ONLY what the documents say. Never invent values, names, dates or phrases; when "
    "something is not specified use null / [] / false and say so in that section's \"notes\". Copy rule texts, "
    "phrases and sentences to be spoken VERBATIM (keep {placeholders} the documents use). Use the exact "
    "identifiers (tool names, field names, enum values, variable names) found in the documents and the tool "
    "API. Documents are given in order: a later document (or a memo/correction/update) overrides or extends an "
    "earlier one wherever they conflict; apply that and record each override. If the documents say their order "
    "is unknown, the latest dated document wins. Read tables carefully, row by row. Numbers written in words "
    "go into numeric slots as digits. Struck-through text (~~like this~~) is cancelled; its replacement applies."
)

# Generic expression vocabularies (options offered to the model; free text is always allowed as fallback).
NORMALISE_OPTIONS = ["dmy_to_iso", "mdy_to_iso", "ymd_to_iso", "digits_only", "lowercase_text", "none"]
AMOUNT_OPTIONS = ["full", "floor"]
DATE_OPTIONS = ["today", "deadline", "next_business_day", "end_of_week_business_day",
                "end_of_next_week_business_day", "end_of_month_business_day", "nth_business_day:<N>"]
DATE_OPTIONS_HELP = ("nth_business_day:<N> = the N-th business day after today (today not counted); "
                     "end_of_week_business_day = last business day of the current week; "
                     "end_of_next_week_business_day = last business day of the following week; "
                     "end_of_month_business_day = last business day of the current month")


def _opts(options):
    return "|".join('"%s"' % o for o in options)


CONTRACT = {
    "source": '{"briefs": [document names in order], "overrides_applied": ["one line per change a later '
              'document/memo makes to an earlier rule: what changed, old -> new, with a short quote"], "notes": []}',
    "language": 'the language the agent speaks on the calls, as an English language name (e.g. "English", '
                '"German", "Spanish"): the language the documents say the calls are held in, else the language '
                'of the sentences the agent must say; null only if the documents give no hint',
    "variables": '[{"name": exact call-variable name, "meaning": text, "example": one example value of this '
                 'variable exactly as the documents show it (e.g. in a sample call or test data), or null}]  -- '
                 'variables the agent knows at call start',
    "clock": '{"now_variable": name of the variable holding the current date/time or null, "timezone": text|null, '
             '"date_format": format of that variable as the documents show it, "notes": []}',
    "calendar": '{"weekend": [ISO weekday ints, Monday=1 .. Sunday=7, that are NOT business days], '
                '"holidays": ["YYYY-MM-DD", every listed non-business date, after applying overrides], '
                '"roll_rule": "next_business_day"|"previous_business_day"|"none" (what to do when a named or '
                'computed date is not a business day), "week_starts": ISO weekday int the documents\' calendar '
                'week starts on (e.g. a week "from Sunday to Saturday" starts on 7) or null if not stated, '
                '"non_holidays": ["YYYY-MM-DD" dates the documents explicitly say are normal business days although '
                'they look like holidays (never put these in holidays)], "notes": [verbatim calendar sentences not '
                'captured]}',
    "opening": '{"text": the required first sentence / its required content, "disclosures": [things that must '
               'be said up front], "forbidden_before_verification": [words/topics that must not be said before '
               'identity is verified], "line": the exact first words of the call ONLY when the documents quote them '
               'word for word: the quoted opening sentence, a space, then the quoted question that asks for the customer, '
               'with the documents\' {placeholders}; null when the opening is described rather than quoted, "notes": '
               '[verbatim rules about confirming who is on the line and any other opening rules]}',
    "third_party": '{"cases": [{"trigger": each situation the documents describe where the customer is not '
                   'reached or not confirmed on the line, "say": verbatim text to say or null, "action": "callback"|'
                   '"end"|"ask_again"|text, "outcome": exact close outcome the documents give, "reason": exact '
                   'reason the documents give or null}], "rules": [verbatim rules about these situations], '
                   '"notes": []}',
    "identity": '{"questions": [{"order": 1.., "tool_field": exact enum value / field the verification tool '
                'expects for this question, "ask": how to ask, "answer_format": "date"|"digits"|"text", '
                '"normalise": one of ' + _opts(NORMALISE_OPTIONS) + ' or a short text rule (how the spoken '
                'answer must be converted before the tool call, following the documents and the tool API; use '
                'a *_to_iso value only when the tool wants YYYY-MM-DD), "tool_format": exact format the tool '
                'expects for this answer (a date pattern such as "YYYY-MM-DD" or the documents\' own pattern, '
                'letter case / spacing rules for text) or null}], "max_wrong": int (the number of wrong '
                'answers that ends the check, after overrides and corrections such as struck-through text), '
                '"max_wrong_scope": "total" (counted over all questions together) | "per_question" (counted '
                'separately for each question), "wrong_counts_rules": [verbatim rules on what counts or does not '
                'count as wrong, retries, hints the tool may return], "fail": {"outcome", "reason", "say"}, '
                '"notes": []}',
    "products": '{"max_count": int (max late products per customer; 1 if the documents only speak of one), '
                '"types": {<exact product type value from the account tool response>: {"speak_as": how to name '
                'it aloud (verbatim, with {response_field} placeholders if it includes data), "floor_field": '
                'response field holding the minimum acceptable amount (when the documents say there is no lower '
                'amount for this type, the full_field; null only if the minimum is a rule), "floor_expr": null, '
                'or when the minimum is a rule rather than one field, an expression over numbers and response '
                'fields using min(a, b), max(a, b), + - * /, round_half_up(x, <decimals>), round_up(x, '
                '<decimals>), round_down(x, <decimals>) (e.g. "min(<number>, <field>)", "round_half_up(0.1 * '
                '<field>, 2)"); write percentages as decimals and include EVERY rounding the documents state '
                '(to the cent = 2 decimals, to a whole unit = 0); a response field the documents say is not '
                'used must not appear, "full_field": response field holding the full amount due}}, '
                '"order": "as_returned", '
                '"list_field": response field holding the product list, "type_field": item field holding the '
                'product type, "id_field": item field holding the product id, "days_late_field": item field '
                'holding the days late, "notes": []}',
    "bands": '[{"name": band name, "min_days_late": int, "max_days_late": int, "deadline": {"kind": '
             '"nth_business_day", "n": int, "counted_from": "today" (today is not counted: the first business '
             'day after today is 1) | "today_inclusive" (today counts as 1 when it is a business day)}, '
             '"attempts": [{"n": 1.., "amount": one of ' + _opts(AMOUNT_OPTIONS) + ' or a text expression, '
             '"date": one of ' + _opts(DATE_OPTIONS) + ' or a text expression (' + DATE_OPTIONS_HELP + '), '
             '"text": verbatim description of this attempt}], "notes": []}]  -- one entry per row of the '
             'lateness / deadline table; attempts = persuasion attempts allowed in that band (empty list if none)',
    "callback": '{"days": [allowed callback days as keywords: "today", "tomorrow", "next_business_day", '
                '"business_days:<N>" (conditions go in the fields below / notes, not in this list)], '
                '"today_requires_business_day": bool (true when a callback today is allowed only if today is a '
                'business day), "start": "HH:MM", "end": "HH:MM", "end_inclusive": bool, "must_be_after_now": '
                'bool, "per_weekday": {<ISO weekday int as a string, Monday="1">: {"start": "HH:MM", "end": '
                '"HH:MM", "end_inclusive": bool}} for weekdays whose window differs from the general one ({} if '
                'none), "per_date": {"YYYY-MM-DD": {"start": "HH:MM", "end": "HH:MM", "end_inclusive": bool}} for '
                'specific dates with their own window, after overrides ({} if none), "max_asks": int (how many '
                'times a callback time may be asked for in total), "notes": [verbatim callback rules: what if the '
                'offered time is outside the window, how many re-asks, outcomes]}',
    "amounts": '{"min": "floor", "max": "full", "below_min": "reject"|"accept"|text, "ask_open_amount": bool '
               '(true only if the documents allow an open question about the amount), "rules": [verbatim amount '
               'rules], "notes": []}',
    "dates": '{"customer_date_after_deadline": text, "vague_date": text, "next_weekday": how the documents '
             'read a customer saying "next" + a weekday name: "following_week" (that weekday in the following '
             'calendar week) | "next_occurrence" (the first such weekday after today) | "" (not stated), '
             '"rules": [verbatim date rules, incl. how relative day expressions are interpreted], "notes": []}',
    "promise": '{"readback": what to read back and the confirmation question (verbatim), "confirm_required": '
               'bool, "retry_on_failure": int (extra tries after a failed recording), "success_say": verbatim, '
               '"failure_say": text, "per_product": bool (one promise per product), "notes": [verbatim promise '
               'rules not captured above]}',
    "triggers": '[{"name": snake_case, "detect": what the customer says/does, "action": "transfer"|"note"|'
                '"callback"|"close"|"continue"|"say", "queue": exact transfer queue or null, "outcome": exact '
                'close outcome or null, "reason": exact close reason or null, "note_reason": exact note reason '
                'or null, "say": verbatim text or null, "precedence": int (1 = checked first; reflect any '
                'precedence the documents state), "condition": null, or for a trigger decided by account data '
                'rather than by what the customer says: {"over": "each_product"|"all_products_sum", "field": '
                'response field, "op": ">"|">="|"<"|"<="|"==", "value": number (digits, also when the documents '
                'write it in words)}, "notes": [verbatim rule text and conditions]}]  -- every '
                'special situation/trigger the documents describe (anything the customer may say or do that '
                'changes the normal flow), with the exact action/queue/outcome/reason values they give',
    "notes": '{"tool_reasons": [exact reason enum values of the note tool the agent may use; leave out values '
             'the documents mark as retired, unused, not to be used or not for the agent], "when": verbatim rule for when a note '
             'is written, "forbidden_on_transfer": bool, "notes": []}',
    "outcomes": '{"table": [{"outcome": exact value, "reason": exact value or null, "when": text with the rule '
                'refs}] (one row per outcome/reason pair the agent may use; leave out values the documents mark '
                'as retired, unused, not to be used or reserved for others such as back office), "precedence": [verbatim rules on which outcome wins], '
                '"multi_product": verbatim rule for choosing the outcome with several products (or ""), "notes": []}',
    "phrases": '{<snake_case key>: verbatim sentence the documents require the agent to say, one entry per '
               'required sentence}',
    "closing": '{"promise": P, "id_fail": P, "callback": P, "no_callback": P, "refused": P, "service_error": P, '
               '"transfer": {<exact transfer queue>: P}, "note_map": {<exact note reason value>: P (the close '
               'outcome/reason the call gets when this note decides the outcome, e.g. through a mapping table '
               'or the no-promise rule)}, "transfer_overrides_promise": bool, "no_reason_outcomes": [outcomes '
               'closed without a reason], "notes": []}  where P = {"outcome": exact close value, "reason": exact '
               'close reason or null}. promise = at least one promise recorded; id_fail = identity check '
               'failed; callback / no_callback = callback booked / not possible; refused = no promise after the '
               'attempts or a refusal to talk; service_error = recording failed. If the close tool has no '
               'reason field, every reason is null',
}


def _contract_text(keys):
    return "\n".join("- %s: %s" % (k, CONTRACT[k]) for k in keys)


def _tools_for_prompt(tools):
    slim = {}
    for name, t in tools.items():
        slim[name] = {
            "summary": t.get("notes"),
            "request": {f: {k: v for k, v in i.items() if k in ("type", "enum", "required", "description", "format")}
                        for f, i in t["fields"].items()},
            "response": {f: _slim_field(i) for f, i in t.get("response_fields", {}).items()},
        }
    return json.dumps(slim, ensure_ascii=False)


def _slim_field(i):
    out = {k: v for k, v in i.items() if k in ("type", "enum", "description")}
    if "items" in i:
        out["items"] = _slim_field(i["items"])
    if "properties" in i:
        out["properties"] = {k: _slim_field(v) for k, v in i["properties"].items()}
    return out


def part_prompt(keys, docs_text, tools, api_description):
    return (
        "Extract the following sections of the policy JSON from the documents below.\n\n"
        "Return ONE JSON object with exactly these top-level keys: %s.\n\nSection contract:\n%s\n\n"
        "General: every section object may carry \"notes\": [verbatim sentences] for anything relevant the "
        "contract has no slot for. Do not drop any rule. Dates as YYYY-MM-DD, times as HH:MM (24h), counts as "
        "integers. When multiple documents conflict, the later one wins.\n\n"
        "Tool API (authoritative for tool fields and enum values):\n%s\n\nTool API description: %s\n\n"
        "Documents:\n%s\n"
        % (", ".join(keys), _contract_text(keys), _tools_for_prompt(tools), api_description, docs_text))


def rules_prompt(docs_text, only=None):
    """Verbatim rules prompt; `only` = names of the documents to list rules from (the rest is context)."""
    scope = ""
    if only:
        scope = ("- List ONLY the rules found in these documents: %s. The other documents are context: use them "
                 "for the override/change marks below, but do not list their rules.\n"
                 % ", ".join('"%s"' % n for n in only))
    return (
        "List EVERY rule in the documents below for the phone agent's behaviour on its calls, verbatim, in "
        "document order, as JSON: {\"rules\": [{\"id\": text, \"text\": verbatim text}]}.\n" + scope +
        "- Leave out instructions to the people who build, test or host the agent (building it, mocking or "
        "deploying services, submission, scoring/judging), setup steps, code, and test/sample data records: "
        "they are not rules for the agent.\n"
        "- A numbered rule keeps its number: id = \"<document name without extension>.<number>\".\n"
        "- Unnumbered rule sentences/bullets (also in mails, memos, code lists): id = \"<document name without "
        "extension>.<short section slug>.<k>\".\n"
        "- Tables: one entry per table row, written as the header names with that row's values, verbatim.\n"
        "- Include sub-bullets inside their rule's text. Keep the exact wording, numbers, quotes and placeholders.\n"
        "- If a later document or memo replaces a rule as a whole, keep both, and start the later one's text with "
        "\"[overrides <id>] \" (several ids comma-separated). The replaced rule is then left out of the agent's "
        "prompt, so use this mark ONLY for a full replacement (also when the later rule contradicts it, e.g. a tool "
        "that was never used is now required).\n"
        "- If a later rule changes only part of an earlier one (a number, an outcome, one case) or adds to it, keep "
        "both and start the later one's text with \"[changes <id>] \": the earlier rule stays in force for the rest."
        "\n\nDocuments:\n%s\n" % docs_text)


def resolve_prompt(conflicts, docs_text, tools):
    blocks = []
    for key, (a, b) in conflicts.items():
        blocks.append("### %s\nContract: %s\nCandidate A:\n%s\nCandidate B:\n%s"
                      % (key, CONTRACT[key], json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False)))
    return (
        "Two independent extractions of a policy JSON disagree on the sections below. Re-read the documents and "
        "decide the correct value of each section, following its contract exactly. You may take one candidate, "
        "merge them, or correct both. Later documents override earlier ones.\n\nReturn JSON: {<section>: "
        "{\"value\": <the correct section value>, \"evidence\": [short verbatim quotes from the documents that "
        "decide it]}} for every section listed.\n\n%s\n\nTool API:\n%s\n\nDocuments:\n%s\n"
        % ("\n\n".join(blocks), _tools_for_prompt(tools), docs_text))


# =============================================================== 4. diff of replicates

def _s(v):
    return "" if v is None else str(v).strip().lower()


def _kind(v, known):
    """Class of an attempt amount/date: a known keyword, else "expr" (free wording is not compared)."""
    v = _s(v)
    return v if v in known else "expr"


def fingerprint(key, value):
    """Structural summary of a section, ignoring wording, for comparing two extractions."""
    try:
        if key == "calendar":
            v = normalise_section("calendar", copy.deepcopy(value), [])
            return (tuple(sorted(v.get("weekend") or [])), tuple(sorted(v.get("holidays") or [])),
                    _s(v.get("roll_rule")))
        if key == "bands":
            out = []
            for b in value or []:
                d = b.get("deadline") or {}
                out.append((_int(b.get("min_days_late")), _int(b.get("max_days_late")), _s(d.get("kind")),
                            _int(d.get("n")), _s(d.get("counted_from")),
                            tuple((_int(a.get("n")), _kind(a.get("amount"), ("full", "floor")),
                                   _kind(a.get("date"), ("deadline", "end_of_week_business_day")))
                                  for a in (b.get("attempts") or []))))
            return tuple(sorted(out, key=str))
        if key == "identity":
            qs = sorted(value.get("questions") or [], key=lambda q: _int(q.get("order")) or 0)
            return (tuple((_s(q.get("tool_field")), _s(q.get("normalise"))) for q in qs),
                    _int(value.get("max_wrong")),
                    (_s((value.get("fail") or {}).get("outcome")), _s((value.get("fail") or {}).get("reason"))))
        if key == "callback":
            v = normalise_section("callback", copy.deepcopy(value), [])
            return (tuple(sorted(_s(d) for d in v.get("days") or [])), v.get("start"), v.get("end"),
                    bool(v.get("end_inclusive")), bool(v.get("must_be_after_now")))
        if key == "outcomes":
            return tuple(sorted({(_s(r.get("outcome")), _s(r.get("reason")))
                                 for r in (value.get("table") or [])}))
        if key == "closing":
            v = normalise_section("closing", copy.deepcopy(value), [])
            return tuple(sorted((k, json.dumps(x, sort_keys=True)) for k, x in v.items() if k != "notes"))
        if key == "triggers":
            return tuple(sorted({(_s(t.get("action")), _s(t.get("queue")), _s(t.get("outcome")),
                                  _s(t.get("reason")), _s(t.get("note_reason"))) for t in value or []}))
    except Exception:
        pass
    return json.dumps(value, sort_keys=True, default=str)


def diff_replicates(a, b, keys=DIFF_KEYS):
    """{key: (a_value, b_value)} for the keys whose structure differs."""
    out = {}
    for k in keys:
        if k in a and k in b and fingerprint(k, a[k]) != fingerprint(k, b[k]):
            out[k] = (a[k], b[k])
    return out


# =============================================================== 5. merge + normalise + validate

_IDENTITY_KEYS = {"bands": "name", "triggers": "name", "rules": "id", "variables": "name"}


def merge_policies(base, overlay, path="", overrides=None):
    """Deep merge, overlay wins. Lists of keyed items (bands/triggers/rules/variables, outcome table rows)
    merge by key; other lists are replaced. Replacements of non-empty values are logged in `overrides`."""
    if overrides is None:
        overrides = []
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            sub = (path + "." + k) if path else k
            if k in out:
                out[k] = merge_policies(out[k], v, sub, overrides)
            else:
                out[k] = copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(overlay, list):
        key = _IDENTITY_KEYS.get(path.split(".")[-1])
        if path.endswith("outcomes.table"):
            key = ("outcome", "reason")
        if path.endswith("notes") or path.endswith("rules") and key is None or path.endswith("overrides_applied"):
            return list(base) + [x for x in overlay if x not in base]
        if key and all(isinstance(x, dict) for x in base + overlay):
            def k_of(x):
                return tuple(x.get(k) for k in key) if isinstance(key, tuple) else x.get(key)
            out = [copy.deepcopy(x) for x in base]
            index = {k_of(x): i for i, x in enumerate(out)}
            for x in overlay:
                kx = k_of(x)
                if kx in index and kx is not None:
                    if out[index[kx]] != x:
                        overrides.append("%s[%s] replaced" % (path, kx))
                    out[index[kx]] = merge_policies(out[index[kx]], x, path + "[]", overrides)
                else:
                    out.append(copy.deepcopy(x))
            return out
        if base and overlay != base:
            overrides.append("%s replaced" % path)
        return copy.deepcopy(overlay)
    if overlay is None:
        return copy.deepcopy(base)
    if base not in (None, "", [], {}) and overlay != base:
        overrides.append("%s: %r -> %r" % (path, base, overlay))
    return copy.deepcopy(overlay)


def _int(v, default=None):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        if m:
            return int(m.group(0))
        words = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10}
        if v.strip().lower() in words:
            return words[v.strip().lower()]
    return default


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9


def norm_date(v):
    """Return 'YYYY-MM-DD' or None. Accepts ISO, Y/M/D, D.M.Y / D/M/Y (day first), '26 October 2026',
    'October 26, 2026'."""
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.strftime("%Y-%m-%d")
    if not isinstance(v, str):
        return None
    s = v.strip().lower().replace(",", " ")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s+", "", s)
    tries = []
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[t\s].*)?$", s)
    if m:
        tries.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", s)
    if m:  # day first; month first only when day-first is impossible (e.g. 12/25/2026)
        tries.append((int(m.group(3)), int(m.group(2)), int(m.group(1))))
        tries.append((int(m.group(3)), int(m.group(1)), int(m.group(2))))
    m = re.match(r"^(\d{1,2})\s+([a-z]+)\.?\s+(\d{4})$", s)
    if m and m.group(2) in _MONTHS:
        tries.append((int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))))
    m = re.match(r"^([a-z]+)\.?\s+(\d{1,2})\s+(\d{4})$", s)
    if m and m.group(1) in _MONTHS:
        tries.append((int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))))
    for y, mo, d in tries:
        try:
            return _dt.date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def norm_time(v):
    """Return 'HH:MM' (24h) or None. Accepts '9', '09:30', '9.30', '9am', '7 pm', '17h', '0930'."""
    if isinstance(v, int) and 0 <= v <= 24:
        return "%02d:00" % v
    if not isinstance(v, str):
        return None
    s = v.strip().lower().replace(" ", "")
    s = re.sub(r"^(\d{1,2})h(\d{2})$", r"\1:\2", s)
    s = re.sub(r"^(\d{1,2})h$", r"\1", s)
    m = re.match(r"^(\d{1,2})(?:[:.](\d{2}))?(am|pm|a\.m\.|p\.m\.)?$", s) or \
        re.match(r"^(\d{2})(\d{2})()$", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi < 60) and (h, mi) != (24, 0):
        return None
    return "%02d:%02d" % (h, mi)


_WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                          "sunday"], 1)}


def _weekday(v):
    """ISO weekday 1..7 from an int (0 = Sunday tolerated), digit string or day name."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
        i = int(v)
        return i if 1 <= i <= 7 else (7 if i == 0 else None)
    if isinstance(v, str):
        s = v.strip().lower()
        for name, n in _WEEKDAYS.items():
            if s == name or s == name[:3]:
                return n
    return None


def _items(v):
    """A list value as a list: strings are split on , ; and newlines (never iterated per character)."""
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,;\n]", v) if x.strip()]
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _notes(section):
    n = section.get("notes")
    if not isinstance(n, list):
        n = [] if n in (None, "") else [n]
        section["notes"] = n
    return n


def _as_dict(key, value, warnings):
    if isinstance(value, dict):
        return value
    if value in (None, "", []):
        return {}
    warnings.append("%s: expected an object, got %s; kept in notes" % (key, type(value).__name__))
    return {"notes": [value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)]}


def _as_list(key, value, warnings):
    if isinstance(value, list):
        return value
    if value in (None, "", {}):
        return []
    if isinstance(value, dict):
        # e.g. {"early": {...}, "late": {...}} -> list with name
        if all(isinstance(v, dict) for v in value.values()):
            return [dict(v, name=v.get("name", k)) for k, v in value.items()]
    warnings.append("%s: expected a list, got %s" % (key, type(value).__name__))
    return [value]


def norm_language(v, warnings=None):
    """Language name as text ("German"), or None when not stated. Accepts {"name": ...}, lists, codes."""
    if isinstance(v, dict):
        v = next((v[k] for k in ("name", "language", "value", "spoken") if v.get(k)), None)
    if isinstance(v, (list, tuple)):
        v = next((x for x in v if isinstance(x, str) and x.strip()), None)
    if not isinstance(v, str):
        if v is not None and warnings is not None:
            warnings.append("language: expected text, got %s" % type(v).__name__)
        return None
    v = re.sub(r"\s+", " ", v).strip().strip(".\"'")
    if not v or v.lower() in ("null", "none", "unknown", "n/a", "not stated"):
        return None
    return v[:1].upper() + v[1:] if v[:1].islower() else v


def normalise_section(key, v, warnings):
    """Coerce one section to its contract types. Bad values move into notes with a warning."""
    if key == "calendar":
        v = _as_dict(key, v, warnings)
        notes = _notes(v)
        wk = []
        for d in _items(v.get("weekend")):
            w = _weekday(d)
            if w is None:
                notes.append("unparsed weekend value: %r" % (d,))
                warnings.append("calendar.weekend: unparsed %r" % (d,))
            elif w not in wk:
                wk.append(w)
        v["weekend"] = sorted(wk)
        hol = []
        for d in _items(v.get("holidays")):
            raw = d.get("date") if isinstance(d, dict) else d
            iso = norm_date(raw)
            if iso is None:
                notes.append("unparsed holiday: %s" % json.dumps(d, ensure_ascii=False))
                warnings.append("calendar.holidays: unparsed %r" % (d,))
            elif iso not in hol:
                hol.append(iso)
        non = []
        for d in _items(v.get("non_holidays")):
            iso = norm_date(d.get("date") if isinstance(d, dict) else d)
            if iso and iso not in non:
                non.append(iso)
        v["non_holidays"] = sorted(non)
        v["holidays"] = sorted(h for h in hol if h not in non)
        ws = _weekday(v.get("week_starts"))
        v["week_starts"] = ws if ws is not None else default_week_start(v["weekend"])
        rr = _s(v.get("roll_rule")).replace(" ", "_").replace("-", "_")
        v["roll_rule"] = rr if rr in ("next_business_day", "previous_business_day", "none") else \
            ("next_business_day" if "next" in rr else "previous_business_day" if "prev" in rr else "none")
        return v
    if key == "callback":
        v = _as_dict(key, v, warnings)
        notes = _notes(v)
        for f in ("start", "end"):
            if f in v and v[f] is not None:
                t = norm_time(v[f])
                if t is None:
                    notes.append("unparsed callback %s: %r" % (f, v[f]))
                    warnings.append("callback.%s: unparsed %r" % (f, v[f]))
                v[f] = t
        days = v.get("days")
        days = [str(d) for d in days] if isinstance(days, list) else ([str(days)] if days else [])
        v["days"] = []
        for d in days:
            kw, cond = callback_day(d)
            if cond:
                notes.append("callback day: %s" % d)
                if kw == "today" and re.search(r"business|working|banking|open", cond):
                    v["today_requires_business_day"] = True
            if kw not in v["days"]:
                v["days"].append(kw)
        v["today_requires_business_day"] = _bool(v.get("today_requires_business_day"))
        for f in ("end_inclusive", "must_be_after_now"):
            if f in v:
                v[f] = _bool(v[f])
        pw = {}
        raw_pw = v.get("per_weekday")
        if isinstance(raw_pw, list):  # [{"weekday": .., "start": .., "end": ..}]
            raw_pw = {(x.get("weekday") or x.get("day")): x for x in raw_pw if isinstance(x, dict)}
        for day, win in (raw_pw or {}).items() if isinstance(raw_pw, dict) else []:
            wd = _weekday(day)
            if wd is None or not isinstance(win, dict):
                notes.append("unparsed per_weekday entry: %s" % json.dumps({str(day): win}, ensure_ascii=False))
                continue
            pw[str(wd)] = _window(win)
        v["per_weekday"] = pw
        pd = {}
        raw_pd = v.get("per_date")
        if isinstance(raw_pd, list):  # [{"date": .., "start": .., "end": ..}]
            raw_pd = {x.get("date"): x for x in raw_pd if isinstance(x, dict)}
        for day, win in (raw_pd or {}).items() if isinstance(raw_pd, dict) else []:
            iso = norm_date(day)
            if iso is None or not isinstance(win, dict):
                notes.append("unparsed per_date entry: %s" % json.dumps({str(day): win}, ensure_ascii=False))
                continue
            pd[iso] = _window(win)
        v["per_date"] = pd
        if v.get("max_asks") is not None:
            v["max_asks"] = _int(v.get("max_asks"))
        return v
    if key == "bands":
        v = _as_list(key, v, warnings)
        out = []
        for i, b in enumerate(v):
            if not isinstance(b, dict):
                warnings.append("bands[%d]: not an object" % i)
                continue
            for f in ("min_days_late", "max_days_late"):
                b[f] = _int(b.get(f))
            d = b.get("deadline")
            if not isinstance(d, dict):
                d = {"kind": "nth_business_day", "n": _int(d), "notes": [d]} if d is not None else {}
            d["n"] = _int(d.get("n"))
            d.setdefault("kind", "nth_business_day")
            d.setdefault("counted_from", "today")
            b["deadline"] = d
            atts = b.get("attempts")
            if isinstance(atts, (int, str)) and not isinstance(atts, bool):
                atts = [{"n": k + 1, "amount": None, "date": None, "text": ""} for k in range(_int(atts, 0))]
            atts = [a for a in (atts or []) if isinstance(a, dict)]
            for k, a in enumerate(atts):
                a["n"] = _int(a.get("n"), k + 1)
                a["date"] = date_expr(a.get("date"))
            b["attempts"] = atts
            b.setdefault("name", "band_%d" % (i + 1))
            if b["min_days_late"] is None or b["max_days_late"] is None or d["n"] is None:
                warnings.append("bands[%d]: missing min/max/deadline n" % i)
            out.append(b)
        return out
    if key == "identity":
        v = _as_dict(key, v, warnings)
        _notes(v)
        qs = [q for q in _as_list("identity.questions", v.get("questions"), warnings) if isinstance(q, dict)]
        for k, q in enumerate(qs):
            q["order"] = _int(q.get("order"), k + 1)
        v["questions"] = sorted(qs, key=lambda q: q["order"])
        v["max_wrong"] = _int(v.get("max_wrong"))
        scope = _s(v.get("max_wrong_scope")).replace("-", "_").replace(" ", "_")
        v["max_wrong_scope"] = "per_question" if ("per" in scope or "each" in scope) else "total"
        if not isinstance(v.get("fail"), dict):
            v["fail"] = {"outcome": None, "reason": None, "say": v.get("fail")}
        if not isinstance(v.get("wrong_counts_rules"), list):
            v["wrong_counts_rules"] = [v["wrong_counts_rules"]] if v.get("wrong_counts_rules") else []
        return v
    if key == "products":
        v = _as_dict(key, v, warnings)
        _notes(v)
        v["max_count"] = _int(v.get("max_count"), 1)
        if not isinstance(v.get("types"), dict):
            types = {}
            for t in v.get("types") or []:
                if isinstance(t, dict) and (t.get("type") or t.get("value")):
                    types[t.get("type") or t.get("value")] = t
            v["types"] = types
        v.setdefault("order", "as_returned")
        for t, info in v["types"].items():
            if not isinstance(info, dict):
                continue
            if not info.get("floor_field") and not info.get("floor_expr") and info.get("full_field"):
                # no lower amount stated: the floor is the full amount
                info["floor_field"] = info["full_field"]
        return v
    if key == "dates":
        v = _as_dict(key, v, warnings)
        _notes(v)
        nw = _s(v.get("next_weekday")).replace(" ", "_").replace("-", "_")
        v["next_weekday"] = "following_week" if "follow" in nw else ("next_occurrence" if "occur" in nw or
                                                                       "first" in nw else "")
        return v
    if key == "closing":
        v = _as_dict(key, v, warnings)
        out = {}
        for k2, val in v.items():
            if k2 in ("transfer", "note_map") and isinstance(val, dict):
                out[k2] = {str(q): _pair(x) for q, x in val.items() if _pair(x)}
            elif k2 == "transfer_overrides_promise":
                out[k2] = _bool(val)
            elif k2 == "no_reason_outcomes":
                out[k2] = [str(x) for x in val] if isinstance(val, list) else ([str(val)] if val else [])
            elif k2 == "notes":
                out[k2] = val if isinstance(val, list) else [val]
            elif isinstance(val, dict):
                if _pair(val):
                    out[k2] = _pair(val)
        return out
    if key == "promise":
        v = _as_dict(key, v, warnings)
        _notes(v)
        v["retry_on_failure"] = _int(v.get("retry_on_failure"), 0)
        for f in ("confirm_required", "per_product"):
            if f in v:
                v[f] = _bool(v[f])
        return v
    if key == "triggers":
        v = _as_list(key, v, warnings)
        out = []
        for i, t in enumerate(v):
            if not isinstance(t, dict):
                t = {"name": "trigger_%d" % (i + 1), "detect": str(t), "action": "say"}
            t["precedence"] = _int(t.get("precedence"), 100 + i)
            c = t.get("condition")
            if isinstance(c, dict):
                val = c.get("value")
                num = _number(val)
                if num is None:
                    warnings.append("triggers.%s: condition value %r is not a number" % (t.get("name"), val))
                c["value"] = num if num is not None else val
                op = _s(c.get("op")).replace(" ", "")
                c["op"] = op if op in (">", ">=", "<", "<=", "==") else ">"
                over = _s(c.get("over"))
                c["over"] = "all_products_sum" if ("sum" in over or "total" in over or "all" in over) else "each_product"
            else:
                t["condition"] = None
            for f in ("queue", "outcome", "reason", "note_reason", "say"):
                t.setdefault(f, None)
            out.append(t)
        return out
    if key == "language":
        return norm_language(v, warnings)
    if key == "variables":
        v = _as_list(key, v, warnings)
        out = []
        for x in v:
            x = dict(x) if isinstance(x, dict) else {"name": str(x), "meaning": ""}
            if "example" in x:  # one value as the documents show it; test calls use it
                ex = x["example"]
                x["example"] = None if ex in (None, "", [], {}) else (
                    ex if isinstance(ex, (str, int, float)) else json.dumps(ex, ensure_ascii=False))
            out.append(x)
        return out
    if key == "rules":
        v = _as_list(key, v, warnings)
        out = []
        for i, r in enumerate(v):
            if isinstance(r, dict) and r.get("text"):
                out.append({"id": str(r.get("id") or "r%d" % (i + 1)), "text": str(r["text"])})
            elif isinstance(r, str) and r.strip():
                out.append({"id": "r%d" % (i + 1), "text": r})
        return out
    if key == "outcomes":
        v = _as_dict(key, v, warnings)
        _notes(v)
        table = []
        for r in _as_list("outcomes.table", v.get("table"), warnings):
            if isinstance(r, dict) and r.get("outcome"):
                reasons = r.get("reason")
                if isinstance(reasons, list):  # one row per pair
                    for rs in reasons or [None]:
                        table.append(dict(r, reason=rs))
                else:
                    table.append(r)
        v["table"] = table
        if not isinstance(v.get("precedence"), list):
            v["precedence"] = [v["precedence"]] if v.get("precedence") else []
        v.setdefault("multi_product", "")
        return v
    if key == "phrases":
        v = _as_dict(key, v, warnings)
        out = {}
        for k, x in v.items():
            if k == "notes":
                out[k] = x if isinstance(x, list) else [x]
            else:
                out[str(k)] = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        return out
    if key == "notes":
        v = _as_dict(key, v, warnings)
        _notes(v)
        if not isinstance(v.get("tool_reasons"), list):
            v["tool_reasons"] = [] if not v.get("tool_reasons") else [v["tool_reasons"]]
        if "forbidden_on_transfer" in v:
            v["forbidden_on_transfer"] = _bool(v["forbidden_on_transfer"])
        return v
    if key == "source":
        v = _as_dict(key, v, warnings)
        for f in ("briefs", "overrides_applied"):
            if not isinstance(v.get(f), list):
                v[f] = [v[f]] if v.get(f) else []
        return v
    if key in LIST_KEYS:
        return _as_list(key, v, warnings)
    if key == "tools":
        return v if isinstance(v, dict) else {}
    v = _as_dict(key, v, warnings)
    _notes(v)
    return v


def default_week_start(weekend):
    """ISO weekday a week starts on when the documents do not say: the first business day after the weekend
    (Saturday+Sunday weekend -> Monday). Monday when there is no weekend."""
    wk = set(weekend or [])
    for d in range(1, 8):
        prev = 7 if d == 1 else d - 1
        if d not in wk and prev in wk:
            return d
    return 1


_DAY_KEYWORDS = [("next_business_day", r"next[ _](?:business|working|banking)[ _]day"), ("today", r"today"),
                 ("tomorrow", r"tomorrow")]


def callback_day(d):
    """('keyword', 'condition text') for a callback day: 'today (only if a business day)' ->
    ('today', 'only if a business day'). Unknown wording is returned unchanged with no condition."""
    s = str(d or "").strip()
    low = s.lower()
    m = re.match(r"^business_days?[:_ ](\d+)$", low)
    if m:
        return "business_days:%s" % m.group(1), ""
    for kw, pat in _DAY_KEYWORDS:
        m = re.match(r"^(?:the )?%s\b(.*)$" % pat, low)
        if m:
            return kw, m.group(1).strip(" ()-,;:")
    return s, ""


def _window(win):
    out = {"start": norm_time(win.get("start")), "end": norm_time(win.get("end"))}
    if win.get("end_inclusive") is not None:
        out["end_inclusive"] = _bool(win.get("end_inclusive"))
    return out


def _pair(x):
    """{"outcome", "reason"} from a dict / [outcome, reason] / "outcome / reason"; None when no outcome."""
    if isinstance(x, dict):
        o, r = x.get("outcome"), x.get("reason")
    elif isinstance(x, (list, tuple)) and x:
        o, r = x[0], (x[1] if len(x) > 1 else None)
    elif isinstance(x, str) and x.strip():
        parts = [y.strip(" `") for y in x.split("/")]
        o, r = parts[0], (parts[1] if len(parts) > 1 else None)
    else:
        return None
    if not o:
        return None
    return {"outcome": str(o), "reason": (str(r) if r not in (None, "", "none", "null") else None)}


_ORDINALS = {w: i for i, w in enumerate(["zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
                                         "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
                                         "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
                                         "nineteenth", "twentieth"])}
_CARDINALS = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                                          "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
                                          "sixteen", "seventeen", "eighteen", "nineteen", "twenty"])}


def date_expr(v):
    """Attempt date -> a DATE_OPTIONS keyword when the wording is a plain business-day count, else unchanged.
    'third working day after today' / '3rd business day' / 'in 3 business days' -> 'nth_business_day:3';
    'next business day (after today)' -> 'next_business_day'."""
    if not isinstance(v, str):
        return v
    s = v.strip().lower()
    m = re.match(r"^nth_business_day[:_ ]?(\d+)$", s)
    if m:
        return "nth_business_day:%s" % m.group(1)
    day = r"(?:business|working|banking|bank)[ _-]days?"
    if re.match(r"^(?:the )?next[ _]%s(?: after today)?\.?$" % day, s):
        return "next_business_day"
    m = re.match(r"^(?:the |in )?(\d+(?:st|nd|rd|th)?|[a-z]+)[ _]%s(?: after today| from today| later)?\.?$" % day, s)
    if m:
        w = re.sub(r"^(\d+)(?:st|nd|rd|th)$", r"\1", m.group(1))
        n = int(w) if w.isdigit() else _ORDINALS.get(w, _CARDINALS.get(w))
        if n:
            return "nth_business_day:%d" % n
    return v


def _number(v):
    """int/float from a number, '3,000', '3000.50', 'EUR 3 000' or simple words ('three thousand'); else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if not isinstance(v, str):
        return None
    t = v.strip().lower()
    m = re.search(r"-?\d[\d ,.]*", t)
    if m:
        raw = _plain_number(m.group(0).strip(" ,."))
        try:
            f = float(raw)
            return int(f) if f.is_integer() else f
        except ValueError:
            pass
    total, cur, seen = 0, 0, False
    for w in re.findall(r"[a-z]+", t.replace("-", " ")):
        if w in _CARDINALS:
            cur += _CARDINALS[w]
            seen = True
        elif w in _TENS:
            cur += _TENS[w]
            seen = True
        elif w == "hundred":
            cur = (cur or 1) * 100
            seen = True
        elif w in ("thousand", "million"):
            total += (cur or 1) * (1000 if w == "thousand" else 1000000)
            cur = 0
            seen = True
    return total + cur if seen else None


def _plain_number(s):
    """'1.234,56' / '1,234.56' / '3 000' / '12,5' -> '1234.56' / '1234.56' / '3000' / '12.5'. The last of
    '.'/',' is the decimal mark when both appear; a single mark followed by exactly 3 digits (once or
    repeatedly) is a thousands separator."""
    s = s.replace(" ", "")
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        th = "," if dec == "." else "."
        return s.replace(th, "").replace(dec, ".")
    for mark in (",", "."):
        if mark in s:
            parts = s.split(mark)
            if len(parts) > 2 or (len(parts[-1]) == 3 and parts[0].lstrip("-")):
                if all(len(p) == 3 for p in parts[1:]):
                    return s.replace(mark, "")
            return s.replace(mark, ".") if len(parts) == 2 else s.replace(mark, "")
    return s


_TENS = {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def eval_amount_expr(expr, fields):
    """Evaluate a floor/amount expression such as 'min(50, due)', 'max(10%, minimum)' or 'due * 0.1'
    over a product's response fields. Only numbers, field names, + - * /, min, max, 'N%' (= N/100) and the
    rounding functions round_half_up(x, d) / round(x, d), round_up(x, d) / ceil(x), round_down(x, d) / floor(x)
    (d decimals, default 0). Returns a float rounded to the cent, or None; never raises."""
    import ast
    import decimal

    def rnd(x, d, mode):
        q = decimal.Decimal(1).scaleb(-int(d))
        x = decimal.Decimal(repr(round(float(x), 9)))
        return float(x.quantize(q, rounding=mode))

    funcs = {"round_half_up": decimal.ROUND_HALF_UP, "round": decimal.ROUND_HALF_UP,
             "round_up": decimal.ROUND_CEILING, "ceil": decimal.ROUND_CEILING, "ceiling": decimal.ROUND_CEILING,
             "round_down": decimal.ROUND_FLOOR, "floor": decimal.ROUND_FLOOR}
    try:
        src = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100.0)", str(expr or ""))
        tree = ast.parse(src, mode="eval")

        def ev(n):
            if isinstance(n, ast.Expression):
                return ev(n.body)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
                return float(n.value)
            if isinstance(n, ast.Name):
                v = fields.get(n.id)
                return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else float(str(v))
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
                return -ev(n.operand)
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                a, b = ev(n.left), ev(n.right)
                return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b}.get(type(n.op), a / b if b else None)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("min", "max") and n.args:
                vals = [ev(a) for a in n.args]
                return min(vals) if n.func.id == "min" else max(vals)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in funcs and n.args:
                d = ev(n.args[1]) if len(n.args) > 1 else 0
                return rnd(ev(n.args[0]), d, funcs[n.func.id])
            raise ValueError("unsupported")
        out = ev(tree)
        return round(out, 2) if out is not None else None
    except Exception:
        return None


def _bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "y")
    return bool(v)


def _enum_index(tools, only=None):
    """field name -> set of enum values, across tools (request + response, nested too).
    only(tool_dict) -> bool restricts which tools are indexed."""
    idx = {}

    def walk(fields):
        for f, i in (fields or {}).items():
            if not isinstance(i, dict):
                continue
            if i.get("enum"):
                idx.setdefault(f, set()).update(i["enum"])
            walk(i.get("properties"))
            if isinstance(i.get("items"), dict):
                walk(i["items"].get("properties"))

    for t in tools.values():
        if only is None or only(t):
            walk(t.get("fields"))
            walk(t.get("response_fields"))
    return idx


def cross_check(policy, warnings):
    """Warn when policy values are not allowed by the tool API (values kept; generator decides).
    Close outcomes/reasons are checked against the tool(s) that take an `outcome` field; note reasons
    against the other tools, so equal field names on different tools do not mix."""
    tools = policy.get("tools") or {}
    idx = _enum_index(tools)
    closing = _enum_index(tools, lambda t: "outcome" in (t.get("fields") or {}))
    others = _enum_index(tools, lambda t: "outcome" not in (t.get("fields") or {}))

    def chk(where, field, val, index):
        allowed = index.get(field)
        if val and allowed and val not in allowed:
            warnings.append("%s: %s=%r not in tool enum %s" % (where, field, val, sorted(allowed)))

    for r in (policy.get("outcomes") or {}).get("table") or []:
        chk("outcomes.table", "outcome", r.get("outcome"), closing)
        chk("outcomes.table", "reason", r.get("reason"), closing)
    for t in policy.get("triggers") or []:
        where = "triggers.%s" % t.get("name")
        chk(where, "queue", t.get("queue"), idx)
        chk(where, "outcome", t.get("outcome"), closing)
        chk(where, "reason", t.get("reason"), closing)
        chk(where, "reason", t.get("note_reason"), others)
    for r in (policy.get("notes") or {}).get("tool_reasons") or []:
        chk("notes.tool_reasons", "reason", r, others)
    all_values = set().union(*idx.values()) if idx else set()
    fields = {f for t in tools.values() for f in (t.get("fields") or {})}
    for q in (policy.get("identity") or {}).get("questions") or []:
        tf = q.get("tool_field")
        if tf and tf not in all_values and tf not in fields:
            warnings.append("identity.questions: tool_field %r not found in tool API" % tf)
    if closing.get("outcome"):
        listed = {r.get("outcome") for r in (policy.get("outcomes") or {}).get("table") or []}
        missing = closing["outcome"] - listed
        if missing:
            warnings.append("outcomes.table: tool outcomes never listed: %s" % sorted(missing))
    ptypes = idx.get("type")
    if ptypes:
        for t in ((policy.get("products") or {}).get("types") or {}):
            if t not in ptypes:
                warnings.append("products.types: %r not in tool enum %s" % (t, sorted(ptypes)))


def local_validate(policy):
    """Light contract check (until policy_schema.validate lands). Returns a list of problems."""
    problems = []
    if not isinstance(policy, dict):
        return ["policy is not an object"]
    for k in TOP_KEYS:
        if k not in policy:
            if k not in TEXT_KEYS:  # optional text sections (language) may be absent
                problems.append("missing top-level key: %s" % k)
        elif k in TEXT_KEYS:
            if policy[k] is not None and not isinstance(policy[k], str):
                problems.append("%s must be text or null" % k)
        elif k in LIST_KEYS and not isinstance(policy[k], list):
            problems.append("%s must be a list" % k)
        elif k not in LIST_KEYS and not isinstance(policy[k], dict):
            problems.append("%s must be an object" % k)
    cal = policy.get("calendar") or {}
    for d in cal.get("holidays") or []:
        if norm_date(d) != d:
            problems.append("calendar.holidays: not ISO: %r" % (d,))
    for w in cal.get("weekend") or []:
        if not isinstance(w, int) or not 1 <= w <= 7:
            problems.append("calendar.weekend: bad weekday %r" % (w,))
    cb = policy.get("callback") or {}
    for f in ("start", "end"):
        if cb.get(f) is not None and not re.match(r"^\d{2}:\d{2}$", str(cb.get(f))):
            problems.append("callback.%s: not HH:MM" % f)
    for i, b in enumerate(policy.get("bands") or []):
        for f in ("min_days_late", "max_days_late"):
            if not isinstance(b.get(f), int):
                problems.append("bands[%d].%s: not an int" % (i, f))
        if not isinstance((b.get("deadline") or {}).get("n"), int):
            problems.append("bands[%d].deadline.n: not an int" % i)
    if not isinstance((policy.get("identity") or {}).get("max_wrong"), int):
        problems.append("identity.max_wrong: not an int")
    if not isinstance((policy.get("products") or {}).get("max_count"), int):
        problems.append("products.max_count: not an int")
    if not policy.get("tools"):
        problems.append("tools: empty")
    return problems


def validate(policy):
    """Problems list. Uses builder.policy_schema.validate when available (T04), else local_validate."""
    if _schema_validate is not None:
        try:
            res = _schema_validate(policy)
            if res is None or res is True:
                return []
            if isinstance(res, (list, tuple)):
                return [str(x) for x in res]
            if isinstance(res, dict) and "errors" in res:
                return [str(x) for x in res["errors"]]
            return []
        except Exception as e:
            return [str(e)]
    return local_validate(policy)


def finalise(parts, tools_info, docs, warnings):
    """Combine part dicts into one policy, normalise every section, add the deterministic tools."""
    policy = {}
    overrides = []
    for part in parts:
        if isinstance(part, dict):
            policy = merge_policies(policy, {k: v for k, v in part.items()
                                             if k in TOP_KEYS + OPTIONAL_KEYS and k != "tools"}, "", overrides)
    policy["tools"] = tools_info["tools"]  # the supplied API is authoritative
    for k in TOP_KEYS + [k for k in OPTIONAL_KEYS if k in policy]:
        try:
            policy[k] = normalise_section(k, policy.get(k), warnings)
        except Exception as e:  # never crash on an odd shape: keep the raw value in notes
            warnings.append("%s: normalise failed (%s)" % (k, e))
            raw = policy.get(k)
            policy[k] = [] if k in LIST_KEYS else {"notes": [json.dumps(raw, ensure_ascii=False, default=str)]}
    src = policy["source"]
    names = [label for label, _ in docs]
    if [b for b in src.get("briefs") or []] != names:
        src["briefs"] = names
    if tools_info.get("servers"):
        src.setdefault("api_servers", tools_info["servers"])
    # notes.tool_reasons must be real enum values of the tool API when the model left it empty
    align_close_reasons(policy, warnings)
    cross_check(policy, warnings)
    return {k: policy[k] for k in TOP_KEYS + OPTIONAL_KEYS if k in policy}


def close_tool(policy):
    """(tool name, outcome field, has_reason_field) of the tool that closes the call: the tool with the enum
    field sharing most values with the outcomes the policy uses. (None, None, True) when not found."""
    outs = {r.get("outcome") for r in (policy.get("outcomes") or {}).get("table") or []}
    outs.add(((policy.get("identity") or {}).get("fail") or {}).get("outcome"))
    outs.discard(None)
    best = (0, None, None)
    for name, t in (policy.get("tools") or {}).items():
        for f, info in (t.get("fields") or {}).items():
            n = len(outs & set(info.get("enum") or []))
            if n > best[0]:
                best = (n, name, f)
    if not best[1]:
        return None, None, True
    fields = policy["tools"][best[1]].get("fields") or {}
    others = [f for f, info in fields.items() if f != best[2]
              and not re.search(r"(^|_)(id|ref)$|customer|^id", f.lower())]
    return best[1], best[2], bool(others)


def align_close_reasons(policy, warnings):
    """When the close tool takes no reason, every close reason in the policy is dropped (the outcome value
    alone is recorded); free-text reasons the model wrote would otherwise reach the tool call."""
    name, _field, has_reason = close_tool(policy)
    if has_reason or not name:
        return
    n = 0

    def clear(d, key="reason"):
        nonlocal n
        if isinstance(d, dict) and d.get(key) not in (None, ""):
            d[key] = None
            n += 1

    seen, table = set(), []
    for r in (policy.get("outcomes") or {}).get("table") or []:
        clear(r)
        if r.get("outcome") not in seen:
            seen.add(r.get("outcome"))
            table.append(r)
    if "outcomes" in policy:
        policy["outcomes"]["table"] = table
    clear((policy.get("identity") or {}).get("fail"))
    for c in (policy.get("third_party") or {}).get("cases") or []:
        clear(c)
    for t in policy.get("triggers") or []:
        clear(t)
    cl = policy.get("closing") or {}
    for k, v in cl.items():
        if k in ("transfer", "note_map") and isinstance(v, dict):
            for x in v.values():
                clear(x)
        else:
            clear(v)
    if n:
        warnings.append("close tool %s has no reason field: %d close reasons dropped" % (name, n))


# =============================================================== deterministic rule fallback

def numbered_rules(docs):
    """Numbered list items ('12. text' + continuation lines) per document, verbatim."""
    rules = []
    for label, text in docs:
        stem = re.sub(r"\.[^.]+$", "", label.rsplit("/", 1)[-1])
        cur = None
        for line in text.splitlines():
            m = re.match(r"^\s{0,3}(\d{1,3})[.)]\s+(\S.*)$", line)
            if m:
                if cur:
                    rules.append(cur)
                cur = {"id": "%s.%s" % (stem, m.group(1)), "text": m.group(2).strip()}
            elif cur and line.strip() and (line.startswith((" ", "\t")) or line.lstrip().startswith(("-", "*"))):
                cur["text"] += "\n" + line.strip()
            elif cur and not line.strip():
                continue
            elif cur:
                rules.append(cur)
                cur = None
        if cur:
            rules.append(cur)
    return rules


def document_rules(docs):
    """Per document: its numbered rules, or (a document without numbered items, e.g. a mail or a meeting
    transcript) its paragraphs verbatim. Used for a rules part whose model call failed, so no document's
    rules are silently dropped."""
    rules = []
    for label, text in docs:
        got = numbered_rules([(label, text)])
        if not got:
            stem = re.sub(r"\.[^.]+$", "", label.rsplit("/", 1)[-1])
            paras = [x.strip() for x in re.split(r"\n\s*\n", text or "") if x.strip()]
            got = [{"id": "%s.p%d" % (stem, i + 1), "text": t} for i, t in enumerate(paras)]
        rules += got
    return rules


# =============================================================== main entry

def _call_json(prompt, purpose, deadline, model=None):
    """JSON model call that must finish by the absolute time `deadline`."""
    return llm.ask_json(prompt, system=SYSTEM, model=model, purpose=purpose,
                        timeout_s=max(0.0, deadline - time.time()), deadline=deadline)


def rules_groups(docs, chunk=None):
    """Consecutive document index groups for the verbatim rules pass: one group for a normal input; a big
    input is split (by document, about equal sizes) so each part's long verbatim answer stays short."""
    chunk = chunk or RULES_CHUNK_CHARS
    sizes = [len(t) for _, t in docs]
    n = min(MAX_RULES_PARTS, len(docs), max(1, -(-sum(sizes) // chunk)))
    if n <= 1:
        return [list(range(len(docs)))]
    target, groups, cur, acc = sum(sizes) / n, [], [], 0
    for i, size in enumerate(sizes):
        cur.append(i)
        acc += size
        if acc >= target * (len(groups) + 1) and len(groups) < n - 1 and i < len(sizes) - 1:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _merge_and_resolve(results, replicates, docs_text, tools_info, end, warnings):
    """Merge each replicate's two parts; settle disagreements between replicates with a resolve call."""
    reps = []
    for r in range(replicates):
        p1, p2 = results.get(("p1", r)), results.get(("p2", r))
        if p1 and p2:
            reps.append(merge_policies(p1, p2))
    if not reps:  # mix surviving halves
        p1 = next((results[("p1", r)] for r in range(replicates) if results.get(("p1", r))), None)
        p2 = next((results[("p2", r)] for r in range(replicates) if results.get(("p2", r))), None)
        if not p1 or not p2:
            raise ExtractError("policy extraction failed: %s" % "; ".join(warnings[-4:]))
        reps = [merge_policies(p1, p2)]
    primary = reps[0]

    if len(reps) > 1:
        conflicts = diff_replicates(reps[0], reps[1])
        if conflicts:
            _log("replicates disagree on: %s -> resolve call" % ", ".join(conflicts))
            left = end - time.time()
            if left >= MIN_RESOLVE_S:
                try:
                    res = _call_json(resolve_prompt(conflicts, docs_text, tools_info["tools"]),
                                     "resolve disagreement on " + ", ".join(conflicts), end)
                    for k in conflicts:
                        item = res.get(k) if isinstance(res, dict) else None
                        if isinstance(item, dict) and "value" in item:
                            primary[k] = item["value"]
                            ev = item.get("evidence")
                            if ev:
                                warnings.append("resolved %s; evidence: %s" % (k, json.dumps(ev, ensure_ascii=False)[:300]))
                        else:
                            warnings.append("resolve: no value for %s; kept replicate 1" % k)
                except Exception as e:
                    warnings.append("resolve call failed (%s); kept replicate 1" % e)
            else:
                warnings.append("no time left to resolve %s; kept replicate 1" % ", ".join(conflicts))
        else:
            _log("replicates agree on %s" % ", ".join(DIFF_KEYS))
    return primary


def extract_policy(brief_paths, openapi_path, *, replicates=2, budget_s=None, warnings=None):
    """brief file(s) + openapi -> policy dict (contract v1). Raises ExtractError if the LLM fails entirely."""
    t0 = time.time()
    budget_s = budget_s or TOTAL_BUDGET_S
    warnings = [] if warnings is None else warnings
    docs = read_documents(brief_paths)
    if not docs:
        raise ExtractError("no brief documents found in %s" % brief_paths)
    tools_info = parse_openapi(openapi_path)
    docs_text = documents_block(docs)
    api_desc = tools_info.get("description", "")
    replicates = max(1, int(replicates))
    end = t0 + budget_s - END_MARGIN_S
    round1_end = t0 + budget_s * ROUND1_SHARE

    results = {}

    def collect(key, fut):
        try:
            res = fut.result()
            results[key] = res if isinstance(res, dict) else {}
            if not isinstance(res, dict):
                warnings.append("%s: model returned %s, not an object" % (key, type(res).__name__))
        except Exception as e:
            warnings.append("%s failed: %s" % (key, e))
            _log("%s failed: %s" % (key, e))

    jobs = {}
    groups = rules_groups(docs)
    pool = cf.ThreadPoolExecutor(max_workers=2 * replicates + len(groups))
    try:
        for r in range(replicates):
            jobs[("p1", r)] = pool.submit(_call_json, part_prompt(PART1_KEYS, docs_text, tools_info["tools"], api_desc),
                                          "extract policy part 1 (replicate %d)" % (r + 1), round1_end)
            jobs[("p2", r)] = pool.submit(_call_json, part_prompt(PART2_KEYS, docs_text, tools_info["tools"], api_desc),
                                          "extract policy part 2 (replicate %d)" % (r + 1), round1_end)
        # the verbatim rules list (long answer) is needed last: it may run on while replicates are compared and
        # resolved, until the end of the whole budget; a big input is listed in parallel parts
        rules_jobs = []
        for gi, g in enumerate(groups):
            only = [docs[i][0] for i in g] if len(groups) > 1 else None
            purpose = "extract verbatim rules" + (" (part %d of %d)" % (gi + 1, len(groups)) if only else "")
            rules_jobs.append(pool.submit(_call_json, rules_prompt(docs_text, only), purpose, end, llm.fast_model()))
        for key, fut in jobs.items():
            collect(key, fut)
        _log("round 1 done in %.0fs" % (time.time() - t0))
        primary = _merge_and_resolve(results, replicates, docs_text, tools_info, end, warnings)
        for gi, fut in enumerate(rules_jobs):
            collect(("rules", gi), fut)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    rules = []
    for gi, g in enumerate(groups):
        part = normalise_section("rules", (results.get(("rules", gi)) or {}).get("rules"), warnings)
        if not part:  # the model call failed or listed nothing: keep these documents' text verbatim
            part = document_rules([docs[i] for i in g])
            warnings.append("rules part %d returned no rules; kept %d numbered rules/paragraphs of its documents "
                            "verbatim" % (gi + 1, len(part)))
        elif len(groups) > 1:
            det_g = numbered_rules([docs[i] for i in g])
            if len(part) < 0.8 * len(det_g):  # this part failed: its numbered rules at least
                warnings.append("rules part %d returned %d rules, numbered parse found %d; using numbered parse"
                                % (gi + 1, len(part), len(det_g)))
                part = det_g
        rules += [r for r in part if r not in rules]
    det = numbered_rules(docs)
    if len(rules) < 0.8 * len(det):
        warnings.append("rules pass returned %d rules, numbered parse found %d; using numbered parse"
                        % (len(rules), len(det)))
        rules = det
    policy = finalise([primary, {"rules": rules}], tools_info, docs, warnings)
    for p in validate(policy):
        warnings.append("validate: " + p)
    _log("extraction done in %.0fs" % (time.time() - t0))
    return policy


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("briefs", nargs="+", help="brief file(s) in order; later ones override earlier ones")
    ap.add_argument("--openapi", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--trace", help="append one JSON line per model call to this file")
    ap.add_argument("--replicates", type=int, default=2)
    args = ap.parse_args(argv)

    trace_f = None
    if args.trace:
        pathlib.Path(args.trace).parent.mkdir(parents=True, exist_ok=True)
        trace_f = open(args.trace, "a")

        def hook(rec):
            trace_f.write(json.dumps(rec) + "\n")
            trace_f.flush()
        llm.on_call(hook)
    llm.on_call(lambda r: _log("llm %s: %s %.0fs %s" % (r["model"], r["purpose"], r["duration_s"],
                                                       "ok" if r["ok"] else "FAILED " + str(r["error"]))))
    warnings = []
    t0 = time.time()
    policy = extract_policy(args.briefs, args.openapi, replicates=args.replicates, warnings=warnings)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(policy, indent=1, ensure_ascii=False) + "\n")
    for w in warnings:
        _log("warning: " + w)
    _log("wrote %s (%d rules, %d bands, %d triggers) in %.0fs"
         % (out, len(policy["rules"]), len(policy["bands"]), len(policy["triggers"]), time.time() - t0))
    if trace_f:
        trace_f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
