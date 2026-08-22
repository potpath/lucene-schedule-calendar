#!/bin/bash
# Checks Lucene Ch.'s reused "weekly schedule" video thumbnail for a new week,
# and if found, updates lucene-schedule.ics and pushes it to GitHub.
# Intended to run daily via launchd (see com.lucene.schedule-sync.plist).
set -uo pipefail

REPO_DIR="$HOME/code/youtube/calendar"
CLAUDE_BIN="/usr/local/bin/claude"
VIDEO_ID="O4FtQpWRAB8"
IMG="$REPO_DIR/schedule.jpg"
RESULT_JSON="$REPO_DIR/.last_sync_result.json"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

cd "$REPO_DIR" || { log "ERROR: cannot cd to $REPO_DIR"; exit 1; }

git pull --ff-only origin main || log "WARN: git pull failed, continuing with local state"

log "Downloading schedule thumbnail..."
if ! curl -sL --fail "https://i.ytimg.com/vi/${VIDEO_ID}/maxresdefault.jpg" -o "$IMG"; then
  log "maxresdefault failed, trying hqdefault..."
  if ! curl -sL --fail "https://i.ytimg.com/vi/${VIDEO_ID}/hqdefault.jpg" -o "$IMG"; then
    log "ERROR: could not download schedule thumbnail (both sizes failed)"
    exit 1
  fi
fi

if ! file "$IMG" | grep -qi "image"; then
  log "ERROR: downloaded file is not an image"
  exit 1
fi

log "Extracting schedule via claude..."
"$CLAUDE_BIN" -p "Read the image schedule.jpg. It is a weekly livestream schedule graphic for a VTuber YouTube channel (Thai text, GMT+7 timezone). Extract it into the required JSON schema. The title area shows a date range (e.g. '17 AUG - 23 AUG') and a month/year (e.g. 'AUGUST 2026') - use these to compute the Monday date of that week as week_monday (YYYY-MM-DD). Then for each day MON through SUN, in order: if the row says REST, set rest=true and leave date/time/title null. Otherwise set rest=false and fill date (as shown, DD/MM/YYYY converted to YYYY-MM-DD), time (as shown, 24h HH:MM, this is local GMT+7 time), and title (transcribe the stream title text exactly as shown, do not translate). If schedule.jpg does not look like this kind of weekly schedule graphic at all, set found_schedule=false and explain why in notes, rather than guessing." \
  --tools Read \
  --permission-mode bypassPermissions \
  --output-format json \
  --json-schema '{"type":"object","properties":{"found_schedule":{"type":"boolean"},"week_monday":{"type":["string","null"]},"days":{"type":"array","items":{"type":"object","properties":{"day":{"type":"string","enum":["MON","TUE","WED","THU","FRI","SAT","SUN"]},"rest":{"type":"boolean"},"date":{"type":["string","null"]},"time":{"type":["string","null"]},"title":{"type":["string","null"]}},"required":["day","rest","date","time","title"]},"minItems":7,"maxItems":7},"notes":{"type":"string"}},"required":["found_schedule","week_monday","days","notes"]}' \
  > "$RESULT_JSON" 2>>"$REPO_DIR/sync.log"

python3 "$REPO_DIR/sync_apply.py" "$RESULT_JSON"
APPLY_STATUS=$?

rm -f "$IMG" "$RESULT_JSON"

if [ "$APPLY_STATUS" -eq 10 ]; then
  log "No new week posted yet. Done."
  exit 0
elif [ "$APPLY_STATUS" -ne 0 ]; then
  log "ERROR: sync_apply.py failed (exit $APPLY_STATUS), leaving repo untouched."
  exit 1
fi

log "New week applied, committing and pushing..."
git add lucene-schedule.ics .last_week
if git commit -m "Update schedule for week of $(cat .last_week)"; then
  if git push origin main; then
    log "Pushed successfully."
  else
    log "ERROR: git push failed."
    exit 1
  fi
else
  log "Nothing to commit (unexpected - sync_apply.py said it applied changes)."
fi
