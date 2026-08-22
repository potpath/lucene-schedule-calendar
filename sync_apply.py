#!/usr/bin/env python3
"""
Applies a structured schedule extraction (JSON, from `claude -p --json-schema`)
to lucene-schedule.ics and .last_week.

Usage: sync_apply.py <path-to-claude-output-json>

Exit codes:
  0  - new week applied, ics/last_week updated (caller should git commit+push)
  10 - no new week (last_week already matches) - nothing to do
  1  - error (bad/missing schedule, malformed data, etc.)
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


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_vevent(local_date: str, local_time: str, title: str, dtstamp: str) -> str:
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


def parse_dtend(vevent_block: str):
    m = re.search(r"^DTEND:(\d{8}T\d{6}Z)$", vevent_block, re.MULTILINE)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def parse_uid(vevent_block: str):
    m = re.search(r"^UID:(.+)$", vevent_block, re.MULTILINE)
    return m.group(1).strip() if m else None


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

    if week_monday == last_week:
        print(f"No new week (already synced {week_monday}).")
        sys.exit(10)

    if last_week:
        try:
            last_week_date = date.fromisoformat(last_week)
        except ValueError:
            print(f"ERROR: .last_week contains an unparseable date: {last_week!r}", file=sys.stderr)
            sys.exit(1)
        if week_monday_date <= last_week_date:
            print(
                f"ERROR: extracted week_monday {week_monday} is not after the currently "
                f"tracked week {last_week} - refusing to apply. This looks like a stale or "
                f"cached thumbnail rather than a real new week. Failing fast without "
                f"touching .last_week or lucene-schedule.ics.",
                file=sys.stderr,
            )
            sys.exit(1)

    days = {d["day"]: d for d in data.get("days", [])}
    missing = [d for d in DAY_ORDER if d not in days]
    if missing:
        print(f"ERROR: missing days in extraction: {missing}", file=sys.stderr)
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")

    new_events = []
    for day in DAY_ORDER:
        row = days[day]
        if row.get("rest"):
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
        expected_date = week_monday_date + timedelta(days=DAY_ORDER.index(day))
        expected_date_str = expected_date.isoformat()
        if row["date"] != expected_date_str:
            print(
                f"WARN: {day} extracted date {row['date']!r} does not match the date "
                f"implied by its position in the week ({expected_date_str}). Using "
                f"{expected_date_str} (day-of-week position is more reliable than the "
                f"source graphic's typed date).",
                file=sys.stderr,
            )

        uid = f"lucene-schedule-{expected_date_str}@lucenech"
        new_events.append((uid, build_vevent(expected_date_str, row["time"], row["title"], dtstamp)))

    if not new_events:
        print(f"ERROR: extraction for week {week_monday} has zero non-rest days - suspicious, refusing to apply.", file=sys.stderr)
        sys.exit(1)

    with open(ICS_PATH, encoding="utf-8") as f:
        ics_text = f.read()

    header_end = ics_text.index("BEGIN:VEVENT") if "BEGIN:VEVENT" in ics_text else ics_text.index("END:VCALENDAR")
    header = ics_text[:header_end]

    new_uids = {uid for uid, _ in new_events}

    existing_blocks = re.findall(r"BEGIN:VEVENT.*?END:VEVENT", ics_text, re.DOTALL)
    kept_blocks = []
    for block in existing_blocks:
        dtend = parse_dtend(block)
        stale = dtend is not None and dtend < now_utc - PRUNE_AFTER
        superseded = parse_uid(block) in new_uids
        if not stale and not superseded:
            kept_blocks.append(block)

    all_blocks = kept_blocks + [vevent for _, vevent in new_events]
    new_ics = header.rstrip("\n") + "\n" + "\n".join(all_blocks) + "\nEND:VCALENDAR\n"

    with open(ICS_PATH, "w", encoding="utf-8") as f:
        f.write(new_ics)

    with open(LAST_WEEK_PATH, "w", encoding="utf-8") as f:
        f.write(week_monday + "\n")

    print(f"Applied new week {week_monday}: {len(new_events)} event(s) added, "
          f"{len(existing_blocks) - len(kept_blocks)} stale event(s) pruned, "
          f"{len(all_blocks)} total.")
    sys.exit(0)


if __name__ == "__main__":
    main()
