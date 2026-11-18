#!/bin/bash
# Relaunch the Kalshi collectors listed in a manifest, e.g. after the Mac restarts.
#
# Manifest lines (blank lines and lines starting with # are ignored):
#     out_dir|interval_s|start_at (may be empty)|other collect arguments
#     data/raw/den_nor|5|2026-11-17T19:30:00Z|--event KXUEFANLGAME-26NOV17DENNOR --exhaustive
#
# A collector is skipped if one is already running for that out_dir, or if its
# meta.json already has a settlement (finished). Collectors resume where they
# stopped, and --start-at waits only if that time is still in the future, so
# running this script twice is harmless.
#
# Usage: scripts/resume_collectors.sh [manifest]   (default: data/raw/collectors.txt)

cd "$(dirname "$0")/.." || exit 1
PY=${PYTHON:-/opt/anaconda3/bin/python}
MANIFEST=${1:-data/raw/collectors.txt}
echo "$(date -u '+%Y-%m-%d %H:%M:%S') resume_collectors: reading $MANIFEST"

while IFS='|' read -r dir interval start_at extra; do
  [[ -z "$dir" || "$dir" == \#* ]] && continue
  if pgrep -f -- "--out $dir( |$)" >/dev/null; then
    echo "  running:  $dir"; continue
  fi
  if [[ -f "$dir/meta.json" ]] && "$PY" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("settlement") else 1)' "$dir/meta.json"; then
    echo "  finished: $dir"; continue
  fi
  args=(-u -m data.kalshi collect --out "$dir" --interval "$interval")
  [[ -n "$start_at" ]] && args+=(--start-at "$start_at")
  read -r -a more <<< "$extra"
  mkdir -p "$dir"
  nohup caffeinate -i "$PY" "${args[@]}" "${more[@]}" >> "$dir/collector.log" 2>&1 &
  echo "  started:  $dir (pid $!)"
done < "$MANIFEST"
