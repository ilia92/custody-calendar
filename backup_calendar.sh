#!/bin/bash
# Hourly snapshot of data.json, only when it actually changed.
# crontab -e  ->  0 * * * * /path/to/custody-calendar/backup_calendar.sh >> /tmp/custody_backup.log 2>&1
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/data.json"
DEST="$(python3 -c "import json,sys;print(json.load(open('$DIR/config.json')).get('backup_dir','../backup_custody_calendar'))" 2>/dev/null || echo ../backup_custody_calendar)"
case "$DEST" in /*) ;; *) DEST="$DIR/$DEST" ;; esac

[ -f "$SRC" ] || { echo "no data.json yet"; exit 0; }
STAMP="$(date +%Y-%m-%d_%H-%M)"
SUB="$DEST/$(date +%Y)/$(date +%m-%B)"
mkdir -p "$SUB"

LAST="$(ls -1t "$DEST"/*/*/*.json 2>/dev/null | head -1)"
if [ -n "$LAST" ] && [ "$(md5sum < "$SRC" | cut -d' ' -f1)" = "$(md5sum < "$LAST" | cut -d' ' -f1)" ]; then
  exit 0   # unchanged since the last snapshot
fi
cp "$SRC" "$SUB/$STAMP.json"
echo "$(date -Is) backed up to $SUB/$STAMP.json"
