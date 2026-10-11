#!/usr/bin/env bash
# Integration agent: takes the files the Drive watcher reported (ops/.pending) and integrates them.
#  - test-environment/* changed -> regenerate the mock's data.json, verify every answer locally, and, when Cloudflare
#    is logged in, deploy and verify again on the live address (written to final-mock/.mock_url).
#  - anything else (documents, example scenarios, brief) -> rebuild the phone agent with the builder from final-case/:
#    with the live mock address and self-test when the mock is deployed, else a trial build without self-test.
# One run at a time (lock). Files that arrive during a run are handled by one more round right after it.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; KIT="$ROOT/build-the-builder-v3/build-the-builder"; LOCK="$ROOT/ops/.lock"
mkdir "$LOCK" 2>/dev/null || { echo "INTEGRATE busy: queued $(date +%H:%M:%S)"; exit 0; }
trap 'rmdir "$LOCK"' EXIT
while [ -s "$ROOT/ops/.pending" ]; do
  changed="$(sort -u "$ROOT/ops/.pending")"; : > "$ROOT/ops/.pending"
  echo "INTEGRATE start $(date +%H:%M:%S): $(echo "$changed" | tr '\n' ' ')"
  if echo "$changed" | grep -q '^test-environment/'; then
    (cd "$ROOT/final-mock" && python3 build_data.py "$ROOT/final-case/test-environment/test-customers.json") | sed 's/^/INTEGRATE mock data: /'
    (cd "$ROOT/final-mock" && (npx --yes wrangler dev --port 8799 >/tmp/wdev-int.log 2>&1 &) ; for i in $(seq 1 40); do curl -s -m 2 -X POST localhost:8799/x >/dev/null 2>&1 && break; sleep 1; done
     python3 verify.py "$ROOT/final-case/test-environment/test-customers.json" 2>&1 | grep -E "exact matches" | sed 's/^/INTEGRATE mock local: /'; pkill -f "wrangler dev --port 8799")
    if ! npx --yes wrangler whoami 2>&1 | grep -q "not authenticated"; then
      url="$(cd "$ROOT/final-mock" && npx --yes wrangler deploy 2>&1 | grep -oE 'https://[a-z0-9.-]+\.workers\.dev' | head -1)"
      if [ -n "$url" ]; then echo "$url" > "$ROOT/final-mock/.mock_url"; echo "INTEGRATE mock deployed: $url"
        python3 "$ROOT/final-mock/verify.py" "$ROOT/final-case/test-environment/test-customers.json" "$url/voice/v1/" 2>&1 | grep -E "exact matches" | sed 's/^/INTEGRATE mock live: /'
      else echo "INTEGRATE mock deploy FAILED"; fi
    else echo "INTEGRATE mock not deployed: Cloudflare not logged in (run: npx wrangler login)"; fi
  fi
  if echo "$changed" | grep -qv '^test-environment/\|^cloudflare-mock/'; then
    cd "$KIT" && source env.sh
    if [ -s "$ROOT/final-mock/.mock_url" ]; then
      .venv/bin/python build.py "$ROOT/final-case" --mock-url "$(cat "$ROOT/final-mock/.mock_url")" --slot final > "$ROOT/ops/build-final.log" 2>&1
      echo "INTEGRATE agent rebuilt (slot final): $(grep -o '"status": "[^"]*"' "$ROOT/ops/build-final.log" | tail -1) $(grep -E 'test run|score' "$(ls -t traces/final-*.txt | head -1)" | tail -2 | tr '\n' ' ')"
    else
      .venv/bin/python build.py "$ROOT/final-case" --mock-url https://freya-final-mock.example.workers.dev --slot final-trial --no-test > "$ROOT/ops/build-trial.log" 2>&1
      echo "INTEGRATE trial agent rebuilt (no live mock yet): $(grep -o '"status": "[^"]*"' "$ROOT/ops/build-trial.log" | tail -1)"
    fi
  fi
  echo "INTEGRATE done $(date +%H:%M:%S)"
done
