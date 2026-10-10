#!/usr/bin/env python3
"""Collect the final submission: exports, traces, code snapshot, MANIFEST.txt.

    python tools/package_submission.py brief1 brief2 [--date 2026-10-11] [--tag NAME]

For every slot name (the --slot used with build.py, or the brief's file name) it takes
  out/<slot>.workflow.json       the exported phone agent
  traces/<slot>-<ts>.txt/.json   the trace of the chosen run (latest one unless --trace SLOT=FILE is given)
plus a code snapshot (`git archive HEAD`, zip) into submission/<UTC date>/ and writes MANIFEST.txt
(commit sha, tag, sha256 of every file, build times from the traces).
Never copies env.sh, .freya-agents.json or results/ (git archive leaves untracked files out; a guard also checks names).
Standard library only, Python 3.9+.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

KIT = pathlib.Path(__file__).resolve().parent.parent
FORBIDDEN = ("env.sh", ".freya-agents.json", ".personas.json")


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args, cwd=KIT):
    r = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def safe_slot(slot):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(slot)).strip("-") or "build"


def find_traces(trace_dir, slot, explicit=None):
    """Latest trace pair (txt + json) for the slot, or the explicit one."""
    if explicit:
        stem = pathlib.Path(explicit).with_suffix("")
        return [p for p in (stem.with_suffix(".txt"), stem.with_suffix(".json")) if p.exists()]
    pat = re.compile(re.escape(safe_slot(slot)) + r"-[0-9TZ]+\.txt")
    txts = sorted(p for p in pathlib.Path(trace_dir).glob("*.txt") if pat.fullmatch(p.name))
    if not txts:
        return []
    t = txts[-1]
    return [p for p in (t, t.with_suffix(".json")) if p.exists()]


def build_info(trace_files):
    for p in trace_files:
        if p.suffix == ".json":
            try:
                d = json.loads(p.read_text())
                res = d.get("result") or {}
                return "start %s, end %s, duration %.0f s, result %s" % (d.get("start"), d.get("end"), d.get("duration_s") or 0, res.get("status"))
            except Exception:
                pass
    for p in trace_files:
        if p.suffix == ".txt":
            keep = [l for l in p.read_text().splitlines() if l.startswith(("start (UTC)", "end (UTC)", "duration"))]
            return "; ".join(keep) or "unparsed"
    return "no trace"


def package(slots, kit=KIT, date=None, out_root=None, tag=None, traces=None, allow_dirty=False):
    kit = pathlib.Path(kit)
    date = date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    dest = pathlib.Path(out_root) if out_root else kit / "submission" / date
    dest.mkdir(parents=True, exist_ok=True)
    traces = traces or {}
    files, problems, build_lines = [], [], []

    for slot in slots:
        exp = kit / "out" / (safe_slot(slot) + ".workflow.json")
        if exp.exists():
            d = dest / exp.name
            shutil.copy2(exp, d)
            files.append(d)
        else:
            problems.append("MISSING export for %s (out/%s.workflow.json)" % (slot, safe_slot(slot)))
        tf = find_traces(kit / "traces", slot, traces.get(slot))
        if not tf:
            problems.append("MISSING trace for %s" % slot)
        for p in tf:
            d = dest / "traces" / p.name
            d.parent.mkdir(exist_ok=True)
            shutil.copy2(p, d)
            files.append(d)
        build_lines.append("%s: %s" % (slot, build_info(tf)))

    rc, sha = git("rev-parse", "HEAD", cwd=kit)
    _, tag_found = git("describe", "--tags", "--exact-match", "HEAD", cwd=kit)
    _, dirty = git("status", "--porcelain", "--untracked-files=no", cwd=kit)
    if rc != 0:
        problems.append("not a git repository: no code snapshot")
    else:
        zp = dest / ("code-%s.zip" % sha[:10])
        r = subprocess.run(["git", "archive", "--format=zip", "-o", str(zp), "HEAD"], cwd=str(kit), capture_output=True, text=True)
        if r.returncode != 0:
            problems.append("git archive failed: " + r.stderr.strip()[:200])
        else:
            with zipfile.ZipFile(str(zp)) as z:
                names = z.namelist()
            bad = [n for n in names if pathlib.PurePosixPath(n).name in FORBIDDEN or "/results/" in "/" + n]
            if bad:
                zp.unlink()
                problems.append("snapshot contained forbidden files: %s" % bad[:3])
            else:
                files.append(zp)
        if dirty and not allow_dirty:
            problems.append("uncommitted tracked changes; the snapshot is HEAD, not your working files")

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = ["SUBMISSION MANIFEST", "created (UTC): " + now, "commit: %s" % (sha if rc == 0 else "n/a"),
             "tag: %s" % (tag or tag_found or "(none on HEAD)"), "slots: " + ", ".join(slots),
             "", "BUILD TIMES (from traces)"] + build_lines + ["", "FILES (sha256  size  name)"]
    for f in files:
        lines.append("%s  %d  %s" % (sha256(f), f.stat().st_size, f.relative_to(dest).as_posix()))
    if problems:
        lines += ["", "PROBLEMS"] + problems
    (dest / "MANIFEST.txt").write_text("\n".join(lines) + "\n")
    return dest, problems


def main():
    ap = argparse.ArgumentParser(description="Package the final submission.")
    ap.add_argument("slots", nargs="+", help="slot names / brief names used with build.py")
    ap.add_argument("--date", help="UTC date folder (default today)")
    ap.add_argument("--tag", help="tag to record in the manifest (default: tag on HEAD)")
    ap.add_argument("--trace", action="append", default=[], metavar="SLOT=FILE", help="use this trace instead of the latest")
    ap.add_argument("--allow-dirty", action="store_true")
    a = ap.parse_args()
    dest, problems = package(a.slots, date=a.date, tag=a.tag, traces=dict(t.split("=", 1) for t in a.trace), allow_dirty=a.allow_dirty)
    print("written to", dest)
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
