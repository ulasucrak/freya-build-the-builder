"""Thin Claude wrapper for the builder: text in, text (or JSON) out. No tools.

    from builder import llm
    llm.on_call(lambda rec: print(rec))          # trace hook (model, purpose, start, duration, ok, ...)
    text = llm.ask("...", purpose="extract policy part 1")
    data = llm.ask_json("...", purpose="resolve calendar")

Backend: claude_agent_sdk.query when importable, else the `claude -p` CLI.
Force one with BUILDER_LLM_BACKEND=sdk|cli. Default model: env BUILDER_MODEL or "claude-opus-5-5".
Standard library only (plus the optional SDK); Python 3.9 compatible.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TIMEOUT_S = 240
MIN_REPAIR_S = 20          # skip a JSON repair call when less time than this is left

_hooks = []
_hooks_lock = threading.Lock()


class LLMError(RuntimeError):
    pass


def default_model():
    return os.environ.get("BUILDER_MODEL") or DEFAULT_MODEL


def fast_model():
    """Model for small/targeted calls. BUILDER_FAST_MODEL, else the default model."""
    return os.environ.get("BUILDER_FAST_MODEL") or default_model()


def on_call(fn):
    """Register fn(record: dict) called after every model call (success or failure).

    record keys: model, purpose, backend, start (ISO local time), start_ts, duration_s, ok, error,
    prompt_chars, response_chars.
    """
    with _hooks_lock:
        _hooks.append(fn)
    return fn


def remove_hook(fn):
    with _hooks_lock:
        if fn in _hooks:
            _hooks.remove(fn)


def _emit(record):
    with _hooks_lock:
        hooks = list(_hooks)
    for fn in hooks:
        try:
            fn(dict(record))
        except Exception:  # a broken trace hook must never break the build
            pass


def _sdk_available():
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except Exception:
        return False


def _backend():
    forced = (os.environ.get("BUILDER_LLM_BACKEND") or "").strip().lower()
    if forced in ("sdk", "cli"):
        return forced
    return "sdk" if _sdk_available() else "cli"


# ---------------------------------------------------------------- SDK backend

def _sdk_options(model, system, max_turns):
    from claude_agent_sdk import ClaudeAgentOptions
    base = dict(model=model, max_turns=max_turns, allowed_tools=[], setting_sources=[])
    if system:
        base["system_prompt"] = system
    # `tools=[]` disables every built-in tool on SDK versions that know it.
    for extra in ({"tools": []}, {}):
        try:
            return ClaudeAgentOptions(**dict(base, **extra))
        except TypeError:
            continue
    return ClaudeAgentOptions(model=model, max_turns=max_turns)


async def _sdk_collect(prompt, options):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query
    texts = []
    result_text = None
    error = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
        elif isinstance(message, ResultMessage):
            if getattr(message, "is_error", False):
                error = str(getattr(message, "result", None) or getattr(message, "subtype", "error"))
            elif getattr(message, "result", None):
                result_text = message.result
    if error and not texts:
        raise LLMError("SDK result error: " + error[:300])
    return result_text if result_text else "".join(texts)


def _ask_sdk(prompt, system, model, max_turns, timeout_s):
    options = _sdk_options(model, system, max_turns)

    async def run():
        return await asyncio.wait_for(_sdk_collect(prompt, options), timeout=timeout_s)

    # asyncio.run creates a fresh loop: safe inside worker threads.
    return asyncio.run(run())


# ---------------------------------------------------------------- CLI backend

def _ask_cli(prompt, system, model, max_turns, timeout_s):
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("neither claude_agent_sdk nor the `claude` CLI is available")
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--max-turns", str(max_turns), "--no-session-persistence", "--setting-sources", ""]
    if system:
        cmd += ["--system-prompt", system]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise LLMError("claude CLI timed out after %ss" % timeout_s)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        raise LLMError("claude CLI exit %s: %s" % (proc.returncode, (proc.stderr or "")[-300:]))
    try:
        data = json.loads(out)
    except ValueError:
        if proc.returncode != 0:
            raise LLMError("claude CLI exit %s: %s" % (proc.returncode, out[-300:]))
        return out
    if isinstance(data, dict):
        if data.get("is_error"):
            raise LLMError("claude CLI error: " + str(data.get("result") or data.get("subtype"))[:300])
        return str(data.get("result") or "")
    return out


# ---------------------------------------------------------------- public API

def ask(prompt, *, system=None, model=None, purpose, max_turns=1, timeout_s=DEFAULT_TIMEOUT_S):
    """One text-only model call. Raises LLMError on failure. Always fires the trace hooks."""
    model = model or default_model()
    backend = _backend()
    start_ts = time.time()
    rec = {"model": model, "purpose": purpose, "backend": backend,
           "start": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(start_ts)),
           "start_ts": start_ts, "prompt_chars": len(prompt) + len(system or "")}
    try:
        if backend == "sdk":
            text = _ask_sdk(prompt, system, model, max_turns, timeout_s)
        else:
            text = _ask_cli(prompt, system, model, max_turns, timeout_s)
        if not text or not text.strip():
            raise LLMError("empty response")
    except Exception as e:
        rec.update(ok=False, error="%s: %s" % (type(e).__name__, str(e)[:300]),
                   duration_s=round(time.time() - start_ts, 2), response_chars=0)
        _emit(rec)
        if isinstance(e, LLMError):
            raise
        raise LLMError("%s: %s" % (type(e).__name__, e))
    rec.update(ok=True, error=None, duration_s=round(time.time() - start_ts, 2), response_chars=len(text))
    _emit(rec)
    return text


def parse_json_text(text):
    """Pull one JSON value out of a model answer (tolerates ``` fences and prose around it)."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    candidates = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(t)
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            pass
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            i, j = c.find(open_ch), c.rfind(close_ch)
            if i != -1 and j > i:
                try:
                    return json.loads(c[i:j + 1])
                except ValueError:
                    pass
    raise ValueError("no JSON found in model answer")


def ask_json(prompt, *, system=None, model=None, purpose, max_turns=1, timeout_s=DEFAULT_TIMEOUT_S,
             retries=1, deadline=None):
    """ask() and parse JSON. On unparsable output, one repair call (counts as its own traced call).

    deadline: absolute time.time() value nothing may run past (default now + timeout_s; the earlier of the
    two wins). The repair call only runs if at least MIN_REPAIR_S remain, and is capped at the time left.
    """
    sys_json = (system + "\n\n" if system else "") + \
        "Answer with one JSON value only. No prose, no markdown fences."
    now = time.time()
    deadline = min(deadline, now + timeout_s) if deadline else now + timeout_s
    if deadline - now <= 0:
        raise LLMError("no time left for %s" % purpose)
    text = ask(prompt, system=sys_json, model=model, purpose=purpose, max_turns=max_turns,
               timeout_s=deadline - now)
    try:
        return parse_json_text(text)
    except ValueError as err:
        last_err = err
    for _ in range(retries):
        left = deadline - time.time()
        if left < MIN_REPAIR_S:
            raise LLMError("unparsable JSON (%s); repair skipped, only %.0fs left" % (last_err, left))
        repair = ("The following answer was meant to be one valid JSON value but does not parse. "
                  "Return the same content as valid JSON only.\n\n" + text)
        text = ask(repair, system=sys_json, model=model or fast_model(), purpose=purpose + " (JSON repair)",
                   max_turns=1, timeout_s=left)
        try:
            return parse_json_text(text)
        except ValueError as err:
            last_err = err
    raise LLMError("unparsable JSON from model: %s" % last_err)
