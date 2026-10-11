"""The end-to-end builder run behind build.py.

    extract (builder.extract) -> validate (builder.policy_schema) -> generate (builder.generate)
    -> prompts (LLM rewrites node texts from the brief) -> freya dry-run validation (fallbacks)
    -> save in place + export (a good version always exists from here on)
    -> self-test (synthetic cases, oracle-computed expectations, test mode) -> repair rounds -> final save + export

Hard wall clock: every stage gets a sub-budget and nothing starts that cannot finish before the end; when time runs
low the best version so far is what stays saved and exported. Every model call, every Freya change and every test
run goes to the trace (builder/trace.py).

All external calls go through `Deps`, so tests replace them with fakes. Nothing here knows a brief.
"""
import copy
import json
import os
import pathlib
import random
import re
import sys
import time

from builder import prompts as prompts_mod
from builder.trace import Budget, CallTimeout, Trace, one_line, run_with_timeout

KIT = pathlib.Path(os.path.abspath(__file__)).parent.parent

# Stage plans in seconds (each capped by what is left after the reserve).
PLAN = {"extract": 330, "policy_fix": 120, "prompts": 240, "casegen": 150, "repair_llm": 200}
RESERVE_SAVE_S = 90          # generate + dry run + save + export of the version we ship
RESERVE_END_S = 45           # trace writing, slack before the hard end
MIN_LLM_STAGE_S = 45         # do not start an optional model call with less than this
REPAIR_MIN_S = 90            # a repair call needs at least this long
REGEN_S = 45                 # generate + dry-run validation of a repaired version
FINAL_MARGIN_S = 15          # every blocking Freya call ends at least this long before the hard end
CAP_DRY_RUN_S = 60           # per-call caps (each also capped by the time left)
CAP_SAVE_S = 240             # save_workflow = create/update + read back + publish (4-6 requests)
CAP_EXPORT_S = 90
MIN_GAIN_PARTIAL = 2         # a repaired version replaces the best one on a partial retest only with this many more passes
RETRY_EXTRACT_S = 150        # second, single-replicate extraction after a failure
POLICY_KEYS = ["source", "variables", "clock", "calendar", "opening", "third_party", "identity", "products",
               "bands", "amounts", "dates", "promise", "callback", "triggers", "notes", "outcomes", "phrases",
               "tools", "rules"]


class Deps:
    """External services. Resolved lazily; tests set attributes to fakes."""

    def __init__(self, **kw):
        self._given = kw

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._given:
            return self._given[name]
        val = self._load(name)
        self._given[name] = val
        return val

    @staticmethod
    def _load(name):
        if str(KIT) not in sys.path:
            sys.path.insert(0, str(KIT))
        if name == "llm":
            from builder import llm
            return llm
        if name == "extract":
            from builder import extract
            return extract
        if name == "schema":
            from builder import policy_schema
            return policy_schema
        if name == "generate":
            from builder import generate
            return generate
        if name == "freya":
            import freya
            return freya
        if name == "casegen":
            from builder import casegen
            return casegen
        if name == "selftester":
            from builder.selftest import SelfTester
            return SelfTester
        raise AttributeError(name)


def slot_from_paths(paths):
    """Default slot: the first brief file's name without extension (folder name for a folder)."""
    p = pathlib.Path(paths[0]) if paths else pathlib.Path("build")
    name = p.name if p.is_dir() else p.stem
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "build"


def expand_briefs(paths):
    """Files as given; a folder contributes its files in name order (hidden files skipped)."""
    out = []
    for p in paths:
        p = pathlib.Path(p)
        if p.is_dir():
            out += sorted(str(x) for x in p.iterdir() if x.is_file() and not x.name.startswith("."))
        else:
            out.append(str(p))
    return out


QUIET = False


def log(msg):
    if QUIET:
        return
    sys.stderr.write("[build %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


class Builder:
    def __init__(self, briefs, openapi, slot=None, budget_min=28.0, no_test=False, best_of=1, out_dir=None,
                 trace_dir=None, state_file=None, max_cases=None, repair_rounds=2, no_prompts=False,
                 deps=None, clock=None, now=None, seed=None):
        self.brief_args = list(briefs)
        self.openapi_path = str(openapi)
        self.slot = slot or slot_from_paths(self.brief_args)
        self.no_test = no_test
        self.best_of = max(1, int(best_of or 1))
        self.out_dir = pathlib.Path(out_dir) if out_dir else KIT / "out"
        self.trace_dir = pathlib.Path(trace_dir) if trace_dir else KIT / "traces"
        self.state_file = str(state_file or (KIT / ".freya-agents.json"))
        self.max_cases = max_cases
        self.repair_rounds = repair_rounds
        self.no_prompts = no_prompts
        self.deps = deps or Deps()
        self.budget = Budget(budget_min * 60.0, clock=clock)
        self.trace = Trace(self.slot, self.trace_dir, now=now, clock=clock)
        self.seed = seed if seed is not None else random.randint(1, 10 ** 6)
        self.policy = None
        self.openapi = None
        self.brief_text = ""
        self.best = None          # {"wf", "policy", "prompts", "passed", "total", "label"}
        self.saved = None         # Freya info of the real slot
        self.export_path = None
        self.cases = []
        self.results = {}         # case id -> last row of the best version
        self.stage_times = {}

    # ------------------------------------------------------------------ helpers
    def deadline(self, seconds):
        return time.time() + max(0.0, seconds)

    def stage_start(self, name):
        log("stage %s (%.0f s left)" % (name, self.budget.remaining()))
        return self.budget.elapsed()

    def stage_end(self, name, t0, status="ok", detail=""):
        d = self.budget.elapsed() - t0
        self.stage_times[name] = round(self.stage_times.get(name, 0) + d, 1)
        self.trace.stage(name, status, d, detail)
        log("stage %s: %s (%.1f s) %s" % (name, status, d, one_line(detail, 200)))

    def write_out(self, name, data):
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            p = self.out_dir / ("%s.%s" % (self.slot, name))
            p.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
            return str(p)
        except Exception:
            return None

    # ------------------------------------------------------------------ stages
    def read_briefs(self):
        files = expand_briefs(self.brief_args)
        try:
            docs = self.deps.extract.read_documents(self.brief_args)
            self.brief_text = self.deps.extract.documents_block(docs)
        except Exception:
            parts = []
            for f in files:
                try:
                    parts.append("=== %s ===\n%s" % (pathlib.Path(f).name, open(f, errors="replace").read()))
                except Exception:
                    pass
            self.brief_text = "\n\n".join(parts)
        return files

    def do_extract(self, files):
        """Extract the policy. On failure: one single-replicate retry when time allows, then a minimal policy from
        the tool API plus the raw brief text as rules, so a phone agent is always built."""
        t0 = self.stage_start("extract")
        sub = self.budget.sub(PLAN["extract"], RESERVE_SAVE_S + RESERVE_END_S)
        warnings = []
        policy, how = None, "ok"
        try:
            policy = self.deps.extract.extract_policy(self.brief_args, self.openapi_path, budget_s=max(60.0, sub),
                                                      warnings=warnings)
        except Exception as e:  # noqa: BLE001
            self.trace.note("extraction failed: %s: %s" % (type(e).__name__, one_line(e, 300)))
            retry_s = self.budget.sub(RETRY_EXTRACT_S, RESERVE_SAVE_S + RESERVE_END_S, MIN_LLM_STAGE_S)
            if retry_s:
                try:
                    try:
                        policy = self.deps.extract.extract_policy(self.brief_args, self.openapi_path, replicates=1,
                                                                  budget_s=retry_s, warnings=warnings)
                    except TypeError:
                        policy = self.deps.extract.extract_policy(self.brief_args, self.openapi_path, budget_s=retry_s,
                                                                  warnings=warnings)
                    how = "ok (single-replicate retry)"
                except Exception as e2:  # noqa: BLE001
                    self.trace.note("extraction retry failed: %s: %s" % (type(e2).__name__, one_line(e2, 300)))
            if policy is None:
                policy = self.fallback_policy()
                how = "fallback"
        if not isinstance(policy, dict):
            policy, how = self.fallback_policy(), "fallback"
        self.policy = policy
        self.write_out("policy.json", policy)
        self.stage_end("extract", t0, how, "%d warnings%s" % (len(warnings), (": " + "; ".join(warnings[:3])) if warnings else ""))
        return policy

    def fallback_policy(self):
        """Minimal contract-v1 policy: the supplied tool API, every other section empty, the brief verbatim as rules
        (numbered rules when the extractor can find them, else paragraphs)."""
        tools = {}
        try:
            tools = self.deps.extract.parse_openapi(self.openapi_path).get("tools") or {}
        except Exception as e:  # noqa: BLE001
            self.trace.note("fallback policy: openapi not parsed (%s)" % one_line(e, 200))
        rules = []
        try:
            docs = self.deps.extract.read_documents(self.brief_args)
            rules = list(self.deps.extract.numbered_rules(docs) or [])
        except Exception:
            rules = []
        if not rules:
            paras = [x.strip() for x in re.split(r"\n\s*\n", self.brief_text or "") if x.strip()]
            rules = [{"id": "p%d" % (i + 1), "text": t} for i, t in enumerate(paras)]
        policy = {k: ([] if k in ("variables", "bands", "triggers", "rules") else {}) for k in POLICY_KEYS}
        policy["source"] = {"briefs": [pathlib.Path(f).name for f in expand_briefs(self.brief_args)],
                            "overrides_applied": [], "notes": ["fallback policy: extraction failed"]}
        policy["tools"] = tools
        policy["rules"] = rules
        self.trace.note("using fallback policy (tool API + %d verbatim brief rules)" % len(rules))
        return policy

    def minimal_workflow(self):
        """Last resort when the generator fails on every policy: one conversation step that carries the brief.
        Valid and submittable, far from good."""
        text = re.sub(r"\{\{|\}\}", lambda m: m.group(0)[0] + " " + m.group(0)[1], self.brief_text or "")
        return {"initial_node": "conversation", "system_prompt": "Follow the bank's brief below exactly.\n\n" + text[:60000],
                "nodes": {"conversation": {"name": "conversation", "node_type": "conversation",
                                           "task_message": "Handle the call as the brief requires.", "transitions": []}}}

    def validate_policy(self, policy):
        try:
            return list(self.deps.schema.validate(policy) or [])
        except Exception as e:
            return ["validate failed: %s" % e]

    def do_validate(self):
        t0 = self.stage_start("validate")
        errors = self.validate_policy(self.policy)
        if errors and self.budget.sub(PLAN["policy_fix"], RESERVE_SAVE_S + RESERVE_END_S, MIN_LLM_STAGE_S):
            sub = self.budget.sub(PLAN["policy_fix"], RESERVE_SAVE_S + RESERVE_END_S)
            try:
                fixed = self.fix_policy(self.policy, errors, sub)
                e2 = self.validate_policy(fixed)
                if len(e2) < len(errors):
                    self.policy, errors = fixed, e2
                    self.write_out("policy.json", self.policy)
            except Exception as e:
                self.trace.note("policy fix failed: %s" % e)
        self.stage_end("validate", t0, "ok" if not errors else "errors", "; ".join(errors[:5]))
        return errors

    def fix_policy(self, policy, errors, seconds):
        prompt = "\n".join([
            "A policy JSON extracted from a bank brief fails validation. Fix only what the errors name, using the brief.",
            "Return JSON {<top-level key>: <corrected full value of that section>} for the sections you change.",
            "", "ERRORS:", "\n".join("- " + e for e in errors[:30]), "",
            "POLICY:", json.dumps({k: v for k, v in policy.items() if k != "tools"}, ensure_ascii=False)[:40000], "",
            "BRIEF:", self.brief_text[:50000]])
        patch = self.deps.llm.ask_json(prompt, purpose="fix policy validation errors", timeout_s=seconds,
                                       deadline=self.deadline(seconds))
        return apply_policy_patch(policy, patch)

    def generate(self, policy, prompts=None):
        gen = self.deps.generate.generate_workflow
        try:
            wf = gen(policy, None, self.openapi)
        except TypeError:
            wf = gen(policy)
        if prompts:
            wf = prompts_mod.apply_prompts(wf, prompts, policy)
        return wf

    def generate_safe(self):
        """(workflow, how): the policy's workflow, else the fallback policy's, else the minimal workflow."""
        try:
            return self.generate(self.policy), "ok"
        except Exception as e:  # noqa: BLE001
            self.trace.note("generate failed: %s: %s" % (type(e).__name__, one_line(e, 300)))
        try:
            fb = self.fallback_policy()
            wf = self.generate(fb)
            self.policy = fb
            return wf, "fallback policy"
        except Exception as e:  # noqa: BLE001
            self.trace.note("generate on fallback policy failed: %s: %s" % (type(e).__name__, one_line(e, 300)))
        return self.minimal_workflow(), "minimal"

    def do_prompts(self, wf0):
        if self.no_prompts:
            return {}
        t0 = self.stage_start("prompts")
        sub = self.budget.sub(PLAN["prompts"], RESERVE_SAVE_S + RESERVE_END_S, MIN_LLM_STAGE_S)
        if not sub:
            self.stage_end("prompts", t0, "skipped", "not enough time")
            return {}
        cands = []
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=self.best_of) as pool:
            futs = [pool.submit(prompts_mod.write_prompts, self.brief_text, self.policy, wf0, self.deps.llm.ask_json,
                                self.deadline(sub), None,
                                "write node prompts" + ("" if self.best_of == 1 else " (candidate %d)" % (i + 1)),
                                None, sub)
                    for i in range(self.best_of)]
            for f in futs:
                try:
                    cands.append(f.result())
                except Exception as e:
                    self.trace.note("prompt candidate failed: %s" % one_line(e, 200))
        if not cands:
            self.stage_end("prompts", t0, "failed", "generated default prompts kept")
            return {}
        best = max(cands, key=lambda c: prompts_mod.score_prompts(c, wf0))
        dropped = best.pop("dropped", [])
        self.write_out("prompts.json", best)
        self.stage_end("prompts", t0, "ok", "%d edits kept (%s); %d dropped" % (
            len(best.get("edits") or []), ", ".join(sorted(set(e["step"] for e in best.get("edits") or []))), len(dropped)))
        return best

    def call_freya(self, cap_s, fn, *args, **kwargs):
        """A blocking Freya call with a hard deadline: its cap, and never past the end of the budget."""
        t = max(1.0, min(cap_s, self.budget.remaining() - FINAL_MARGIN_S))
        return run_with_timeout(fn, t, *args, **kwargs)

    def dry_run(self, wf):
        """None when the platform accepts the workflow, else the error text."""
        try:
            self.call_freya(CAP_DRY_RUN_S, self.deps.freya.validate_workflow, "%s dry-run" % self.slot, wf)
            return None
        except CallTimeout as e:
            return "timeout: %s" % e
        except Exception as e:
            return one_line(e, 600)

    def pick_valid(self, candidates):
        """First candidate (label, wf) the platform accepts; else the last one with its error."""
        err = None
        for label, wf in candidates:
            if wf is None:
                continue
            err = self.dry_run(wf)
            self.trace.note("dry-run validation of %s: %s" % (label, "ok" if not err else "rejected: " + err))
            if not err:
                return label, wf, None
        last = [c for c in candidates if c[1] is not None][-1]
        if err and err.startswith("timeout:"):
            return last[0], last[1], None            # the platform did not answer: no verdict, ship what we have
        return last[0], last[1], err

    def save_and_export(self, wf, label):
        t0 = self.stage_start("save")
        try:
            info = self.call_freya(CAP_SAVE_S, self.deps.freya.save_workflow, self.slot, wf, state_file=self.state_file)
        except Exception as e:
            self.stage_end("save", t0, "failed", one_line(e, 300))
            raise
        self.saved = info
        self.export_path = None
        self.trace.freya("saved phone agent (%s)" % label, slot=self.slot, workflow_id=info.get("workflow_id"),
                         agent_id=info.get("agent_id"), version=info.get("version"))
        self.export()
        self.stage_end("save", t0, "ok", "%s -> %s" % (label, info.get("workflow_id")))
        return info

    def export_target(self):
        return str(self.out_dir / ("%s.workflow.json" % self.slot))

    def export(self):
        """Export the saved workflow (bounded). True on success."""
        if not self.saved:
            return False
        path = self.export_target()
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self.call_freya(CAP_EXPORT_S, self.deps.freya.export_workflow, self.saved["workflow_id"], path)
            self.export_path = path
            self.trace.freya("exported", workflow_id=self.saved.get("workflow_id"), path=path)
            return True
        except Exception as e:
            self.trace.note("export failed: %s" % one_line(e, 300))
            return False

    # ------------------------------------------------------------------ self-test
    def make_cases(self):
        t0 = self.stage_start("casegen")
        sub = self.budget.sub(PLAN["casegen"], RESERVE_END_S)
        n_max = self.max_cases or 10
        ask = self.deps.llm.ask_json if sub >= MIN_LLM_STAGE_S else None
        try:
            model = self.deps.llm.fast_model()
        except Exception:
            model = None
        cases, warns = self.deps.casegen.generate_cases(self.policy, ask_json=ask, n_max=n_max,
                                                        deadline=self.deadline(sub) if ask else None,
                                                        seed=self.seed, model=model, timeout_s=max(sub, 1))
        self.cases = cases
        self.write_out("selftest-cases.json", cases)
        self.stage_end("casegen", t0, "ok", "%d cases%s" % (len(cases), ("; " + "; ".join(warns[:3])) if warns else ""))
        return cases

    def run_test(self, tester, wf, cases, label):
        """One simulation within the budget. Returns the result dict, or None when it cannot fit."""
        est = _estimate(len(cases))
        if not self.budget.can_start(est, RESERVE_SAVE_S + RESERVE_END_S):
            self.trace.test_run(label, cases=[c["id"] for c in cases], detail="skipped: estimated %.0f s, %.0f s left" % (est, self.budget.remaining()))
            return None
        t0 = self.stage_start(label)
        timeout = self.budget.remaining() - RESERVE_SAVE_S - RESERVE_END_S
        try:
            res = tester.run(wf, cases, label=label, timeout_s=timeout, min_run_s=est)
        except TypeError:
            res = tester.run(wf, cases, label=label, timeout_s=timeout)
        if res.get("ok"):
            failed = [r["id"] for r in res["rows"] if not r.get("pass")]
            self.trace.test_run(label, cases=[c["id"] for c in cases], passed=res["passed"], total=res["total"],
                                duration_s=res.get("duration_s"), detail=("failed: " + ", ".join(failed)) if failed else "all passed",
                                simulation_id=res.get("simulation_id"), mode="test mode (fake bank in Freya)")
        else:
            self.trace.test_run(label, cases=[c["id"] for c in cases], duration_s=res.get("duration_s"),
                                detail="error: " + str(res.get("error")))
        self.stage_end(label, t0, "ok" if res.get("ok") else "error", res.get("error") or "%s/%s" % (res.get("passed"), res.get("total")))
        return res

    def fit_cases(self, cases):
        """Subset of the cases whose simulation fits the time left, picked round-robin by family so every kind of
        situation is covered before any family gets a second case. Original order is kept."""
        avail = self.budget.remaining() - RESERVE_SAVE_S - RESERVE_END_S
        return round_robin(cases, max_cases_within(avail))

    def repair_feedback(self, rows):
        by_id = {c["id"]: c for c in self.cases}
        parts = []
        for r in rows:
            if r.get("pass"):
                continue
            c = by_id.get(r["id"], {})
            parts.append("\n".join([
                "CASE %s (%s): %s" % (r["id"], c.get("family"), c.get("title")),
                "customer: " + str(c.get("script") or c.get("plan")),
                "call variables: " + json.dumps(c.get("variables")),
                "products: " + json.dumps(c.get("products")),
                "expected bank records: " + json.dumps(c.get("expected")),
                "what failed: " + " | ".join(str(x) for x in r.get("failed") or []),
                "tool calls seen: " + " > ".join(r.get("tools") or []),
                "transcript tail:\n" + str(r.get("transcript") or "(none)")]))
        return "\n\n".join(parts)

    def repair(self, rows, cur_policy, cur_prompts, wf_cur, seconds, round_no):
        feedback = self.repair_feedback(rows)
        req = "\n".join([
            prompts_mod.build_request(self.brief_text, cur_policy, wf_cur, feedback, answer=False), "",
            "You may ALSO correct the policy when a failure shows it misreads the brief (a wrong deadline, holiday, "
            "band, outcome, reason, queue, identity rule ...). Expected records in the feedback come from the policy "
            "itself, so only change the policy where the BRIEF clearly says otherwise.",
            'Answer JSON: {"diagnosis": "one line per failing case", "edits": [ ...prompt edits... ], '
            '"policy_patch": {<top-level policy key>: <full corrected value>}}  (policy_patch may be {})'])
        raw = self.deps.llm.ask_json(req, system=prompts_mod.SYSTEM, purpose="repair round %d from %d failing self-test cases"
                                     % (round_no, sum(1 for r in rows if not r.get("pass"))),
                                     timeout_s=seconds, deadline=self.deadline(seconds))
        raw = raw if isinstance(raw, dict) else {}
        new_prompts, dropped = prompts_mod.clean_prompts({"edits": raw.get("edits") or (raw.get("prompts") or {}).get("edits") or []},
                                                          wf_cur, cur_policy)
        merged = prompts_mod.merge_prompts(cur_prompts, new_prompts)
        patch = raw.get("policy_patch") if isinstance(raw.get("policy_patch"), dict) else {}
        new_policy = cur_policy
        if patch:
            cand = apply_policy_patch(cur_policy, patch)
            if len(self.validate_policy(cand)) <= len(self.validate_policy(cur_policy)):
                new_policy = cand
            else:
                self.trace.note("repair round %d: policy patch rejected (more validation errors)" % round_no)
        self.trace.note("repair round %d: %d prompt edits (%d dropped), policy sections changed: %s; diagnosis: %s" % (
            round_no, len(new_prompts.get("edits") or []), len(dropped),
            ", ".join(sorted(k for k in patch if new_policy is not cur_policy)) or "none",
            one_line(raw.get("diagnosis"), 300)))
        return new_policy, merged

    # ------------------------------------------------------------------ main
    def run(self):
        status, err = "failed", ""
        llm_hook = None
        try:
            try:
                llm_hook = self.deps.llm.on_call(self.trace.model_call)
            except Exception:
                llm_hook = None
            self.trace.set_info(briefs=", ".join(self.brief_args), openapi=self.openapi_path,
                                budget_s=int(self.budget.total_s), slot=self.slot, state_file=self.state_file,
                                agent_model_rule="phone agent runs on freya.AGENT_LLM (unchanged)")
            with open(self.openapi_path) as fh:
                self.openapi = json.load(fh)
            files = self.read_briefs()
            self.do_extract(files)
            self.do_validate()

            t0 = self.stage_start("generate")
            wf0, how = self.generate_safe()
            self.stage_end("generate", t0, how, "%d nodes" % len(wf0.get("nodes") or {}))
            prompts = self.do_prompts(wf0) if how != "minimal" else {}
            wf1 = None
            if prompts:
                try:
                    wf1 = self.generate(self.policy, prompts)
                except Exception as e:  # noqa: BLE001
                    self.trace.note("generate with prompts failed: %s" % one_line(e, 200))

            t0 = self.stage_start("dry_run")
            label, wf, verr = self.pick_valid([("generated + LLM prompts", wf1), ("generated defaults", wf0)])
            if wf is wf0:
                prompts = {}
            self.stage_end("dry_run", t0, "ok" if not verr else "rejected", verr or label)
            self.write_out("workflow.local.json", wf)
            self.save_and_export(wf, label)
            self.best = {"wf": wf, "policy": self.policy, "prompts": prompts, "passed": None, "total": None, "label": "v1"}

            if self.no_test:
                self.trace.note("self-test skipped (--no-test)")
            else:
                self.self_test_and_repair()
            status = "ok"
        except Exception as e:  # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, one_line(e, 400))
            log("FAILED: " + err)
            self.trace.note("error: " + err)
            status = "ok (with errors after save)" if self.saved else "failed"
        finally:
            if self.saved and not (self.export_path and os.path.exists(self.export_path)):
                self.trace.note("export missing at the end: retrying")
                self.export()
            b = self.best or {}
            self.trace.finish(status, workflow_id=(self.saved or {}).get("workflow_id"),
                              agent_id=(self.saved or {}).get("agent_id"), version=(self.saved or {}).get("version"),
                              export=self.export_path, best_version=b.get("label"),
                              selftest_score=("%s/%s%s" % (b["passed"], b["total"], " (estimated)" if b.get("estimated") else ""))
                              if b.get("total") else None,
                              build_seconds=round(self.budget.elapsed()), error=err or None,
                              stage_seconds=json.dumps(self.stage_times))
            self.log_score(status)
            if llm_hook is not None:
                try:
                    self.deps.llm.remove_hook(llm_hook)
                except Exception:
                    pass
            log("trace: %s" % self.trace.path)
        return {"status": status, "error": err, "saved": self.saved, "export": self.export_path,
                "trace": str(self.trace.path), "best": {k: v for k, v in (self.best or {}).items() if k in ("passed", "total", "label")},
                "stages": self.stage_times}

    def log_score(self, status):
        """One JSON line per build for the scoreboard (results/log.jsonl). Never raises."""
        try:
            b = self.best or {}
            tested = b.get("total") is not None
            rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "build", "status": status,
                   "agent_id": (self.saved or {}).get("agent_id"), "workflow_id": (self.saved or {}).get("workflow_id"),
                   "slot": self.slot, "bank": self.slot or slot_from_paths(self.brief_args),
                   "label": os.environ.get("RUN_LABEL") or "ours", "git_sha": git_sha(),
                   "build_seconds": round(self.budget.elapsed()), "stage_seconds": self.stage_times,
                   "passed": b.get("passed") if tested else None, "total": b.get("total") if tested else None,
                   "failed": sorted(cid for cid, r in self.results.items() if not r.get("pass")) if tested else [],
                   "estimated": bool(b.get("estimated")) if tested else None,
                   "score_kind": ("estimated" if b.get("estimated") else "verified") if tested else None,
                   "best_version": b.get("label"), "exported": self.export_path, "trace": str(self.trace.path)}
            path = pathlib.Path(score_log_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(path), "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
            self.trace.note("scoreboard line appended to %s" % path)
        except Exception as e:  # noqa: BLE001
            log("score log failed: %s" % e)

    def self_test_and_repair(self):
        if not self.budget.can_start(_estimate(1) + PLAN["casegen"] / 3.0, RESERVE_SAVE_S + RESERVE_END_S):
            self.trace.note("self-test skipped: not enough time (%.0f s left)" % self.budget.remaining())
            return
        try:
            cases = self.make_cases()
        except Exception as e:
            self.trace.note("self-test skipped: case generation failed: %s" % one_line(e, 300))
            return
        if not cases:
            self.trace.note("self-test skipped: no cases could be planned from the policy")
            return
        try:
            tester = self.deps.selftester(self.openapi_path, self.slot + "-selftest", self.state_file,
                                          trace=self.trace, policy=self.policy)
        except Exception as e:
            self.trace.note("self-test skipped: runner unavailable: %s" % one_line(e, 300))
            return
        run_cases = self.fit_cases(cases)
        res = self.run_test(tester, self.best["wf"], run_cases, "self-test v1")
        if not res or not res.get("ok"):
            return
        self.results = {r["id"]: r for r in res["rows"]}
        self.best.update(passed=res["passed"], total=res["total"], estimated=False)
        tested = {c["id"] for c in run_cases}
        version = 1
        rerun_done = False
        for rnd in range(1, self.repair_rounds + 1):
            failing = [r for r in self.results.values() if not r.get("pass")]
            if not failing:
                self.trace.note("all self-test cases pass; no repair needed")
                return
            if not rerun_done and all(not r.get("tools") for r in failing):
                # every failure stopped before the first tool call (a platform/code-step hiccup, seen on Freya): the
                # prompts are not the cause, so retest the same version once instead of spending a repair on them
                rerun_done = True
                again = [c for c in cases if c["id"] in {r["id"] for r in failing} and c["id"] in tested]
                if again and self.budget.can_start(_estimate(len(again)), RESERVE_SAVE_S + RESERVE_END_S):
                    self.trace.note("%d failure(s) without any tool call: retesting the same version (no repair)" % len(again))
                    res = self.run_test(tester, self.best["wf"], again, "self-test v%d (rerun, no change)" % version)
                    if not res or not res.get("ok"):
                        return
                    self.results.update({r["id"]: r for r in res["rows"]})
                    self.best.update(passed=sum(1 for r in self.results.values() if r.get("pass")))
                    continue
            passing = [cid for cid, r in self.results.items() if r.get("pass")]
            retest_ids = [r["id"] for r in failing] + random.Random(self.seed + rnd).sample(passing, min(2, len(passing)))
            retest = [c for c in cases if c["id"] in retest_ids and c["id"] in tested]
            fixed = REPAIR_MIN_S + REGEN_S + RESERVE_SAVE_S + RESERVE_END_S
            if not self.budget.can_start(_estimate(len(retest)), fixed):
                retest = [c for c in retest if c["id"] in set(r["id"] for r in failing)]
                retest = retest[:max_cases_within(self.budget.remaining() - fixed)]
            if not retest:
                self.trace.note("repair round %d skipped: not enough time (%.0f s left)" % (rnd, self.budget.remaining()))
                return
            t0 = self.stage_start("repair_%d" % rnd)
            sub = self.budget.sub(PLAN["repair_llm"], RESERVE_SAVE_S + RESERVE_END_S + _estimate(len(retest)) + REGEN_S, REPAIR_MIN_S)
            if not sub:
                self.stage_end("repair_%d" % rnd, t0, "skipped", "not enough time")
                return
            try:
                new_policy, new_prompts = self.repair(list(self.results.values()), self.best["policy"], self.best["prompts"],
                                                      self.best["wf"], sub, rnd)
                cands = []
                try:
                    cands.append(("repair %d (policy + prompts)" % rnd, self.generate(new_policy, new_prompts)))
                except Exception as e:
                    self.trace.note("repair %d: generate with patched policy failed: %s" % (rnd, one_line(e, 200)))
                if new_policy is not self.best["policy"]:
                    cands.append(("repair %d (prompts only)" % rnd, self.generate(self.best["policy"], new_prompts)))
                label, wf_new, verr = self.pick_valid(cands) if cands else (None, None, "no candidate")
                if verr or wf_new is None:
                    self.stage_end("repair_%d" % rnd, t0, "rejected", verr or "")
                    continue
                if "prompts only" in label:
                    new_policy = self.best["policy"]
                self.stage_end("repair_%d" % rnd, t0, "ok", label)
            except Exception as e:
                self.stage_end("repair_%d" % rnd, t0, "failed", one_line(e, 300))
                continue
            res = self.run_test(tester, wf_new, retest, "self-test v%d (retest after repair %d)" % (version + 1, rnd))
            if not res or not res.get("ok"):
                return
            old_sub = sum(1 for c in retest if self.results.get(c["id"], {}).get("pass"))
            new_sub = res["passed"]
            gain = new_sub - old_sub
            rows_new = {r["id"]: r for r in res["rows"]}
            full = len(retest) == len(tested)
            verdict, est = None, True
            if gain >= MIN_GAIN_PARTIAL or (full and gain >= 1):
                verdict, est = "kept", not full
            elif gain >= 1:
                # a gain of one case on a partial retest is noise-level: confirm on every tested case if time allows
                all_cases = [c for c in cases if c["id"] in tested]
                if self.budget.can_start(_estimate(len(all_cases)), RESERVE_SAVE_S + RESERVE_END_S):
                    res2 = self.run_test(tester, wf_new, all_cases, "self-test v%d (full retest after repair %d)" % (version + 1, rnd))
                    if res2 and res2.get("ok"):
                        best_passed = self.best["passed"] or 0
                        if res2["passed"] > best_passed:
                            rows_new = {r["id"]: r for r in res2["rows"]}
                            verdict, est = "kept", False
                            new_sub, old_sub = res2["passed"], best_passed
                        else:
                            self.trace.note("repair %d discarded after full retest: %d vs %d passing (%s)"
                                            % (rnd, res2["passed"], best_passed, self.best["label"]))
                            continue
                    else:
                        return
                else:
                    self.trace.note("repair %d discarded: gain of %d on a partial retest and no time for a full retest; kept %s"
                                    % (rnd, gain, self.best["label"]))
                    continue
            if verdict != "kept":
                self.trace.note("repair %d discarded: retested %d -> %d passing; kept %s"
                                % (rnd, old_sub, new_sub, self.best["label"]))
                continue
            for cid, r in rows_new.items():
                self.results[cid] = r
            new_total = sum(1 for cid in tested if self.results.get(cid, {}).get("pass"))
            version += 1
            try:
                self.save_and_export(wf_new, "v%d after repair %d" % (version, rnd))
            except Exception as e:  # noqa: BLE001
                self.trace.note("save of v%d failed (%s); %s stays the shipped version" % (version, one_line(e, 200), self.best["label"]))
                return
            self.best = {"wf": wf_new, "policy": new_policy, "prompts": new_prompts, "passed": new_total,
                         "total": self.best["total"], "label": "v%d" % version, "estimated": est}
            self.write_out("policy.json", new_policy)
            self.write_out("prompts.json", new_prompts)
            self.trace.note("repair %d kept: %s %d -> %d passing; %s total %d/%d"
                            % (rnd, "full retest" if not est else "retested subset", old_sub, new_sub,
                               "estimated" if est else "verified", new_total, self.best["total"]))


def score_log_path():
    """SCORE_LOG, else <kit>/results/log.jsonl next to build.py."""
    return os.environ.get("SCORE_LOG") or str(KIT / "results" / "log.jsonl")


def round_robin(cases, n):
    """Up to n cases, one per family in turn (families in first-seen order), returned in the original order."""
    if n >= len(cases):
        return list(cases)
    groups = {}
    for c in cases:
        groups.setdefault(c.get("family") or c.get("kind") or "", []).append(c)
    picked, depth = [], 0
    while len(picked) < n:
        added = False
        for g in groups.values():
            if depth < len(g) and len(picked) < n:
                picked.append(g[depth])
                added = True
        if not added:
            break
        depth += 1
    ids = set(id(c) for c in picked)
    return [c for c in cases if id(c) in ids]


def git_sha():
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(KIT), capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def _estimate(n):
    from builder.selftest import estimate_sim_s
    return estimate_sim_s(n)


def max_cases_within(seconds):
    from builder.selftest import max_cases_within as m
    return m(seconds)


def apply_policy_patch(policy, patch):
    """Copy of the policy with whole top-level sections replaced by the patch (known keys only, same type)."""
    out = copy.deepcopy(policy)
    for k, v in (patch or {}).items():
        if k in POLICY_KEYS and k != "tools" and v is not None and (k not in out or isinstance(v, type(out[k]))):
            out[k] = v
    return out
