#!/usr/bin/env bash
# Edge-case test: 23 tricky customers, 2 runs each, against this builder's agent.
#   bash run_edge.sh            test the agent this folder built last (builds one first if there is none)
#   bash run_edge.sh --build    build a fresh agent from Brief A+B first
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
if [ "$1" = "--build" ] || [ ! -f .freya-agents.json ]; then
  $PY build.py briefs/brief-a.md briefs/brief-b.md --openapi tools/openapi.json
fi
AGENT=$($PY -c "import json;print(list(json.load(open('.freya-agents.json')).values())[-1]['agent_id'])")
echo "agent_id: $AGENT"
$PY tools/edge_cases.py
$PY tools/run_cases.py "$AGENT" --cases results/edge-cases.json --reps 2 --out results/run-edge.json
echo "=== DONE $(date) ==="
