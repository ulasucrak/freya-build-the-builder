#!/usr/bin/env bash
# Waits for the running chain (full_chain.sh, s6_after.sh), then shares its results: copies them to
# shared-results/<day>/, writes failure transcripts, appends a results section to HANDOFF.md and pushes to main.
#   nohup bash tools/publish_results.sh 2026-10-11 > results/publish.log 2>&1 &
set -u
KIT="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$KIT/../.." && pwd)"; DAY="${1:-$(date +%F)}"
cd "$KIT"
while pgrep -f "full_chain.sh|s6_after.sh|run_cases.py|build.py" >/dev/null; do sleep 30; done
source env.sh
D="$ROOT/shared-results/$DAY"; mkdir -p "$D"
for f in r250b-report.txt run-250b.json days-report.txt run-days.json build-opt3.log build-s6b.log s6-after.log full-chain.log edge-cases-250.json edge-cases-days.json; do
  [ -f "results/$f" ] && cp "results/$f" "$D/"
done
[ -f results/run-250b.json ] && .venv/bin/python tools/fail_transcripts.py FAILED "$D/tx-250b" results/run-250b.json
[ -f results/run-days.json ] && .venv/bin/python tools/fail_transcripts.py FAILED "$D/tx-days" results/run-days.json
[ -f results/run-250b.json ] && .venv/bin/python tools/turn_latency.py "$D/turns-250b.json" results/run-250b.json > "$D/latency-250b.txt" 2>&1
{
  echo; echo "## 15. Results of the chain (appended automatically $(date '+%d %b %H:%M'))"; echo
  echo '```'
  echo "--- 250 re-run (compare: 222/250 before the fixes)"; grep -E "passed|slowest|WARNING" results/r250b-report.txt 2>/dev/null | tail -3
  echo "--- failed in the 250 re-run:"; grep -E "^FAIL " results/r250b-report.txt 2>/dev/null | sed 's/ |.*//' | head -60
  echo "--- call days x2 (84 cases, 30 Brief B)"; grep -E "passed|slowest|run [12] of" results/days-report.txt 2>/dev/null | tail -4
  echo "--- failed on call days:"; grep -E "^FAIL [A-Z].* [0-9]/2$" results/days-report.txt 2>/dev/null | head -40
  echo "--- S6 rebuild with the fixed self-test bank (compare: self-test 1/10 with the broken bank)"; grep -E "stage (save|self-test|repair).*:|budget" results/build-s6b.log 2>/dev/null | tail -6
  echo "--- turn latency"; head -8 "$D/latency-250b.txt" 2>/dev/null
  echo '```'
  echo; echo "Transcripts of every failure: \`shared-results/$DAY/tx-250b/\`, \`tx-days/\`. Next: §12 step 2."
} >> "$ROOT/HANDOFF.md"
cd "$ROOT"
git add HANDOFF.md "shared-results/$DAY"
git commit -q -m "Chain results ($DAY): 250 re-run, call days, S6 rebuild (auto)" || exit 0
git pull -q --rebase origin main && git push -q origin main && echo "pushed $(git log --oneline -1)"
