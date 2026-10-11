"""Compare two run_cases --out files on the cases they share: pass rate, slowest turn per call, tool calls per call.
    python tools/compare_runs.py <old run.json> <new run.json> [more new run.json ...]"""
import json
import statistics as st
import sys


def rows_of(path):
    d = json.load(open(path))
    out = {}
    for r in d.get("rows") or []:
        out[r["id"]] = r
    return out


def summary(rows):
    slow = [r.get("slowest_turn_s") for r in rows if r.get("slowest_turn_s") is not None]
    tools = [len(r.get("tools") or []) for r in rows]
    return (sum(1 for r in rows if r.get("pass")), len(rows),
            st.median(slow) if slow else float("nan"), max(slow) if slow else float("nan"),
            st.mean(tools) if tools else float("nan"))


def main():
    old = rows_of(sys.argv[1])
    new = {}
    for p in sys.argv[2:]:
        new.update(rows_of(p))
    ids = sorted(set(old) & set(new))
    for label, rows in (("old", [old[i] for i in ids]), ("new", [new[i] for i in ids])):
        p, n, med, worst, tc = summary(rows)
        print(f"{label}: {p}/{n} passed | slowest turn per call: median {med:.1f}s, worst {worst:.1f}s | tool calls per call {tc:.1f}")
    fixed = [i for i in ids if new[i].get("pass") and not old[i].get("pass")]
    broke = [i for i in ids if old[i].get("pass") and not new[i].get("pass")]
    print("fixed:", ", ".join(fixed) or "-")
    print("newly failing:", ", ".join(broke) or "-")
    only_new = sorted(set(new) - set(old))
    if only_new:
        p, n, med, worst, tc = summary([new[i] for i in only_new])
        print(f"only in new ({n} cases): {p}/{n} passed, median slowest turn {med:.1f}s; failing:",
              ", ".join(i for i in only_new if not new[i].get("pass")) or "-")


if __name__ == "__main__":
    main()
