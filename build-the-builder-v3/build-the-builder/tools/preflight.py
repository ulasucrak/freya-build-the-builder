#!/usr/bin/env python3
"""Final-day preflight. Prints PASS / WARN / FAIL lines; exit code 1 if any FAIL. Never prints secret values.

    source /abs/path/env.sh && python tools/preflight.py [--skip-claude] [--min-disk-gb 2]

Standard library only, Python 3.9+. Read-only: lists workflows, GETs the bank mock, runs `git status`.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
RESULTS = []


def say(level, name, detail=""):
    RESULTS.append(level)
    print("%-4s %s%s" % (level, name, (" - " + detail) if detail else ""))


def run(cmd, timeout=30, cwd=KIT):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def check_python():
    v = sys.version_info
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    say("PASS" if v >= (3, 9) else "FAIL", "python version", "%d.%d.%d" % v[:3])
    say("PASS" if in_venv else "WARN", "virtualenv active", "yes" if in_venv else "no (builder needs the venv python with claude_agent_sdk)")


def check_sdk():
    try:
        import claude_agent_sdk  # noqa: F401
        say("PASS", "claude_agent_sdk import", getattr(claude_agent_sdk, "__version__", ""))
    except Exception as e:
        say("FAIL", "claude_agent_sdk import", type(e).__name__)


def check_auth(skip):
    if os.environ.get("ANTHROPIC_API_KEY"):
        say("PASS", "Claude auth", "ANTHROPIC_API_KEY is set")
        return
    if skip:
        say("WARN", "Claude auth", "skipped (--skip-claude)")
        return
    try:
        r = run(["claude", "-p", "say ok"], timeout=60)
        ok = r.returncode == 0 and r.stdout.strip() != ""
        say("PASS" if ok else "FAIL", "Claude auth", "claude -p works (subscription; consider ANTHROPIC_API_KEY)" if ok else "claude -p failed, exit %d" % r.returncode)
    except FileNotFoundError:
        say("FAIL", "Claude auth", "no ANTHROPIC_API_KEY and `claude` CLI not found")
    except subprocess.TimeoutExpired:
        say("FAIL", "Claude auth", "claude -p timed out after 60 s")


def check_env():
    for k in ("FREYA_API_KEY", "FREYA_API_URL", "FREYA_BANK_TEAM_KEY"):
        say("PASS" if os.environ.get(k) else "FAIL", "env " + k, "set" if os.environ.get(k) else "NOT set (source env.sh)")


def check_freya():
    if not (os.environ.get("FREYA_API_KEY") and os.environ.get("FREYA_API_URL")):
        say("FAIL", "Freya API", "skipped, env missing")
        return
    try:
        import freya
        items = freya.api("GET", "/api/v2/workflow", workspaceId=freya.workspace_id())
        if isinstance(items, dict):
            items = items.get("workflows") or items.get("items") or []
        n = len(items or [])
        say("PASS", "Freya API reachable", "%d workflows listed" % n)
        lvl = "PASS" if n < 170 else ("WARN" if n < 200 else "FAIL")
        say(lvl, "workspace usage", "%d / 200 workflows" % n)
    except Exception as e:
        say("FAIL", "Freya API", str(e)[:160].replace(os.environ.get("FREYA_API_KEY", "\0"), "***"))


def check_bank():
    import freya  # noqa: F401  (only for path consistency)
    host = os.environ.get("FREYA_BANK_HOST", "https://freya-bank-mock.freyavoice.workers.dev")
    req = urllib.request.Request(host + "/", headers={"User-Agent": "freya-bank-scorer/0.1"})
    try:
        code = urllib.request.urlopen(req, timeout=20).status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        say("FAIL", "bank mock reachable", type(e).__name__)
        return
    say("PASS" if code < 500 else "FAIL", "bank mock reachable", "HTTP %s" % code)


def check_git():
    try:
        st = run(["git", "status", "--porcelain"]).stdout.strip()
        sha = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
        tag = run(["git", "describe", "--tags", "--always", "--dirty"]).stdout.strip()
        say("PASS" if not st else "FAIL", "git working tree clean", "" if not st else "%d changed files" % len(st.splitlines()))
        say("PASS", "git commit/tag", "%s / %s" % (sha, tag))
    except Exception as e:
        say("FAIL", "git", type(e).__name__)


def check_disk(min_gb):
    free = shutil.disk_usage(KIT).free / 1e9
    say("PASS" if free >= min_gb else "FAIL", "free disk", "%.1f GB" % free)


def check_llm():
    try:
        import freya
        cur = json.dumps(freya.AGENT_LLM, sort_keys=True)
        top = run(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
        rel = os.path.relpath(os.path.join(KIT, "freya.py"), top)
        src = run(["git", "show", "HEAD:" + rel], cwd=top).stdout
        ns = {}
        env_keep = {k: os.environ.pop(k) for k in ("FREYA_AGENT_PROVIDER", "FREYA_AGENT_MODEL") if k in os.environ}
        try:
            for line in src.splitlines():
                if line.startswith("AGENT_LLM"):
                    exec("import os\n" + line, ns)
        finally:
            os.environ.update(env_keep)
        head = json.dumps(ns.get("AGENT_LLM"), sort_keys=True)
        same = cur == head
        say("PASS" if same else "FAIL", "freya.AGENT_LLM unchanged vs HEAD", "" if same else "differs (env override or edited file)")
    except Exception as e:
        say("FAIL", "freya.AGENT_LLM check", type(e).__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skip-claude", action="store_true", help="do not run `claude -p` when no API key is set")
    ap.add_argument("--min-disk-gb", type=float, default=2.0)
    a = ap.parse_args()
    check_python(); check_sdk(); check_auth(a.skip_claude); check_env(); check_freya(); check_bank()
    check_git(); check_disk(a.min_disk_gb); check_llm()
    bad = RESULTS.count("FAIL")
    print("\n%d PASS, %d WARN, %d FAIL" % (RESULTS.count("PASS"), RESULTS.count("WARN"), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
