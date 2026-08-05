#!/bin/bash
# push_dashboard.sh — regenerate data.json and push to GitHub Pages repo.
# Cron watchdog semantics: silent on success, prints on failure (exit 1).
set -uo pipefail
DASH="/Users/mohindpa/Documents/My Files/Curiosity Projects/INSSIST/ogletrends-dashboard"
export PATH="$HOME/.composio:$PATH"

# 1. Regenerate data.json
OUT=$(python3 "$DASH/build_data.py" 2>&1)
if [ $? -ne 0 ]; then
  echo "DASHBOARD DATA FAILED: $OUT"
  exit 1
fi
# If Gumroad source went unavailable, still push stale data but note it
if echo "$OUT" | grep -q "unavailable"; then
  echo "WARN: Gumroad unavailable, keeping stale data: $(echo "$OUT" | grep unavailable | head -1)"
fi

# 2. Commit + push
cd "$DASH"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "DASHBOARD NOT A GIT REPO — init once first"
  exit 1
fi
git add -A
# Only commit if something changed
if git diff --cached --quiet; then
  echo "no changes"
  exit 0
fi
TS=$(date "+%Y-%m-%d %H:%M:%S %Z")
if git commit -m "update dashboard data [$TS]" >/dev/null 2>&1; then
  if git push origin main 2>&1; then
    echo "pushed [$TS]"
  else
    echo "PUSH FAILED"
    exit 1
  fi
else
  echo "commit failed"
  exit 1
fi
