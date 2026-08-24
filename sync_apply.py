#!/usr/bin/env python3
"""
Applies a structured schedule extraction (JSON, from `claude -p --json-schema`)
to lucene-schedule.ics and .last_week.

A day can hold more than one stream: the graphic sometimes schedules two
lives on the same row (e.g. "17:00 | 20:30"), and each becomes its own event.

Every run reconciles all 7 days of the extracted week against whatever is
currently published for those same dates - not just brand-new weeks. If a
stream's time or title changed, or a day flipped between "stream" and "rest",
or a day gained/lost one of its streams, those events are added/updated/
removed accordingly. Streams whose content is identical to what's already
published are left untouched (old DTSTAMP/SEQUENCE preserved) so the file
only actually changes when something real changed.

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
# Second capture is the slot suffix for a day's 2nd, 3rd, ... stream. It
# starts at 2, so the first stream of every day keeps the original date-only
# UID that single-stream days have always published.
UID_RE = re.compile(r"^lucene-schedule-(\d{4}-\d{2}-\d{2})(?:-([2-9][0-9]*))?@lucenech$")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def uid_for(local_date: str, index: int) -> str:
    """UID of the index-th stream of a day (streams ordered by start time).

    The first stream of a day keeps the plain date-only UID. That's what every
    event published before multi-stream support used, so existing subscribers
    see no churn - only a genuine 2nd/3rd stream introduces a new UID.
    """
    suffix = "" if index == 0 else f"-{index + 1}"
    return f"lucene-schedule-{local_date}{suffix}@lucenech"


def build_vevent(local_date: str, local_time: str, title: str, dtstamp: str,
                 sequence: int, index: int, start_local: datetime, end_local: datetime) -> str:
    duration = end_local - start_local
    if duration == STREAM_DURATION:
        duration_note = "Duration is an estimate (2h), actual stream length may vary."
    else:
        duration_note = (
            f"Duration is an estimate ({duration.total_seconds() / 3600:g}h, shortened so it "
            f"does not overlap the next stream that day), actual stream length may vary."
        )

    summary = esc(f"[Lucene Ch.] {title}")
    description = esc(
        f"{title}\n"
        f"Local time: {local_date} {local_time} GMT+7 (Asia/Bangkok).\n"
        f"{duration_note}\n"
        f"Schedule source: {SOURCE_VIDEO_URL}"
    )
    return "\n".join([
        "BEGIN:VEVENT",
        f"UID:{uid_for(local_date, index)}",
        f"DTSTAMP:{dtstamp}",
        f"SEQUENCE:{sequence}",
        f"DTSTART:{utc_stamp(start_local)}",
        f"DTEND:{utc_stamp(end_local)}",
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


def parse_uid_key(uid):
    """(date, slot index) an existing event occupies, or (None, None)."""
    if not uid:
        return None, None
    m = UID_RE.match(uid)
    if not m:
        return None, None
    return m.group(1), (int(m.group(2)) - 1 if m.group(2) else 0)


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

    # Normalise every day into a list of (time, title), earliest first. The
    # order matters beyond tidiness: a stream's slot index is what its UID is
    # built from, so sorting by start time keeps UIDs stable when the same week
    # gets re-extracted. Validate here rather than mid-write, so a malformed
    # extraction fails before lucene-schedule.ics has been touched.
    streams_by_day = {}
    for day in DAY_ORDER:
        row = days[day]
        streams = row.get("streams") or []

        if row.get("rest"):
            if streams:
                print(
                    f"ERROR: day {day} is marked rest but also lists {len(streams)} stream(s) - "
                    f"contradictory extraction, refusing to guess: {row}",
                    file=sys.stderr,
                )
                sys.exit(1)
            streams_by_day[day] = []
            continue

        if not streams:
            print(f"ERROR: non-rest day {day} lists no streams: {row}", file=sys.stderr)
            sys.exit(1)

        parsed = []
        for stream in streams:
            time_str = stream.get("time")
            title = stream.get("title")
            if not time_str or not TIME_RE.match(time_str):
                print(
                    f"ERROR: day {day} has a stream with an unusable time {time_str!r} "
                    f"(want a single 24h HH:MM): {stream}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not title:
                print(f"ERROR: day {day} has a stream with no title: {stream}", file=sys.stderr)
                sys.exit(1)
            parsed.append((time_str, title))

        times = [t for t, _ in parsed]
        if len(set(times)) != len(times):
            print(f"ERROR: day {day} lists the same start time more than once: {times}", file=sys.stderr)
            sys.exit(1)

        streams_by_day[day] = sorted(parsed)

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
    existing_by_slot = {}
    kept_blocks = []
    pruned = 0
    for block in existing_blocks:
        block_date, block_index = parse_uid_key(parse_uid(block))
        if block_date in week_dates:
            existing_by_slot[(block_date, block_index)] = block
            continue
        dtend = parse_dtend(block)
        if dtend is not None and dtend < now_utc - PRUNE_AFTER:
            pruned += 1
            continue
        kept_blocks.append(block)

    week_blocks = []
    added = updated = removed = 0
    multi_stream_days = 0
    for day in DAY_ORDER:
        row = days[day]
        expected_date = week_monday_date + timedelta(days=DAY_ORDER.index(day))
        expected_date_str = expected_date.isoformat()
        streams = streams_by_day[day]
        if len(streams) > 1:
            multi_stream_days += 1

        # Whatever is already published for this date in a slot the new
        # extraction no longer fills is gone: either the day flipped to REST, or
        # it dropped one of several streams. Not re-emitting the block is what
        # actually removes it; this only counts it for the summary line.
        removed += sum(
            1 for (block_date, block_index) in existing_by_slot
            if block_date == expected_date_str and block_index >= len(streams)
        )

        if not streams:
            continue

        # The day-of-week position in the graphic (MON..SUN, always a fixed 7-row
        # template) is more trustworthy than the OCR'd/typed date text next to it -
        # the channel occasionally typos the printed date. So the actual date used
        # is always derived from week_monday + weekday offset; the extracted date
        # is only a sanity cross-check (also implicitly a range check, since the
        # derived date is by construction always within the week).
        if row.get("date") != expected_date_str:
            print(
                f"WARN: {day} extracted date {row.get('date')!r} does not match the date "
                f"implied by its position in the week ({expected_date_str}). Using "
                f"{expected_date_str} (day-of-week position is more reliable than the "
                f"source graphic's typed date).",
                file=sys.stderr,
            )

        year, month, dom = (int(x) for x in expected_date_str.split("-"))
        starts = []
        for time_str, _title in streams:
            hh, mm = (int(x) for x in time_str.split(":"))
            starts.append(datetime(year, month, dom, hh, mm, tzinfo=BANGKOK))

        for index, (time_str, title) in enumerate(streams):
            start_local = starts[index]
            end_local = start_local + STREAM_DURATION
            # Two lives on one day can be scheduled closer together than the
            # assumed 2h duration; end the earlier one where the next starts so
            # they don't sit on top of each other in subscribers' calendars.
            if index + 1 < len(starts) and starts[index + 1] < end_local:
                end_local = starts[index + 1]

            summary = esc(f"[Lucene Ch.] {title}")
            old_block = existing_by_slot.get((expected_date_str, index))

            unchanged = (
                old_block is not None
                and parse_field(old_block, "DTSTART") == utc_stamp(start_local)
                and parse_field(old_block, "DTEND") == utc_stamp(end_local)
                and parse_field(old_block, "SUMMARY") == summary
            )

            if unchanged:
                week_blocks.append(old_block)
            else:
                sequence = parse_sequence(old_block) + 1 if old_block is not None else 0
                week_blocks.append(build_vevent(
                    expected_date_str, time_str, title, dtstamp, sequence, index,
                    start_local, end_local,
                ))
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
        f"{removed} removed (rest or dropped), {pruned} pruned (stale), "
        f"{multi_stream_days} day(s) with more than one stream, "
        f"{len(all_blocks)} total event(s)."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
