#!/bin/bash
# Checks Lucene Ch.'s reused "weekly schedule" video thumbnail for a new week,
# and if found, updates lucene-schedule.ics (kept privately here for history)
# and mirrors just that file to the public lucene-schedule-ics repo.
# Intended to run daily via launchd (see com.lucene.schedule-sync.plist).
set -uo pipefail

PRIVATE_DIR="$HOME/code/youtube/calendar"
PUBLIC_DIR="$HOME/code/youtube/calendar-public"
CLAUDE_BIN="/usr/local/bin/claude"
VIDEO_ID="O4FtQpWRAB8"
IMG="$PRIVATE_DIR/schedule.jpg"
INPUT_NDJSON="$PRIVATE_DIR/.last_sync_input.ndjson"
OUTPUT_NDJSON="$PRIVATE_DIR/.last_sync_output.ndjson"
RESULT_JSON="$PRIVATE_DIR/.last_sync_result.json"

SCHEMA='{"type":"object","properties":{"found_schedule":{"type":"boolean"},"week_monday":{"type":["string","null"]},"days":{"type":"array","items":{"type":"object","properties":{"day":{"type":"string","enum":["MON","TUE","WED","THU","FRI","SAT","SUN"]},"rest":{"type":"boolean"},"date":{"type":["string","null"]},"time":{"type":["string","null"]},"title":{"type":["string","null"]}},"required":["day","rest","date","time","title"]},"minItems":7,"maxItems":7},"notes":{"type":"string"}},"required":["found_schedule","week_monday","days","notes"]}'

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# If a previous run committed locally but failed to push (network blip, auth
# expiry, etc.), the commit sits ahead of origin and next time we'd otherwise
# never look at it again (state on disk already says "synced"). So before
# doing anything else, retry pushing any such leftover commits in both repos.
# If a retry still fails, stop here rather than starting a fresh extraction
# on top of an already-inconsistent repo.
ensure_pushed() {
  local dir="$1"
  cd "$dir" || { log "ERROR: cannot cd to $dir"; exit 1; }
  git fetch origin main --quiet 2>>"$PRIVATE_DIR/sync.log"
  local ahead
  ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
  if [ "$ahead" -gt 0 ]; then
    log "$dir has $ahead unpushed commit(s) from a previous run, retrying push..."
    if git push origin main; then
      log "Retry push succeeded for $dir."
    else
      log "ERROR: retry push still failing for $dir. Will retry again next run."
      exit 1
    fi
  fi
}

ensure_pushed "$PRIVATE_DIR"
ensure_pushed "$PUBLIC_DIR"

cd "$PRIVATE_DIR" || { log "ERROR: cannot cd to $PRIVATE_DIR"; exit 1; }
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

log "Extracting schedule via claude (zero tool access - Read/Bash/network all disabled)..."
python3 "$PRIVATE_DIR/build_input.py" "$IMG" > "$INPUT_NDJSON"

"$CLAUDE_BIN" -p \
  --input-format stream-json \
  --output-format stream-json \
  --verbose \
  --tools "" \
  --json-schema "$SCHEMA" \
  < "$INPUT_NDJSON" > "$OUTPUT_NDJSON" 2>>"$PRIVATE_DIR/sync.log"

tail -n 1 "$OUTPUT_NDJSON" > "$RESULT_JSON"

python3 "$PRIVATE_DIR/sync_apply.py" "$RESULT_JSON"
APPLY_STATUS=$?

rm -f "$IMG" "$INPUT_NDJSON" "$OUTPUT_NDJSON" "$RESULT_JSON"

if [ "$APPLY_STATUS" -eq 10 ]; then
  log "No schedule changes. Done."
  exit 0
elif [ "$APPLY_STATUS" -ne 0 ]; then
  log "ERROR: sync_apply.py failed (exit $APPLY_STATUS), leaving repos untouched."
  exit 1
fi

log "New week applied. Committing private repo (scripts/state/history)..."
git add lucene-schedule.ics .last_week
if git diff --cached --quiet; then
  log "WARN: nothing staged in private repo (unexpected, sync_apply.py reported a change)."
else
  if ! git commit -m "Update schedule for week of $(cat .last_week)"; then
    log "ERROR: private repo git commit failed."
    exit 1
  fi
  if ! git push origin main; then
    log "ERROR: private repo git push failed. Will retry on next run."
    exit 1
  fi
fi

log "Mirroring lucene-schedule.ics to public repo..."
cp "$PRIVATE_DIR/lucene-schedule.ics" "$PUBLIC_DIR/lucene-schedule.ics"
cd "$PUBLIC_DIR" || { log "ERROR: cannot cd to $PUBLIC_DIR"; exit 1; }
git add lucene-schedule.ics
if git diff --cached --quiet; then
  log "WARN: nothing staged in public repo (unexpected, private copy just changed)."
else
  if ! git commit -m "Update schedule for week of $(cat "$PRIVATE_DIR/.last_week")"; then
    log "ERROR: public repo git commit failed."
    exit 1
  fi
  if ! git push origin main; then
    log "ERROR: public repo git push failed. Will retry on next run."
    exit 1
  fi
  log "Public repo pushed successfully."
fi
