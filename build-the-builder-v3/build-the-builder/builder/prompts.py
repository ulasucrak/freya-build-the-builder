"""PROMPTS stage: the LLM refines the dialogue texts of a generated workflow from the brief text and the policy.
The generator (builder/generate.py) owns the graph, the code and the tools and already writes complete default
texts from the policy; this module only EDITS texts: the workflow `system_prompt` and the `task_message` /
`loop_prompt` of conversation steps.

The model answers with small edits, not full rewrites (the generated texts are long and carry facts blocks and
tool rules that must survive):

    {"edits": [{"step": "<step key>|system_prompt", "field": "task_message", "op": "append"|"replace"|"rewrite",
                "old": "<exact text to replace>", "new": "<text>"}]}

    prompts = write_prompts(brief_text, policy, wf, ask_json=llm.ask_json, deadline=...)   # {"edits": [...]}
    wf2 = apply_prompts(wf, prompts)       # edits that do not apply cleanly or break placeholders are skipped

Edits are re-applied to every freshly generated workflow (after a policy repair, too), so the generator's
policy-derived content always stays current. Nothing here knows a brief.
"""
import copy
import json
import re

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.:\-\[\]]+)\s*\}\}")
TEXT_FIELDS = ("task_message", "loop_prompt")
OPS = ("append", "replace", "rewrite")
MAX_BRIEF_CHARS = 60000
MAX_POLICY_CHARS = 30000
MAX_EDITS = 40

SYSTEM = (
    "You review the dialogue prompts of a phone agent that runs on the Freya platform as a graph of steps. "
    "A deterministic generator already built the graph, the code steps, the tool calls, the routing and complete "
    "default texts from a policy extracted from the bank's brief. You make targeted edits so the texts follow the "
    "brief exactly. You never invent rules, numbers, names or phrases that are not in the brief."
)

RULES = """How the platform uses these texts:
- system_prompt is part of the dialogue prompt in every step. task_message is the instruction for one step only; loop_prompt (if present) replaces it from the second turn in that step.
- Routing is decided elsewhere: never describe routing or name other steps. If a step has tools, refer to them by name.
- The current texts were generated from the policy and work together with the code steps: keep their instructions about tools, tool results, `what_to_do_next` hints, facts blocks and closing unless the brief contradicts them.
- Your job: find what is missing or wrong compared with the BRIEF (verbatim sentences the brief prescribes, rules the policy lost, wrong wording, forbidden words before identity is verified, honesty about failed tools) and fix it with small edits. Do not restate what is already there.
- Keep the texts short: every turn sends system_prompt plus one step's text, and a longer prompt makes each reply slower. Say each rule once: do not copy a rule from system_prompt into a step (or the other way), do not restate tool descriptions, and add a sentence only where the brief needs it.
- Keep every {{placeholder}} spelled exactly as it is. Use only placeholders listed as available. Never write double braces for anything else.

Edit operations (answer with a list; at most %d edits):
- {"step": "<step or system_prompt>", "op": "append", "new": "<text added at the end>"}
- {"step": "<step or system_prompt>", "op": "replace", "old": "<exact text copied from the current text, unique>", "new": "<replacement>"}
- {"step": "<step or system_prompt>", "op": "rewrite", "new": "<complete new text>"}  (rarely; only for a short text that is wrong as a whole)
Optional "field": "task_message" (default) or "loop_prompt".""" % MAX_EDITS


def _cut(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n...[cut]"


def placeholders(text):
    return set(m.group(1) for m in PLACEHOLDER.finditer(text or ""))


def conversation_nodes(wf):
    nodes = (wf or {}).get("nodes") or {}
    return {k: n for k, n in nodes.items() if isinstance(n, dict) and n.get("node_type", "conversation") == "conversation"}


def known_variables(wf, policy=None):
    """Every placeholder used anywhere in the workflow plus the policy's call variables."""
    found = placeholders(json.dumps(wf or {}))
    for v in (policy or {}).get("variables") or []:
        if isinstance(v, dict) and v.get("name"):
            found.add(str(v["name"]))
    now_var = ((policy or {}).get("clock") or {}).get("now_variable")
    if now_var:
        found.add(str(now_var))
    for v in (wf or {}).get("test_variables") or {}:
        found.add(str(v))
    return found


def _tool_names(node):
    out = []
    for t in node.get("tools") or []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else {}
            name = t.get("name") or fn.get("name")
            if name:
                out.append(str(name))
    return out


def workflow_outline(wf):
    """Compact view of the conversation steps for the prompt writer."""
    out = []
    for key, n in conversation_nodes(wf).items():
        tr = []
        for t in n.get("transitions") or []:
            if isinstance(t, dict):
                d = t.get("condition") if isinstance(t.get("condition"), dict) else {}
                desc = t.get("description") or d.get("description") or d.get("prompt") or ""
                if desc:
                    tr.append(str(desc)[:200])
        out.append({"step": key, "tools": _tool_names(n), "leaves_when": tr[:8],
                    "task_message": n.get("task_message") or "", "loop_prompt": n.get("loop_prompt") or "",
                    "placeholders": sorted(placeholders((n.get("task_message") or "") + (n.get("loop_prompt") or "")))})
    return out


def build_request(brief_text, policy, wf, feedback=None, answer=True):
    """User prompt for one prompt-editing call."""
    pol = {k: v for k, v in (policy or {}).items() if k != "tools"}
    tools = {k: {"fields": sorted(((v or {}).get("fields") or {}).keys())} for k, v in ((policy or {}).get("tools") or {}).items()
             if isinstance(v, dict)}
    parts = [RULES, "",
             "BRIEF (all files, in order; later material overrides earlier):", _cut(brief_text, MAX_BRIEF_CHARS), "",
             "POLICY extracted from the brief (JSON):", _cut(json.dumps(pol, ensure_ascii=False), MAX_POLICY_CHARS), "",
             "TOOLS of the bank API (name -> fields):", json.dumps(tools), "",
             "AVAILABLE VARIABLES for {{placeholders}}:", ", ".join(sorted(known_variables(wf, policy))), "",
             "CURRENT system_prompt:", (wf or {}).get("system_prompt") or "", "",
             "CURRENT STEPS (JSON):", json.dumps(workflow_outline(wf), ensure_ascii=False, indent=1)]
    if feedback:
        parts += ["", "FEEDBACK FROM TEST CALLS (fix the causes; keep what works):", _cut(feedback, 20000)]
    if answer:
        parts += ["", 'Answer with JSON: {"edits": [ ... ]}. An empty list is fine when nothing needs to change.']
    return "\n".join(parts)


def _norm_edit(e):
    if not isinstance(e, dict):
        return None
    step = str(e.get("step") or e.get("target") or "").strip()
    op = str(e.get("op") or ("replace" if e.get("old") else "append")).strip().lower()
    field = str(e.get("field") or "task_message").strip()
    new = e.get("new") if e.get("new") is not None else e.get("text")
    if not step or op not in OPS or not isinstance(new, str):
        return None
    if step != "system_prompt" and field not in TEXT_FIELDS:
        return None
    out = {"step": step, "op": op, "new": new}
    if step != "system_prompt":
        out["field"] = field
    if op == "replace":
        if not isinstance(e.get("old"), str) or not e["old"]:
            return None
        out["old"] = e["old"]
    return out


def _get_text(wf, edit):
    if edit["step"] == "system_prompt":
        return wf.get("system_prompt") or ""
    return (wf["nodes"][edit["step"]].get(edit.get("field", "task_message")) or "")


def _set_text(wf, edit, text):
    if edit["step"] == "system_prompt":
        wf["system_prompt"] = text
    else:
        wf["nodes"][edit["step"]][edit.get("field", "task_message")] = text


def apply_edit(wf, edit, known):
    """Apply one edit in place. Returns None when applied, else the reason it was skipped."""
    if edit["step"] != "system_prompt" and edit["step"] not in conversation_nodes(wf):
        return "%s: not a conversation step" % edit["step"]
    old_text = _get_text(wf, edit)
    if edit["op"] == "append":
        if not edit["new"].strip():
            return "%s: empty append" % edit["step"]
        text = (old_text.rstrip() + "\n\n" + edit["new"].strip()) if old_text.strip() else edit["new"].strip()
    elif edit["op"] == "replace":
        n = old_text.count(edit["old"])
        if n != 1:
            return "%s: text to replace found %d times" % (edit["step"], n)
        text = old_text.replace(edit["old"], edit["new"])
    else:
        if not edit["new"].strip():
            return "%s: empty rewrite" % edit["step"]
        text = edit["new"].strip()
    missing = placeholders(old_text) - placeholders(text)
    if missing:
        return "%s: would lose placeholders %s" % (edit["step"], sorted(missing))
    unknown = placeholders(text) - known
    if unknown:
        return "%s: unknown placeholders %s" % (edit["step"], sorted(unknown))
    rest = PLACEHOLDER.sub("", text)
    if "{{" in rest or "}}" in rest:
        return "%s: stray double braces" % edit["step"]
    _set_text(wf, edit, text)
    return None


def clean_prompts(raw, wf, policy=None):
    """Keep only edits that apply cleanly to wf, in order. Returns ({"edits": [...]}, dropped_reasons)."""
    dropped = []
    if isinstance(raw, list):
        raw = {"edits": raw}
    if not isinstance(raw, dict):
        return {"edits": []}, ["answer is not an object"]
    edits = raw.get("edits")
    if not isinstance(edits, list):
        edits = []
    known = known_variables(wf, policy)
    w = copy.deepcopy(wf)
    kept = []
    for e in edits[:MAX_EDITS]:
        ne = _norm_edit(e)
        if ne is None:
            dropped.append("malformed edit: %s" % json.dumps(e, default=str)[:120])
            continue
        why = apply_edit(w, ne, known)
        if why:
            dropped.append(why)
        else:
            kept.append(ne)
    return {"edits": kept}, dropped


def apply_prompts(wf, prompts, policy=None):
    """Copy of wf with the prompt edits applied in order (edits that no longer apply are skipped)."""
    w = copy.deepcopy(wf)
    if not isinstance(prompts, dict):
        return w
    known = known_variables(wf, policy)
    for e in prompts.get("edits") or []:
        ne = _norm_edit(e)
        if ne is not None:
            apply_edit(w, ne, known)
    return w


def merge_prompts(base, patch):
    """Edits of base, then edits of patch."""
    return {"edits": list((base or {}).get("edits") or []) + list((patch or {}).get("edits") or [])}


def score_prompts(prompts, wf):
    """Static quality score of a candidate (higher is better): number of clean edits, spread over steps."""
    edits = (prompts or {}).get("edits") or []
    return len(edits) + 0.5 * len(set(e["step"] for e in edits))


def write_prompts(brief_text, policy, wf, ask_json, deadline=None, feedback=None, purpose="write node prompts",
                  model=None, timeout_s=240):
    """One LLM call -> cleaned prompts dict {"edits": [...], "dropped": [...]}. Raises whatever ask_json raises."""
    kw = {"system": SYSTEM, "purpose": purpose, "timeout_s": timeout_s}
    if deadline:
        kw["deadline"] = deadline
    if model:
        kw["model"] = model
    raw = ask_json(build_request(brief_text, policy, wf, feedback), **kw)
    prompts, dropped = clean_prompts(raw, wf, policy)
    prompts["dropped"] = dropped
    return prompts
