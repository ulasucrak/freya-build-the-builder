"""Build a Freya phone agent from a bank's brief, end to end, with no manual step.

    python build.py <brief files or folder...> --openapi <tool API file> [--slot NAME] [--budget-min 28]
                    [--no-test] [--best-of N] [--out-dir out/]

Stages: extract policy -> validate -> generate workflow -> LLM prompts -> Freya dry run -> save + export
-> self-test on synthetic cases (test mode) -> up to 2 repair rounds -> final save + export.
Writes out/<slot>.workflow.json (the export to submit) and traces/<slot>-<UTC time>.txt (+ .json).

Needs FREYA_API_KEY (and FREYA_BANK_TEAM_KEY for the self-test's close_call sink) and a Claude login or
ANTHROPIC_API_KEY. A self-test runs a simulation: only one per workspace at a time. Before each simulation the
builder checks that Freya (and the bank mock) answer and skips the test when they do not; every simulation has a
hard timeout. With ORCH_SIM_LOCK=1 (development only, off for the real run) each simulation, and nothing else, runs
under `orch.py with-lock db`. Do not wrap the whole build in the lock.
"""
import argparse
import json
import os
import pathlib
import sys

KIT = pathlib.Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(KIT))

from builder.pipeline import Builder  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Brief(s) + tool API file -> Freya phone agent (saved, exported, traced).")
    ap.add_argument("briefs", nargs="+", help="brief files, or a folder of brief material (later files override earlier)")
    ap.add_argument("--openapi", help="the bank's tool API file (openapi.json); without it: document mode (services read from the documents)")
    ap.add_argument("--mock-url", default=os.environ.get("MOCK_URL", ""), help="document mode: base URL of the bank's services the agent calls")
    ap.add_argument("--slot", help="label of the workflow in the state file (default: first brief's file name)")
    ap.add_argument("--budget-min", type=float, default=28.0, help="hard wall clock for the whole run, minutes (default 28)")
    ap.add_argument("--no-test", action="store_true", help="skip the self-test and repair")
    ap.add_argument("--best-of", type=int, default=1, help="prompt candidates written in parallel; the best one is used")
    ap.add_argument("--out-dir", help="where the exported workflow goes (default: <kit>/out)")
    ap.add_argument("--trace-dir", help="where the trace goes (default: <kit>/traces)")
    ap.add_argument("--state-file", help="slot -> workflow id file (default: <kit>/.freya-agents.json)")
    ap.add_argument("--max-cases", type=int, help="self-test cases (default 10)")
    ap.add_argument("--repair-rounds", type=int, default=2)
    ap.add_argument("--no-prompts", action="store_true", help="keep the generator's prompts (no LLM prompt stage)")
    ap.add_argument("--seed", type=int)
    a = ap.parse_args(argv)
    if not a.openapi:
        from builder import docmode
        kit = pathlib.Path(__file__).resolve().parent
        res = docmode.build(a.briefs, a.mock_url, a.slot or "final", a.out_dir or str(kit / "out"), a.trace_dir or str(kit / "traces"),
                            a.state_file or str(kit / ".freya-agents.json"), budget_min=a.budget_min)
        print(json.dumps(res, indent=1, default=str))
        return 0 if res["status"] == "ok" else 1
    b = Builder(a.briefs, a.openapi, slot=a.slot, budget_min=a.budget_min, no_test=a.no_test, best_of=a.best_of,
                out_dir=a.out_dir, trace_dir=a.trace_dir, state_file=a.state_file, max_cases=a.max_cases,
                repair_rounds=a.repair_rounds, no_prompts=a.no_prompts, seed=a.seed)
    res = b.run()
    print(json.dumps(res, indent=1, default=str))
    return 0 if res["status"].startswith("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
