#!/bin/bash
# Hourly snapshot of data.json, written only when the file actually changed.
#
#   crontab -e
#   0 * * * * /path/to/custody-calendar/backup_calendar.sh >> /path/to/custody-calendar/backup_calendar.log 2>&1
#
# Snapshots land in <backup_dir>/<year>/<month>/<timestamp>.json.
# backup_dir and backup_keep (how many snapshots to retain) come from config.json.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/data.json"
CFG="$DIR/config.json"

cfg() {  # cfg <key> <fallback>
  python3 -c "
import json,sys
try:
    print(json.load(open('$CFG')).get('$1', '$2'))
except Exception:
    print('$2')
" 2>/dev/null || echo "$2"
}

DEST="$(cfg backup_dir ../backup_custody_calendar)"
KEEP="$(cfg backup_keep 1000)"
case "$DEST" in /*) ;; *) DEST="$DIR/$DEST" ;; esac
case "$KEEP" in ''|*[!0-9]*) KEEP=1000 ;; esac

[ -f "$SRC" ] || { echo "$(date -Is) no data.json yet, nothing to back up"; exit 0; }

# never archive a corrupt file - that would poison the "last known good" snapshot
if ! python3 -c "import json,sys; json.load(open('$SRC'))" 2>/dev/null; then
  echo "$(date -Is) WARNING: data.json is not valid JSON - skipping this backup"
  exit 1
fi

SUB="$DEST/$(date +%Y)/$(date +%m-%B)"
mkdir -p "$SUB" || { echo "$(date -Is) cannot create $SUB"; exit 1; }

# compare against the newest existing snapshot
LAST="$(find "$DEST" -type f -name '*.json' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
if [ -n "$LAST" ] && cmp -s "$SRC" "$LAST"; then
  exit 0   # unchanged since the last snapshot
fi

# hourly runs give one clean file per hour; extra runs inside the same minute must
# never silently overwrite an existing snapshot, so find a free name
BASE="$SUB/$(date +%Y-%m-%d_%H-%M)"
OUT="$BASE.json"
n=0
while [ -e "$OUT" ]; do
  n=$((n + 1))
  OUT="$BASE-$n.json"
done
cp "$SRC" "$OUT.part" && mv "$OUT.part" "$OUT" || { echo "$(date -Is) copy failed"; exit 1; }
DAYS="$(python3 -c "import json;print(len(json.load(open('$OUT'))['days']))" 2>/dev/null || echo '?')"
echo "$(date -Is) backed up $DAYS days -> $OUT"

# retention: keep the newest $KEEP snapshots
TOTAL="$(find "$DEST" -type f -name '*.json' | wc -l)"
if [ "$TOTAL" -gt "$KEEP" ]; then
  find "$DEST" -type f -name '*.json' -printf '%T@ %p\n' | sort -n | head -n "$((TOTAL - KEEP))" \
    | cut -d' ' -f2- | while read -r old; do rm -f "$old"; done
  echo "$(date -Is) pruned $((TOTAL - KEEP)) old snapshot(s), keeping $KEEP"
fi
