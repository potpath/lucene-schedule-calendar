#!/usr/bin/env python3
"""
Applies a structured schedule extraction (JSON, from `claude -p --json-schema`)
to lucene-schedule.ics and .last_week.

Every run reconciles all 7 days of the extracted week against whatever is
currently published for those same dates - not just brand-new weeks. If a
day's time or title changed, or a day flipped between "stream" and "rest",
that event is added/updated/removed accordingly. Days whose content is
identical to what's already published are left untouched (old DTSTAMP/
SEQUENCE preserved) so the file only actually changes when something real
changed.

Usage: sync_apply.py <path-to-claude-output-json>

Exit codes:
  0  - something changed (new/updated/removed event, or a stale prune) -
       ics/last_week updated
  10 - no changes at all - nothing to do
  1  - error (bad/missing schedule, malformed data, regression, etc.)
"""
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

REPO_DIR = "$HOME/code/youtube/calendar"
ICS_PATH = f"{REPO_DIR}/lucene-schedule.ics"
LAST_WEEK_PATH = f"{REPO_DIR}/.last_week"
SOURCE_VIDEO_URL = "https://www.youtube.com/watch?v=O4FtQpWRAB8"
CHANNEL_LIVE_URL = "https://www.youtube.com/@LucenePLG/live"
BANGKOK = timezone(timedelta(hours=7))
STREAM_DURATION = timedelta(hours=2)
PRUNE_AFTER = timedelta(days=14)
DAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
UID_RE = re.compile(r"^lucene-schedule-(\d{4}-\d{2}-\d{2})@lucenech$")


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_vevent(local_date: str, local_time: str, title: str, dtstamp: str, sequence: int) -> str:
    hh, mm = (int(x) for x in local_time.split(":"))
    y, mo, d = (int(x) for x in local_date.split("-"))
    start_local = datetime(y, mo, d, hh, mm, tzinfo=BANGKOK)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + STREAM_DURATION).astimezone(timezone.utc)

    uid = f"lucene-schedule-{local_date}@lucenech"
    summary = esc(f"[Lucene Ch.] {title}")
    description = esc(
        f"{title}\n"
        f"Local time: {local_date} {local_time} GMT+7 (Asia/Bangkok).\n"
        f"Duration is an estimate (2h), actual stream length may vary.\n"
        f"Schedule source: {SOURCE_VIDEO_URL}"
    )
    return "\n".join([
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"SEQUENCE:{sequence}",
        f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"URL:{CHANNEL_LIVE_URL}",
        "LOCATION:YouTube - Lucene Ch.",
        "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ])


def parse_field(vevent_block: str, field: str):
    m = re.search(rf"^{field}:(.*)$", vevent_block, re.MULTILINE)
    return m.group(1).strip() if m else None


def parse_dtend(vevent_block: str):
    val = parse_field(vevent_block, "DTEND")
    if val is None or not re.match(r"^\d{8}T\d{6}Z$", val):
        return None
    return datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def parse_uid(vevent_block: str):
    return parse_field(vevent_block, "UID")


def parse_sequence(vevent_block: str) -> int:
    val = parse_field(vevent_block, "SEQUENCE")
    return int(val) if val is not None and val.isdigit() else 0


def date_from_uid(uid):
    if not uid:
        return None
    m = UID_RE.match(uid)
    return m.group(1) if m else None


def main():
    if len(sys.argv) != 2:
        print("usage: sync_apply.py <claude-output-json-path>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        envelope = json.load(f)

    if envelope.get("is_error"):
        print(f"ERROR: claude call reported an error: {envelope}", file=sys.stderr)
        sys.exit(1)

    data = envelope.get("structured_output") or json.loads(envelope["result"])

    if not data.get("found_schedule"):
        print(f"ERROR: no schedule found in image. notes={data.get('notes')!r}", file=sys.stderr)
        sys.exit(1)

    week_monday = data.get("week_monday")
    if not week_monday or not re.match(r"^\d{4}-\d{2}-\d{2}$", week_monday):
        print(f"ERROR: bad week_monday value: {week_monday!r}", file=sys.stderr)
        sys.exit(1)

    try:
        week_monday_date = date.fromisoformat(week_monday)
    except ValueError:
        print(f"ERROR: unparseable week_monday value: {week_monday!r}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(LAST_WEEK_PATH, encoding="utf-8") as f:
            last_week = f.read().strip()
    except FileNotFoundError:
        last_week = ""

    # Only reject a week that's strictly BEFORE the one we're already tracking
    # (a stale/cached thumbnail rolling us backwards). The same week as last
    # time is expected and handled below - that's exactly how we detect a
    # mid-week edit (time/topic change, or a stream getting cancelled).
    if last_week:
        try:
            last_week_date = date.fromisoformat(last_week)
        except ValueError:
            print(f"ERROR: .last_week contains an unparseable date: {last_week!r}", file=sys.stderr)
            sys.exit(1)
        if week_monday_date < last_week_date:
            print(
                f"ERROR: extracted week_monday {week_monday} is before the currently "
                f"tracked week {last_week} - refusing to apply. This looks like a stale or "
                f"cached thumbnail rather than a real update. Failing fast without "
                f"touching .last_week or lucene-schedule.ics.",
                file=sys.stderr,
            )
            sys.exit(1)

    days = {d["day"]: d for d in data.get("days", [])}
    missing = [d for d in DAY_ORDER if d not in days]
    if missing:
        print(f"ERROR: missing days in extraction: {missing}", file=sys.stderr)
        sys.exit(1)

    non_rest_days = [d for d in DAY_ORDER if not days[d].get("rest")]
    if not non_rest_days:
        print(f"ERROR: extraction for week {week_monday} has zero non-rest days - suspicious, refusing to apply.", file=sys.stderr)
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    week_dates = {(week_monday_date + timedelta(days=i)).isoformat() for i in range(7)}

    with open(ICS_PATH, encoding="utf-8") as f:
        ics_text = f.read()

    header_end = ics_text.index("BEGIN:VEVENT") if "BEGIN:VEVENT" in ics_text else ics_text.index("END:VCALENDAR")
    header = ics_text[:header_end]

    existing_blocks = re.findall(r"BEGIN:VEVENT.*?END:VEVENT", ics_text, re.DOTALL)

    # Split existing events into "belongs to the week we're reconciling right
    # now" (set aside for the per-day comparison below) vs. "some other week"
    # (kept as history, only ever dropped once genuinely stale).
    existing_by_date = {}
    kept_blocks = []
    pruned = 0
    for block in existing_blocks:
        d = date_from_uid(parse_uid(block))
        if d in week_dates:
            existing_by_date[d] = block
            continue
        dtend = parse_dtend(block)
        if dtend is not None and dtend < now_utc - PRUNE_AFTER:
            pruned += 1
            continue
        kept_blocks.append(block)

    week_blocks = []
    added = updated = removed = 0
    for day in DAY_ORDER:
        row = days[day]
        expected_date = week_monday_date + timedelta(days=DAY_ORDER.index(day))
        expected_date_str = expected_date.isoformat()
        old_block = existing_by_date.get(expected_date_str)

        if row.get("rest"):
            if old_block is not None:
                removed += 1  # was previously scheduled, now marked as rest/cancelled
            continue

        if not (row.get("date") and row.get("time") and row.get("title")):
            print(f"ERROR: non-rest day {day} missing date/time/title: {row}", file=sys.stderr)
            sys.exit(1)

        # The day-of-week position in the graphic (MON..SUN, always a fixed 7-row
        # template) is more trustworthy than the OCR'd/typed date text next to it -
        # the channel occasionally typos the printed date. So the actual date used
        # is always derived from week_monday + weekday offset; the extracted date
        # is only a sanity cross-check (also implicitly a range check, since the
        # derived date is by construction always within the week).
        if row["date"] != expected_date_str:
            print(
                f"WARN: {day} extracted date {row['date']!r} does not match the date "
                f"implied by its position in the week ({expected_date_str}). Using "
                f"{expected_date_str} (day-of-week position is more reliable than the "
                f"source graphic's typed date).",
                file=sys.stderr,
            )

        hh, mm = (int(x) for x in row["time"].split(":"))
        y, mo, d = (int(x) for x in expected_date_str.split("-"))
        start_local = datetime(y, mo, d, hh, mm, tzinfo=BANGKOK)
        start_utc = start_local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end_utc = (start_local + STREAM_DURATION).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        summary = esc(f"[Lucene Ch.] {row['title']}")

        unchanged = (
            old_block is not None
            and parse_field(old_block, "DTSTART") == start_utc
            and parse_field(old_block, "DTEND") == end_utc
            and parse_field(old_block, "SUMMARY") == summary
        )

        if unchanged:
            week_blocks.append(old_block)
        else:
            sequence = parse_sequence(old_block) + 1 if old_block is not None else 0
            week_blocks.append(build_vevent(expected_date_str, row["time"], row["title"], dtstamp, sequence))
            if old_block is not None:
                updated += 1
            else:
                added += 1

    all_blocks = kept_blocks + week_blocks
    if all_blocks:
        new_ics = header.rstrip("\n") + "\n" + "\n".join(all_blocks) + "\nEND:VCALENDAR\n"
    else:
        new_ics = header.rstrip("\n") + "\nEND:VCALENDAR\n"

    if new_ics == ics_text:
        print(f"No changes for week {week_monday} (already in sync).")
        sys.exit(10)

    with open(ICS_PATH, "w", encoding="utf-8") as f:
        f.write(new_ics)

    with open(LAST_WEEK_PATH, "w", encoding="utf-8") as f:
        f.write(week_monday + "\n")

    print(
        f"Synced week {week_monday}: {added} added, {updated} updated, "
        f"{removed} removed (now rest), {pruned} pruned (stale), "
        f"{len(all_blocks)} total event(s)."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
