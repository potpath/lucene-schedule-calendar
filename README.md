# Lucene Ch. Weekly Schedule

Auto-updating `.ics` calendar of [Lucene Ch.](https://www.youtube.com/@PLGLucene)'s (VTuber, Polygon Project) weekly livestream schedule.

Source: the channel's pinned "Weekly Schedule" video, whose thumbnail is edited with the new week's lineup: https://www.youtube.com/watch?v=O4FtQpWRAB8

## Subscribe

Add this URL to your calendar app (Google Calendar → Settings → Add calendar → From URL; Apple Calendar → File → New Calendar Subscription; Outlook → Add calendar → Subscribe from web):

```
https://raw.githubusercontent.com/potpath/lucene-schedule-calendar/main/lucene-schedule.ics
```

## How it stays updated

A scheduled cloud agent checks the schedule video daily. When a new week's schedule image is posted, it reads the graphic, converts each stream's local time (GMT+7 / Asia/Bangkok) to UTC, and commits the new events to `lucene-schedule.ics`. Events older than ~14 days are pruned to keep the file small. No action needed once you've subscribed — your calendar app will pick up changes on its own refresh interval.
