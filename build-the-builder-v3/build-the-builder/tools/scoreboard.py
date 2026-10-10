#!/usr/bin/env python3
"""Scoreboard: one place to see whether test results are improving.

Reads results/log.jsonl (shared log) and renders a terminal table, markdown,
or a static auto-refreshing HTML page.  Stdlib only, python 3.9+.

  python tools/scoreboard.py            terminal table + summary
  python tools/scoreboard.py --md       also write results/SCOREBOARD.md
  python tools/scoreboard.py --html     also write results/scoreboard.html
  python tools/scoreboard.py --watch    re-render every 60 s
"""
import argparse
import html
import json
import os
import sys
import time

DEFAULT_LOG = "/Users/ulas/Desktop/Freya/build-the-builder-v3/build-the-builder/results/log.jsonl"
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_IDS = ("0178a85a", "9ce4eaee")
LAST_N = 30
HEAD = ["time", "label/slot", "bank", "source", "style", "reps", "pass", "all-reps",
        "%", "med s", "failed"]


def load_rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except (OSError, UnicodeDecodeError):
        pass
    return rows


def load_slots(kit=KIT):
    out = {}
    for base in (kit, os.path.dirname(DEFAULT_LOG).rsplit("/results", 1)[0]):
        try:
            with open(os.path.join(base, ".freya-agents.json"), encoding="utf-8") as f:
                data = json.load(f)
            for slot, v in data.items():
                if isinstance(v, dict) and v.get("agent_id"):
                    out.setdefault(v["agent_id"], slot)
        except (OSError, ValueError):
            pass
    return out


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def norm(d, slots):
    """Flatten one log line into a display record; never raises."""
    aid = str(d.get("agent_id") or "")
    slot = d.get("slot") or slots.get(aid) or (aid[:8] if aid else "?")
    label = d.get("label")
    if aid.startswith(BASELINE_IDS):
        label = "baseline"
        who = "baseline"
    else:
        who = "baseline" if str(label or "").startswith("baseline") else "ours"
        label = label or slot
    passed, total = num(d.get("passed")), num(d.get("total"))
    pct = (100.0 * passed / total) if passed is not None and total else None
    failed = d.get("failed")
    if not isinstance(failed, list):
        pc = d.get("per_case")
        failed = [k for k, v in pc.items() if str(v) != "PASS"] if isinstance(pc, dict) else []
    runs = d.get("reps") or d.get("runs")
    return {
        "at": str(d.get("at") or "?"), "who": who, "slot": str(slot), "label": str(label),
        "bank": str(d.get("bank") or d.get("brief") or "-"),
        "source": str(d.get("source") or "public"), "style": str(d.get("style") or "-"),
        "reps": "-" if runs is None else str(runs),
        "passed": passed, "total": total, "pct": pct,
        "all": d.get("pass_all_reps"), "med": num(d.get("median_turn_s")),
        "failed": [str(x) for x in failed],
    }


def fmt_row(r):
    ps = "-" if r["passed"] is None or r["total"] is None else "%d/%d" % (r["passed"], r["total"])
    fl = ",".join(r["failed"])
    if len(fl) > 28:
        fl = fl[:27] + "~"
    lab = r["label"] if r["label"] == r["slot"] else "%s/%s" % (r["label"], r["slot"])
    return [r["at"][5:16], lab, r["bank"], r["source"], r["style"], r["reps"], ps,
            "-" if r["all"] is None else str(r["all"]),
            "-" if r["pct"] is None else "%.0f%%" % r["pct"],
            "-" if r["med"] is None else "%.1f" % r["med"], fl]


def summary(recs):
    """Per bank: best/latest for ours vs baseline + trend vs previous same slot+bank+style."""
    out = []
    for bank in sorted({r["bank"] for r in recs}):
        line = [bank]
        for who in ("ours", "baseline"):
            rs = [r for r in recs if r["bank"] == bank and r["who"] == who and r["pct"] is not None]
            if not rs:
                line.append("-")
                line.append("-")
                continue
            last = rs[-1]
            prev = [r for r in rs[:-1] if r["slot"] == last["slot"] and r["style"] == last["style"]]
            arrow = "="
            if prev:
                arrow = "^" if last["pct"] > prev[-1]["pct"] else ("v" if last["pct"] < prev[-1]["pct"] else "=")
            else:
                arrow = "new"
            line.append("%.0f%%" % max(r["pct"] for r in rs))
            line.append("%.0f%% %s" % (last["pct"], arrow))
        out.append(line)
    return out


SUM_HEAD = ["bank", "ours best", "ours latest", "base best", "base latest"]


def reorder_summary(rows):
    # rows built as [bank, obest, olast, bbest, blast] already
    return rows


def text_table(head, rows):
    w = [len(h) for h in head]
    for r in rows:
        for i, c in enumerate(r):
            w[i] = max(w[i], len(c))
    f = lambda r: "  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip()
    return "\n".join([f(head), "  ".join("-" * x for x in w)] + [f(r) for r in rows])


def md_table(head, rows):
    esc = lambda c: str(c).replace("|", "\\|")
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] +
                     ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows])


def color(pct):
    if pct is None:
        return "#ddd"
    return "#b7e4b0" if pct >= 90 else ("#ffe699" if pct >= 70 else "#f4b6b6")


def html_page(rows_fmt, recs, summ):
    th = lambda hs: "".join("<th>%s</th>" % html.escape(h) for h in hs)
    body = []
    for cells, r in zip(rows_fmt, recs):
        tds = []
        for i, c in enumerate(cells):
            st = ' style="background:%s"' % color(r["pct"]) if HEAD[i] == "%" else ""
            tds.append("<td%s>%s</td>" % (st, html.escape(c)))
        body.append("<tr>%s</tr>" % "".join(tds))
    sb = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % html.escape(c) for c in r) for r in summ)
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='60'><title>Scoreboard</title>"
            "<style>body{font-family:sans-serif}table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:3px 8px;font-size:13px}"
            "th{background:#eee}</style></head><body><h2>Scoreboard</h2>"
            "<p>updated %s</p><h3>Summary</h3><table><tr>%s</tr>%s</table>"
            "<h3>Runs</h3><table><tr>%s</tr>%s</table></body></html>"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), th(SUM_HEAD), sb, th(HEAD), "".join(body)))


def render(log, args):
    slots = load_slots()
    recs = [norm(d, slots) for d in load_rows(log)]
    summ = summary(recs)
    shown = recs[-LAST_N:]
    rows = [fmt_row(r) for r in shown]
    print(text_table(HEAD, rows) if rows else "(no runs in %s)" % log)
    print("\nSummary (trend vs previous same slot+bank+style)")
    print(text_table(SUM_HEAD, summ) if summ else "(empty)")
    outdir = os.path.dirname(os.path.abspath(log))
    if args.md:
        p = os.path.join(outdir, "SCOREBOARD.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# Scoreboard\n\n## Summary\n\n%s\n\n## Runs\n\n%s\n"
                    % (md_table(SUM_HEAD, summ), md_table(HEAD, rows)))
        print("wrote", p)
    if args.html:
        p = os.path.join(outdir, "scoreboard.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(html_page(rows, shown, summ))
        print("wrote", p)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", default=os.environ.get("SCOREBOARD_LOG", DEFAULT_LOG))
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args(argv)
    if not args.watch:
        render(args.log, args)
        return 0
    try:
        while True:
            print("\033[2J\033[H", end="")
            render(args.log, args)
            time.sleep(60)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
