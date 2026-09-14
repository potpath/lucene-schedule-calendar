# Lucene Ch. Weekly Schedule (private: scripts + state)

Auto-updating `.ics` calendar of [Lucene Ch.](https://www.youtube.com/@LucenePLG)'s (VTuber, Polygon Project) weekly livestream schedule.

Source: the channel's pinned "Weekly Schedule" video, whose thumbnail is edited with the new week's lineup: https://www.youtube.com/watch?v=O4FtQpWRAB8

This repo is **private** and holds the sync scripts, run state (`.last_week`), the hand-maintained title spelling list (`known_titles.txt`), and a working copy of `lucene-schedule.ics` for history/debugging. The published calendar people actually subscribe to lives in the separate **public** repo `potpath/lucene-schedule-ics`, which contains nothing but that one file.

## Subscribe

Add this URL to your calendar app (Google Calendar → Settings → Add calendar → From URL; Apple Calendar → File → New Calendar Subscription; Outlook → Add calendar → Subscribe from web):

```
https://raw.githubusercontent.com/potpath/lucene-schedule-ics/main/lucene-schedule.ics
```

## How it stays updated

`sync.sh` runs daily via a local `launchd` job (`com.lucene.schedule-sync`, ~/Library/LaunchAgents). Before doing anything expensive, it gates on two cheap "did the thumbnail actually change" checks straight from YouTube: first the CDN's `ETag` header on the thumbnail URL (a single HEAD request, no download at all), then, if that differs, a sha256 hash of the downloaded bytes. Only if both say "changed" does it call `claude` at all — this both avoids unnecessary calls and avoids re-OCRing an unchanged image, which could otherwise produce spurious non-deterministic diffs.

When the thumbnail has genuinely changed, it's passed to a **zero-tool** `claude -p` call (`--tools ""`, image supplied inline via stream-json, no Read/Bash/network access at all) that does nothing but OCR the graphic into structured JSON — this keeps any text embedded in the thumbnail from being able to act as a prompt injection, since the model has no tool to act with even if it tried.

Two things guard that OCR's accuracy, because the graphic draws some titles as stylized logo artwork whose small print is only ~12px tall at the thumbnail's native 1280x720. First, `build_input.py` sends the schedule rows a **second time**, cropped out and enlarged, so those pixels get more of the model's attention; without it the week of 14 Sep read `ลุ้นดวงกับเน่` as `ลุ้นดวงกับพี่` on every run, and raising the model's effort didn't help. Second, `known_titles.txt` lists the exact spelling of every title the channel has used before, and recurring titles are copied from that list rather than re-guessed. That file is edited by hand on purpose — it is never generated from the published calendar, because a wrong title fed back in as a hint would keep reproducing itself. It is a strong hint, so if the channel genuinely renames a recurring show, edit or remove its line.

`sync_apply.py` then converts each stream's local time (GMT+7 / Asia/Bangkok) to UTC and reconciles all 7 days of the week against what's already published in `lucene-schedule.ics` (events older than ~14 days are pruned) — so a mid-week time/topic edit, cancellation, or addition gets republished too, not just a brand-new week. A day can carry more than one stream, since the graphic sometimes puts two lives on one row (e.g. `17:00 | 20:30`); each becomes its own event, and if two are scheduled less than the assumed 2h apart the earlier one is trimmed so they don't overlap in your calendar. If nothing actually changed, no commit happens. When it does change, `sync.sh` commits it here (private, for history) and copies the same file into the public repo's working copy, committing and pushing it there too. No action needed once you've subscribed to the public URL above — your calendar app picks up changes on its own refresh interval.
