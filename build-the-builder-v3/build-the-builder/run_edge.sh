#!/usr/bin/env bash
# Edge-case test: 23 tricky customers, 2 runs each, against this builder's agent.
#   bash run_edge.sh            test the agent this folder built last (builds one first if there is none)
#   bash run_edge.sh --build    build a fresh agent from Brief A+B first
#   bash run_edge.sh --quick    only the 23 hand-written cases (default: all 99, about 40-60 minutes)
#   AGENT_ID=<id> bash run_edge.sh   test that agent instead
# Results: results/edge-report.txt (readable) and results/run-edge.json (details, call ids).
set -e
cd "$(dirname "$0")"
mkdir -p results
[ -f env.sh ] || { [ -f "$HOME/BuildTheBuilder/env.sh" ] && cp "$HOME/BuildTheBuilder/env.sh" env.sh; }
[ -f env.sh ] || { echo "env.sh missing: copy env.example to env.sh and put your keys in it."; exit 1; }
source env.sh
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv -q --python 3.12 .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
  else python3.12 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt; fi
fi
PY=.venv/bin/python
exec > >(tee results/edge-report.txt) 2>&1
echo "=== EDGE TEST $(date) ==="
if [[ " $* " == *" --build "* ]] || [ ! -f .freya-agents.json ]; then
  $PY build.py briefs/brief-a.md briefs/brief-b.md --openapi tools/openapi.json
fi
# the phone agent, not the build's "-selftest" copy (fake bank, needs scenario_json); AGENT_ID=... overrides
AGENT=${AGENT_ID:-$($PY -c "import json;print([v for k,v in json.load(open('.freya-agents.json')).items() if not k.endswith('-selftest')][-1]['agent_id'])")}
echo "agent_id: $AGENT"
$PY tools/edge_cases.py
SET=results/edge-cases.json; [[ " $* " == *" --quick "* ]] && SET=results/edge-cases-quick.json
$PY tools/run_cases.py "$AGENT" --cases "$SET" --reps 2 --out results/run-edge.json
echo "=== DONE $(date) ==="
