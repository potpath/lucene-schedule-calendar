#!/bin/bash
# Keeps public/schedule.ics in step with Lucene Ch.'s published schedule, from
# two sources: her channel's reused "weekly schedule" video thumbnail, and
# Polygon Project's weekly post covering every member of the agency. public/ is
# the asset root of the Cloudflare Worker that serves the calendar, so the push
# is the publish - there is no separate public repo to mirror into.
# Intended to run daily via launchd (see com.lucene.schedule-sync.plist).
#
# The channel's own thumbnail is authoritative and the agency post is early:
# see sync_apply.py's module docstring for the precedence rule. Both sources
# are checked every run, cheaply gated so that the expensive step - the claude
# extraction - only happens when that source has published something new, and
# both feed the same commit at the end.
set -uo pipefail

# Resolve from this script's own location so a clone works wherever it sits;
# CLAUDE_BIN falls back to whatever `claude` is on PATH.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo claude)}"
VIDEO_ID="O4FtQpWRAB8"
IMG="$REPO_DIR/schedule.jpg"
LAST_ETAG_PATH="$REPO_DIR/.last_thumbnail_etag"
LAST_HASH_PATH="$REPO_DIR/.last_thumbnail_hash"
LAST_X_STATUS_PATH="$REPO_DIR/.last_x_status"

# A day carries a LIST of streams, not a single time/title: the graphic
# sometimes puts two lives on one row (e.g. "17:00 | 20:30"). The HH:MM pattern
# on time makes a merged "17:00 | 20:30" string fail extraction loudly instead
# of reaching sync_apply.py and crashing it mid-run.
SCHEMA='{"type":"object","properties":{"found_schedule":{"type":"boolean"},"week_monday":{"type":["string","null"]},"days":{"type":"array","items":{"type":"object","properties":{"day":{"type":"string","enum":["MON","TUE","WED","THU","FRI","SAT","SUN"]},"rest":{"type":"boolean"},"date":{"type":["string","null"]},"streams":{"type":"array","items":{"type":"object","properties":{"time":{"type":"string","pattern":"^([01][0-9]|2[0-3]):[0-5][0-9]$"},"title":{"type":"string"}},"required":["time","title"]}}},"required":["day","rest","date","streams"]},"minItems":7,"maxItems":7},"notes":{"type":"string"}},"required":["found_schedule","week_monday","days","notes"]}'

# Weeks applied this run, for the commit message.
APPLIED_WEEKS=()

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

# OCRs one schedule graphic into structured JSON and applies it. Shared by both
# sources; everything source-specific is in the --source prompt and the
# precedence rule downstream.
# Usage: extract_and_apply <tag> <image> --source <name>
# Returns sync_apply.py's exit status (0 changed, 10 unchanged, 11 not this
# source's week to write, anything else an error).
extract_and_apply() {
  local tag="$1" img="$2"; shift 2
  local source_args=("$@")

  local input="$REPO_DIR/.last_sync_input_${tag}.ndjson"
  local output="$REPO_DIR/.last_sync_output_${tag}.ndjson"
  local result="$REPO_DIR/.last_sync_result_${tag}.json"

  # build_input.py also crops and enlarges the schedule rows into a second
  # image (small stylized titles are misread at the thumbnail's native size),
  # so it can now fail in ways that used to be impossible - check it, rather
  # than handing claude a truncated/empty input and failing further downstream.
  if ! python3 "$REPO_DIR/build_input.py" "${source_args[@]}" "$img" > "$input" 2>>"$REPO_DIR/sync.log"; then
    log "ERROR: build_input.py could not prepare the $tag extraction input (see sync.log)."
    rm -f "$input"
    return 1
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
    < "$input" > "$output" 2>>"$REPO_DIR/sync.log"

  tail -n 1 "$output" > "$result"

  python3 "$REPO_DIR/sync_apply.py" "${source_args[@]}" "$result"
  local status=$?

  rm -f "$input" "$output" "$result"
  return $status
}

# Records a week for the commit message. Callers pass the marker file they
# wrote, so a caller with its own marker still ends up in the same commit.
record_applied_week() {
  APPLIED_WEEKS+=("$(cat "$1")")
}

# Polygon Project's weekly post - the early source. It goes up on Sunday for the
# week starting the next day, a day or two before the channel's own thumbnail
# follows, and that head start is the whole of what this source adds.
# x_source.py reads its endpoints from git-ignored local config and reports
# "not configured" (exit 10) when that is absent, so a fresh clone of this
# public repo simply runs the thumbnail source and nothing breaks.
sync_polygon() {
  local img="$REPO_DIR/polygon.jpg"
  local meta_path="$REPO_DIR/.last_x_meta.json"
  local last_status=""
  [ -f "$LAST_X_STATUS_PATH" ] && last_status=$(cat "$LAST_X_STATUS_PATH")

  log "Checking the agency account for a newer weekly schedule post..."
  # One status id is all that comes out of the third-party mirror this uses to
  # find the post; the post itself and its image come from the platform's own
  # endpoint. See x_source.py.
  python3 "$REPO_DIR/x_source.py" --skip-status "$last_status" "$img" > "$meta_path" 2>>"$REPO_DIR/sync.log"
  local discover_status=$?
  if [ "$discover_status" -ne 0 ]; then
    rm -f "$img" "$meta_path"
    if [ "$discover_status" -eq 10 ]; then
      log "No new schedule post to read (see sync.log). Skipping claude call."
      return 10
    fi
    log "ERROR: could not fetch the latest schedule post (see sync.log)."
    return 1
  fi

  local status_id
  status_id=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["status_id"])' "$meta_path")
  rm -f "$meta_path"
  if [ -z "$status_id" ]; then
    log "ERROR: x_source.py did not report a usable post."
    rm -f "$img"
    return 1
  fi

  log "New schedule post $status_id, extracting Lucene's row via claude (zero tool access)..."
  extract_and_apply polygon "$img" --source polygon
  local status=$?
  rm -f "$img"

  if [ "$status" -ne 0 ] && [ "$status" -ne 10 ] && [ "$status" -ne 11 ]; then
    log "Not recording status $status_id, so this same post is retried next run."
    return $status
  fi

  # Recorded for exit 11 too: "the channel's own thumbnail already covers this
  # week" will not stop being true later, so re-OCRing this post daily until
  # the next one appears would only burn calls.
  echo "$status_id" > "$LAST_X_STATUS_PATH"
  [ "$status" -eq 0 ] && record_applied_week "$REPO_DIR/.last_week_x"
  return $status
}

# Lucene Ch.'s own pinned schedule video thumbnail - the authoritative source.
sync_thumbnail() {
  log "Checking thumbnail ETag (cheap pre-check, no download)..."
  local new_etag last_etag=""
  new_etag=$(fetch_etag "https://i.ytimg.com/vi/${VIDEO_ID}/maxresdefault.jpg")
  [ -z "$new_etag" ] && new_etag=$(fetch_etag "https://i.ytimg.com/vi/${VIDEO_ID}/hqdefault.jpg")
  [ -f "$LAST_ETAG_PATH" ] && last_etag=$(cat "$LAST_ETAG_PATH")

  if [ -n "$new_etag" ] && [ "$new_etag" = "$last_etag" ]; then
    log "Thumbnail ETag unchanged ($new_etag) - YouTube hasn't touched it since last run. Skipping download and claude call."
    return 10
  fi
  [ -z "$new_etag" ] && log "WARN: could not read an ETag from either thumbnail size, falling back to download+hash check."

  log "Downloading schedule thumbnail..."
  if ! curl -sL --fail "https://i.ytimg.com/vi/${VIDEO_ID}/maxresdefault.jpg" -o "$IMG"; then
    log "maxresdefault failed, trying hqdefault..."
    if ! curl -sL --fail "https://i.ytimg.com/vi/${VIDEO_ID}/hqdefault.jpg" -o "$IMG"; then
      log "ERROR: could not download schedule thumbnail (both sizes failed)"
      return 1
    fi
  fi

  if ! file "$IMG" | grep -qi "image"; then
    log "ERROR: downloaded file is not an image"
    rm -f "$IMG"
    return 1
  fi

  # Second gate, on actual bytes: catches the case where the CDN reissued a new
  # ETag (e.g. re-encode/reprocess) without the visible content actually
  # changing. Skipping claude here also avoids feeding it the same image twice,
  # which would risk non-deterministic OCR output on an unchanged thumbnail.
  local new_hash last_hash=""
  new_hash=$(shasum -a 256 "$IMG" | awk '{print $1}')
  [ -f "$LAST_HASH_PATH" ] && last_hash=$(cat "$LAST_HASH_PATH")

  if [ "$new_hash" = "$last_hash" ]; then
    log "Thumbnail bytes unchanged (hash $new_hash) even though the ETag differed - skipping claude call."
    rm -f "$IMG"
    [ -n "$new_etag" ] && echo "$new_etag" > "$LAST_ETAG_PATH"
    return 10
  fi

  log "Thumbnail changed, extracting schedule via claude (zero tool access - Read/Bash/network all disabled)..."
  extract_and_apply thumbnail "$IMG" --source youtube
  local status=$?
  rm -f "$IMG"

  if [ "$status" -ne 0 ] && [ "$status" -ne 10 ]; then
    log "ERROR: extraction failed for this thumbnail (exit $status), leaving the calendar untouched."
    log "Not recording thumbnail ETag/hash, so this same thumbnail is retried next run."
    return $status
  fi

  # Only now, once claude successfully parsed this exact image, do we record it
  # as "seen" - a failed/errored run above deliberately skips this so the same
  # thumbnail gets retried next time instead of being silently skipped forever.
  [ -n "$new_etag" ] && echo "$new_etag" > "$LAST_ETAG_PATH"
  echo "$new_hash" > "$LAST_HASH_PATH"
  [ "$status" -eq 0 ] && record_applied_week "$REPO_DIR/.last_week"
  return $status
}

ensure_pushed "$REPO_DIR"

cd "$REPO_DIR" || { log "ERROR: cannot cd to $REPO_DIR"; exit 1; }
git pull --ff-only origin main || log "WARN: git pull failed, continuing with local state"

# The authoritative source goes first: it sets the week marker that decides
# whether the agency post is still allowed to write the same week at all.
FAILED=0
sync_thumbnail
case $? in
  0|10) ;;
  *) FAILED=1; log "ERROR: the thumbnail source failed; continuing with the agency post." ;;
esac

sync_polygon
case $? in
  0|10|11) ;;
  *) FAILED=1; log "ERROR: the agency post source failed." ;;
esac

if [ ${#APPLIED_WEEKS[@]} -eq 0 ]; then
  log "No schedule changes. Done."
  exit $FAILED
fi

log "New week applied. Committing..."
# One missing pathspec makes git add reject the whole call, so each marker is
# only staged once it exists.
git add public/schedule.ics
for marker in .last_week .last_week_x; do
  [ -f "$REPO_DIR/$marker" ] && git add "$marker"
done
if git diff --cached --quiet; then
  log "WARN: nothing staged (unexpected, sync_apply.py reported a change)."
  exit $FAILED
fi

MESSAGE="Update schedule for week of ${APPLIED_WEEKS[0]}"
for week in "${APPLIED_WEEKS[@]:1}"; do
  MESSAGE="$MESSAGE and week of $week"
done
if ! git commit -m "$MESSAGE"; then
  log "ERROR: git commit failed."
  exit 1
fi
if ! git push origin main; then
  log "ERROR: git push failed. Will retry on next run."
  exit 1
fi
log "Pushed. Cloudflare redeploys public/schedule.ics from this commit."
exit $FAILED
