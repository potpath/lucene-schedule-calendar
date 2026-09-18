#!/bin/bash
# Checks Lucene Ch.'s reused "weekly schedule" video thumbnail for a new week,
# and if found, updates public/schedule.ics and pushes it. That path is
# the asset root of the Cloudflare Worker that serves the calendar, so the push
# is the publish - there is no separate public repo to mirror into.
# Intended to run daily via launchd (see com.lucene.schedule-sync.plist).
set -uo pipefail

# Resolve from this script's own location so a clone works wherever it sits;
# CLAUDE_BIN falls back to whatever `claude` is on PATH.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo claude)}"
VIDEO_ID="O4FtQpWRAB8"
IMG="$REPO_DIR/schedule.jpg"
INPUT_NDJSON="$REPO_DIR/.last_sync_input.ndjson"
OUTPUT_NDJSON="$REPO_DIR/.last_sync_output.ndjson"
RESULT_JSON="$REPO_DIR/.last_sync_result.json"
LAST_ETAG_PATH="$REPO_DIR/.last_thumbnail_etag"
LAST_HASH_PATH="$REPO_DIR/.last_thumbnail_hash"

# A day carries a LIST of streams, not a single time/title: the graphic
# sometimes puts two lives on one row (e.g. "17:00 | 20:30"). The HH:MM pattern
# on time makes a merged "17:00 | 20:30" string fail extraction loudly instead
# of reaching sync_apply.py and crashing it mid-run.
SCHEMA='{"type":"object","properties":{"found_schedule":{"type":"boolean"},"week_monday":{"type":["string","null"]},"days":{"type":"array","items":{"type":"object","properties":{"day":{"type":"string","enum":["MON","TUE","WED","THU","FRI","SAT","SUN"]},"rest":{"type":"boolean"},"date":{"type":["string","null"]},"streams":{"type":"array","items":{"type":"object","properties":{"time":{"type":"string","pattern":"^([01][0-9]|2[0-3]):[0-5][0-9]$"},"title":{"type":"string"}},"required":["time","title"]}}},"required":["day","rest","date","streams"]},"minItems":7,"maxItems":7},"notes":{"type":"string"}},"required":["found_schedule","week_monday","days","notes"]}'

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Fetches just the ETag header for a thumbnail URL, no body download. YouTube's
# CDN sets this to what looks like the Unix timestamp of when that thumbnail
# was last (re)generated (verified: it decodes to a plausible recent date), so
# an unchanged ETag is a reliable "nothing to do" signal straight from
# YouTube - cheaper than even downloading the image, let alone calling claude.
fetch_etag() {
  curl -sI --fail "$1" 2>/dev/null | tr -d '\r' | grep -i '^etag:' | head -n1 | cut -d: -f2 | tr -d ' "'
}

# If a previous run committed locally but failed to push (network blip, auth
# expiry, etc.), the commit sits ahead of origin and next time we'd otherwise
# never look at it again (state on disk already says "synced"). So before
# doing anything else, retry pushing any such leftover commits.
# If a retry still fails, stop here rather than starting a fresh extraction
# on top of an already-inconsistent repo.
ensure_pushed() {
  local dir="$1"
  cd "$dir" || { log "ERROR: cannot cd to $dir"; exit 1; }
  git fetch origin main --quiet 2>>"$REPO_DIR/sync.log"
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

ensure_pushed "$REPO_DIR"

cd "$REPO_DIR" || { log "ERROR: cannot cd to $REPO_DIR"; exit 1; }
git pull --ff-only origin main || log "WARN: git pull failed, continuing with local state"

log "Checking thumbnail ETag (cheap pre-check, no download)..."
NEW_ETAG=$(fetch_etag "https://i.ytimg.com/vi/${VIDEO_ID}/maxresdefault.jpg")
[ -z "$NEW_ETAG" ] && NEW_ETAG=$(fetch_etag "https://i.ytimg.com/vi/${VIDEO_ID}/hqdefault.jpg")

LAST_ETAG=""
[ -f "$LAST_ETAG_PATH" ] && LAST_ETAG=$(cat "$LAST_ETAG_PATH")

if [ -n "$NEW_ETAG" ] && [ "$NEW_ETAG" = "$LAST_ETAG" ]; then
  log "Thumbnail ETag unchanged ($NEW_ETAG) - YouTube hasn't touched it since last run. Skipping download and claude call."
  exit 0
fi
[ -z "$NEW_ETAG" ] && log "WARN: could not read an ETag from either thumbnail size, falling back to download+hash check."

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

# Second gate, on actual bytes: catches the case where the CDN reissued a new
# ETag (e.g. re-encode/reprocess) without the visible content actually
# changing. Skipping claude here also avoids feeding it the same image twice,
# which would risk non-deterministic OCR output on an unchanged thumbnail.
NEW_HASH=$(shasum -a 256 "$IMG" | awk '{print $1}')
LAST_HASH=""
[ -f "$LAST_HASH_PATH" ] && LAST_HASH=$(cat "$LAST_HASH_PATH")

if [ "$NEW_HASH" = "$LAST_HASH" ]; then
  log "Thumbnail bytes unchanged (hash $NEW_HASH) even though the ETag differed - skipping claude call."
  rm -f "$IMG"
  [ -n "$NEW_ETAG" ] && echo "$NEW_ETAG" > "$LAST_ETAG_PATH"
  exit 0
fi

log "Thumbnail changed, extracting schedule via claude (zero tool access - Read/Bash/network all disabled)..."
# build_input.py also crops and enlarges the schedule rows into a second
# image (small stylized titles are misread at the thumbnail's native size),
# so it can now fail in ways that used to be impossible - check it, rather
# than handing claude a truncated/empty input and failing further downstream.
if ! python3 "$REPO_DIR/build_input.py" "$IMG" > "$INPUT_NDJSON" 2>>"$REPO_DIR/sync.log"; then
  log "ERROR: build_input.py could not prepare the extraction input (see sync.log)."
  log "Not recording thumbnail ETag/hash, so this same thumbnail is retried next run."
  rm -f "$IMG" "$INPUT_NDJSON"
  exit 1
fi

# Pin the model explicitly: the scheduled run otherwise inherits whatever the
# session default happens to be, and a run that fell through to Haiku produced
# badly garbled OCR that had to be reverted by hand. Opus reads the stylized
# logo lettering more faithfully than Sonnet (which misread "KULO" as "KULC"),
# and low effort is plenty for what is a reading task, not a reasoning one.
"$CLAUDE_BIN" -p \
  --model claude-opus-5 \
  --effort low \
  --input-format stream-json \
  --output-format stream-json \
  --verbose \
  --tools "" \
  --strict-mcp-config \
  --json-schema "$SCHEMA" \
  --settings '{"sandbox": {"enabled": true, "allowUnsandboxedCommands": false}}' \
  < "$INPUT_NDJSON" > "$OUTPUT_NDJSON" 2>>"$REPO_DIR/sync.log"

tail -n 1 "$OUTPUT_NDJSON" > "$RESULT_JSON"

python3 "$REPO_DIR/sync_apply.py" "$RESULT_JSON"
APPLY_STATUS=$?

rm -f "$IMG" "$INPUT_NDJSON" "$OUTPUT_NDJSON" "$RESULT_JSON"

if [ "$APPLY_STATUS" -eq 10 ]; then
  log "No schedule changes. Done."
  [ -n "$NEW_ETAG" ] && echo "$NEW_ETAG" > "$LAST_ETAG_PATH"
  echo "$NEW_HASH" > "$LAST_HASH_PATH"
  exit 0
elif [ "$APPLY_STATUS" -ne 0 ]; then
  log "ERROR: sync_apply.py failed (exit $APPLY_STATUS), leaving repos untouched."
  log "Not recording thumbnail ETag/hash, so this same thumbnail is retried next run."
  exit 1
fi

# Only now, once claude successfully parsed this exact image, do we record it
# as "seen" - a failed/errored run above deliberately skips this so the same
# thumbnail gets retried next time instead of being silently skipped forever.
[ -n "$NEW_ETAG" ] && echo "$NEW_ETAG" > "$LAST_ETAG_PATH"
echo "$NEW_HASH" > "$LAST_HASH_PATH"

log "New week applied. Committing..."
git add public/schedule.ics .last_week
if git diff --cached --quiet; then
  log "WARN: nothing staged (unexpected, sync_apply.py reported a change)."
else
  if ! git commit -m "Update schedule for week of $(cat .last_week)"; then
    log "ERROR: git commit failed."
    exit 1
  fi
  if ! git push origin main; then
    log "ERROR: git push failed. Will retry on next run."
    exit 1
  fi
  log "Pushed. Cloudflare redeploys public/schedule.ics from this commit."
fi
