#!/usr/bin/env bash
# Mirrors the organisers' Drive folder into final-case/ every INTERVAL seconds (default 180). Prints one line per
# new or changed file and starts the integration agent (integrate.sh) for them. Downloads go to a staging folder;
# nothing downloaded is ever executed.
URL="${DRIVE_URL:-https://drive.google.com/drive/folders/1r9KOek3SXiE0mu1wBofezD0bjiAJhcYQ}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; CASE="$ROOT/final-case"; STAGE="$ROOT/.drive-stage"; INT="${INTERVAL:-180}"
mkdir -p "$CASE"
while true; do
  rm -rf "$STAGE"; mkdir -p "$STAGE"
  if (cd "$STAGE" && uvx --quiet --with gdown python -I "$ROOT/ops/drive_sync.py" "$URL" "$STAGE/" >/dev/null 2>&1) && [ -n "$(find "$STAGE" -type f | head -1)" ]; then
    SRC="$(find "$STAGE" -mindepth 1 -maxdepth 1 -type d | head -1)"; [ -n "$SRC" ] || SRC="$STAGE"
    changed="$(cd "$SRC" && find . -type f ! -name .DS_Store | sed 's|^\./||' | sort | while read -r f; do cmp -s "$SRC/$f" "$CASE/$f" || echo "$f"; done)"
    if [ -n "$changed" ]; then
      rsync -a --exclude .DS_Store "$SRC/" "$CASE/"
      echo "$changed" | while read -r f; do echo "DRIVE CHANGED $(date +%H:%M:%S): $f"; done
      echo "$changed" >> "$ROOT/ops/.pending"
      bash "$ROOT/ops/integrate.sh" >> "$ROOT/ops/integrate.log" 2>&1 &
    fi
  else
    echo "DRIVE CHECK FAILED $(date +%H:%M:%S) (will retry)"
  fi
  sleep "$INT"
done
