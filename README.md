# Lucene Ch. Weekly Schedule

Auto-updating `.ics` calendar of [Lucene Ch.](https://www.youtube.com/@LucenePLG)'s (VTuber, Polygon Project) weekly livestream schedule.

Two sources feed it, and they are not equal:

- **The channel's pinned "Weekly Schedule" video**, whose thumbnail is edited with the new week's lineup: https://www.youtube.com/watch?v=O4FtQpWRAB8. This is the authoritative one — it is Lucene's own calendar, and it is what gets edited when a stream moves, gains a topic, or is cancelled mid-week.
- **Polygon Project's weekly post.** Polygon Project is Lucene's agency; every Sunday it posts one graphic covering the week starting the next day for *all* of its members, and Lucene is one row of that grid. The channel's own thumbnail usually follows by Tuesday, so this source is not a substitute for it — it buys a day or two of lead time on each new week.

Where they overlap, the channel's own thumbnail wins: the agency post may only write a week the thumbnail has not published yet. Once a week is the thumbnail's, a later agency post for that same week is out of date rather than newer, and is ignored. Every event records which graphic it came from in an `X-SCHEDULE-SOURCE` property and in its description.

**The addresses the agency-post source reads are not in this repository.** The code is all here and explained below, but the mirror roots and API endpoints it fetches live in `local/sources.json`, which is git-ignored — this repo and the `.ics` it serves are public, and the endpoints a scraper reads are the part of it that should not be. Without that file the source reports itself unconfigured and the pipeline just runs the thumbnail, so a fresh clone works out of the box. Nothing in `public/schedule.ics` links to it either: agency-sourced events name the source without linking it.

This repo both holds the machinery and publishes the calendar, because `public/` is the asset root of a Cloudflare Worker (`wrangler.jsonc`) deployed on every push. `wrangler deploy` uploads *only* what is inside `public/`, so the sync scripts, run state (`.last_week`, `.last_week_x`) and the hand-maintained title spelling list (`known_titles.txt`) sitting alongside it are cloned by the builder and never served. `public/schedule.ics` is therefore both the working copy and the published file; there is no second repo and no mirroring step.

Treat `public/` as a publish directory rather than a scratch space: anything added there becomes a public URL.

The repo itself is public, so nothing here — code, comments, commit messages, run state — is private either. Keep credentials out of it entirely: `sync.sh` authenticates through the locally installed `claude` CLI and stores no key.

## Subscribe

Add this URL to your calendar app (Google Calendar → Settings → Add calendar → From URL; Apple Calendar → File → New Calendar Subscription; Outlook → Add calendar → Subscribe from web):

```
https://lucene.hana-momo.workers.dev/schedule.ics
```

(Served by the `lucene` Worker from this repo's `public/`. Pointing a custom domain at that Worker and publishing *that* URL instead would mean subscribers never have to re-add the calendar again if the host ever changes — the one thing a URL migration cannot be done gracefully for.)

## How it stays updated

`sync.sh` runs daily via a local `launchd` job (`com.lucene.schedule-sync`, ~/Library/LaunchAgents) and checks both sources every run, cheaply, before doing anything expensive.

For the **thumbnail**, it gates on two "did it actually change" checks straight from YouTube: first the CDN's `ETag` header on the thumbnail URL (a single HEAD request, no download at all), then, if that differs, a sha256 hash of the downloaded bytes. Only if both say "changed" does it call `claude` at all — this both avoids unnecessary calls and avoids re-OCRing an unchanged image, which could otherwise produce spurious non-deterministic diffs.

For the **agency post**, `x_source.py` finds the newest post whose text carries the weekly-schedule marker and stops at its status id if that is the one already handled — which it is six days a week, so nothing gets downloaded and `claude` is not called. Finding that post is the awkward part, because X serves logged-out clients a ranked "best highlights" timeline rather than a chronological one, through both its guest API and the syndication endpoint behind embedded timelines (checked on 2026-09-20: both return the same 100 curated posts, not one of them a schedule post). So discovery goes through a Nitter mirror's RSS feed, which is chronological. A third-party mirror is not trusted with the content, though: all it supplies is a numeric status id, and the post's author, text and image are then re-fetched from X's own public syndication endpoint, which does answer reliably for a known id. A mirror that lied could at worst point this at some other Polygon Project post, which the author and marker checks then reject. Mirrors come and go, so `rss_bases` in `local/sources.json` is meant to be edited, and `POLYGON_X_RSS_BASES` overrides it for a single run.

When a graphic has genuinely changed, it's passed to a **zero-tool** `claude -p` call (`--tools ""`, image supplied inline via stream-json, no Read/Bash/network access at all) that does nothing but OCR the graphic into structured JSON — this keeps any text embedded in the thumbnail from being able to act as a prompt injection, since the model has no tool to act with even if it tried.

The two graphics are shaped differently — the thumbnail is seven rows, one per weekday, for this one channel; the agency post is a grid of every member against seven day columns — so `build_input.py --source` picks the prompt, and only Lucene's row is read out of the agency grid.

Two things guard that OCR's accuracy, because both graphics draw some titles as stylized logo artwork. On the thumbnail that print is only ~12px tall at its native 1280x720, so `build_input.py` sends the schedule rows a **second time**, cropped out and enlarged, and those pixels get more of the model's attention; without it the week of 14 Sep read `ลุ้นดวงกับเน่` as `ลุ้นดวงกับพี่` on every run, and raising the model's effort didn't help. The agency post needs no crop — at 1920x2400 its text survives the downscale, measured over 16 runs that agreed unanimously on every time, date, rest day and Thai title — and it deliberately gets none, because a fixed crop box would have to assume where Lucene's row sits, and the agency adding or dropping a member moves every row.

Second, `known_titles.txt` lists the exact spelling of every title the channel has used before, and recurring titles are copied from that list rather than re-guessed. Entries match case-insensitively and their own capitalisation wins, which is also what keeps the two sources from spelling one show two ways: the agency grid draws an all-caps `MEMBERSHIP` badge where the thumbnail writes `Membership`. That file is edited by hand on purpose — it is never generated from the published calendar, because a wrong title fed back in as a hint would keep reproducing itself. It is a strong hint, so if the channel genuinely renames a recurring show, edit or remove its line.

`sync_apply.py` then applies the precedence rule above, converts each stream's local time (GMT+7 / Asia/Bangkok) to UTC, and reconciles all 7 days of the extracted week against what's already published in `public/schedule.ics` (events older than ~14 days are pruned) — so a mid-week time/topic edit, cancellation, or addition gets republished too, not just a brand-new week. A day can carry more than one stream, since both graphics sometimes schedule two lives on one day (the thumbnail marks it with a `|` in the row, the agency grid splits the cell with a divider line); each becomes its own event, and if two are scheduled less than the assumed 2h apart the earlier one is trimmed so they don't overlap in your calendar. Each source keeps its own week marker (`.last_week`, `.last_week_x`), because between the Sunday post and the thumbnail catching up they legitimately name different weeks, and neither should read the other's progress as going backwards. If nothing actually changed, no commit happens. When either source does change something, `sync.sh` commits both sources' work together and pushes it, and Cloudflare redeploys `public/` from that commit — the push is the publish. No action needed once you've subscribed to the URL above — your calendar app picks up changes on its own refresh interval.
