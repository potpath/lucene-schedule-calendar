# Lucene Ch. Weekly Schedule (private: scripts + state)

Auto-updating `.ics` calendar of [Lucene Ch.](https://www.youtube.com/@LucenePLG)'s (VTuber, Polygon Project) weekly livestream schedule.

Source: the channel's pinned "Weekly Schedule" video, whose thumbnail is edited with the new week's lineup: https://www.youtube.com/watch?v=O4FtQpWRAB8

This repo is **private** and holds the sync scripts, run state (`.last_week`), and a working copy of `lucene-schedule.ics` for history/debugging. The published calendar people actually subscribe to lives in the separate **public** repo `potpath/lucene-schedule-ics`, which contains nothing but that one file.

## Subscribe

Add this URL to your calendar app (Google Calendar → Settings → Add calendar → From URL; Apple Calendar → File → New Calendar Subscription; Outlook → Add calendar → Subscribe from web):

```
https://raw.githubusercontent.com/potpath/lucene-schedule-ics/main/lucene-schedule.ics
```

## How it stays updated

`sync.sh` runs daily via a local `launchd` job (`com.lucene.schedule-sync`, ~/Library/LaunchAgents). It downloads the current schedule thumbnail and passes it to a **zero-tool** `claude -p` call (`--tools ""`, image supplied inline via stream-json, no Read/Bash/network access at all) that does nothing but OCR the graphic into structured JSON — this keeps any text embedded in the thumbnail from being able to act as a prompt injection, since the model has no tool to act with even if it tried.

`sync_apply.py` then converts each stream's local time (GMT+7 / Asia/Bangkok) to UTC and updates `lucene-schedule.ics` (events older than ~14 days are pruned). If nothing changed since the last run, no commit happens. When it does change, `sync.sh` commits it here (private, for history) and copies the same file into the public repo's working copy, committing and pushing it there too. No action needed once you've subscribed to the public URL above — your calendar app picks up changes on its own refresh interval.
