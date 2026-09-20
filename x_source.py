#!/usr/bin/env python3
"""
Finds Polygon Project's latest weekly schedule post on X and downloads its graphic.

Polygon Project is Lucene Ch.'s agency. Every Sunday it posts one graphic
covering the week starting the next day for *all* its members, a day or two
before Lucene's own channel refreshes its thumbnail - so this is the early
source, and the channel's own thumbnail stays the authoritative one (see
sync_apply.py's precedence rule).

Finding "the newest post" is the hard part, because X serves logged-out clients
a ranked "best highlights" timeline instead of a chronological one - both
through its own guest GraphQL API and through the syndication endpoint that
powers embedded timelines. Verified on 2026-09-20: both return the same 100
curated posts, none of them a weekly schedule post. So discovery goes through a
Nitter mirror's RSS feed, which is chronological.

A third-party mirror is not something to trust with the content, though, so it
is used for one thing only: to learn a numeric status id. Everything that is
actually used - the post's author, its text and the image itself - is then
re-fetched from X's own syndication endpoint, which needs no auth and does
answer reliably for a known id. A mirror that lied could at worst point this at
a different Polygon Project post, and the author/marker checks below reject
anything that is not one of theirs.

The addresses of all of that - the mirrors to try and the two X endpoints - are
NOT in this file. This repository and the calendar it publishes are public, and
the endpoints a scraper reads are the one part of it that should not be. They
live in local/sources.json, which git ignores; see CONFIG_PATH below. Without
that file this source simply reports that it is not configured and the rest of
the pipeline carries on with the channel's own thumbnail.

Usage: x_source.py [--skip-status <id>] <output-image-path>

--skip-status is the id this pipeline handled last. If the newest schedule post
is still that one, the run stops at exit 10 before downloading anything, which
is the normal outcome six days a week.

Prints one JSON object describing the post on success. Exit codes:
  0  - found a new schedule post, image written to <output-image-path>
  10 - nothing to do: a mirror answered, but it holds no schedule post newer
       than --skip-status
  1  - error, including "no mirror answered at all". That is deliberately not
       a quiet 10: a dead mirror list is the way this source fails for good,
       and it would otherwise look exactly like a week with no new post.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

ACCOUNT = "Polygon_Project"
# Git-ignored, and deliberately so - see the module docstring. Expected keys:
#   rss_bases  list of Nitter-style mirror roots, tried in order, each used as
#              "<base>/<account>/rss"
#   post_api   URL template taking {id}, returning X's own JSON for one post
#   post_url   URL template taking {account} and {id}, the human permalink;
#              used for local logging only and never written to the .ics
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local", "sources.json")
# Every one of these posts opens with this phrase ("weekly live schedule"), and
# nothing else the account posts does. Matching on it rather than on "newest
# post with a picture" is what keeps an announcement or a birthday graphic from
# being fed to the extractor.
SCHEDULE_MARKER = "ตารางไลฟ์ประจำสัปดาห์"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
STATUS_RE = re.compile(rf"/{ACCOUNT}/status/(\d{{1,25}})", re.IGNORECASE)
TIMEOUT = 30


def log(msg):
    print(msg, file=sys.stderr)


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read()
    return body if binary else body.decode("utf-8", "replace")


def load_config():
    """Endpoints from local/sources.json, or None when it isn't there.

    Mirrors come and go, so rss_bases is meant to be edited; setting
    POLYGON_X_RSS_BASES (comma-separated) overrides it for one run.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        log(f"ERROR: {CONFIG_PATH} is not valid JSON: {exc}")
        return None

    override = os.environ.get("POLYGON_X_RSS_BASES", "").strip()
    if override:
        config["rss_bases"] = [b.strip() for b in override.split(",") if b.strip()]
    config["rss_bases"] = [b.rstrip("/") for b in config.get("rss_bases") or []]

    missing = [k for k in ("rss_bases", "post_api") if not config.get(k)]
    if missing:
        log(f"ERROR: {CONFIG_PATH} is missing {', '.join(missing)}.")
        return None
    return config


def candidates_from_feed(xml_text):
    """Schedule posts in one mirror's feed, newest first: [(datetime, status_id)].

    Only the status id is taken from here. Retweets are excluded by requiring
    the link to point at this account's own status path.
    """
    root = ET.fromstring(xml_text)
    found = []
    for item in root.findall(".//item"):
        text = " ".join(filter(None, (item.findtext("title"), item.findtext("description"))))
        if SCHEDULE_MARKER not in text:
            continue
        match = STATUS_RE.search(item.findtext("link") or "")
        if not match:
            continue
        try:
            when = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        found.append((when, match.group(1)))
    found.sort(reverse=True)
    return found


def discover_status_id(config):
    """(status id or None, whether any mirror served a readable feed)."""
    reachable = False
    for base in config["rss_bases"]:
        url = f"{base}/{ACCOUNT}/rss"
        try:
            found = candidates_from_feed(fetch(url))
        except (urllib.error.URLError, ET.ParseError, OSError) as exc:
            log(f"WARN: {base} did not serve a usable feed ({exc}); trying the next mirror.")
            continue
        reachable = True
        if not found:
            log(f"WARN: {base} answered but its feed holds no schedule post; trying the next mirror.")
            continue
        when, status_id = found[0]
        log(f"Newest schedule post per {base}: {status_id} ({when.isoformat()}).")
        return status_id, True
    return None, reachable


def fetch_post(config, status_id):
    """The post as X itself reports it, or None if it fails any identity check."""
    try:
        post = json.loads(fetch(config["post_api"].format(id=status_id)))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        log(f"ERROR: could not read status {status_id} back from X: {exc}")
        return None

    screen_name = (post.get("user") or {}).get("screen_name") or ""
    if screen_name.lower() != ACCOUNT.lower():
        log(f"ERROR: status {status_id} belongs to @{screen_name}, not @{ACCOUNT}. Ignoring it.")
        return None

    text = post.get("text") or ""
    if SCHEDULE_MARKER not in text:
        log(f"ERROR: status {status_id} is not a weekly schedule post. Ignoring it.")
        return None

    photos = post.get("photos") or []
    if not photos or not photos[0].get("url"):
        log(f"ERROR: status {status_id} carries no image.")
        return None

    return post


def parse_args(argv):
    skip_status, rest = None, []
    i = 0
    while i < len(argv):
        if argv[i] == "--skip-status" and i + 1 < len(argv):
            skip_status, i = argv[i + 1], i + 2
        else:
            rest.append(argv[i])
            i += 1
    if len(rest) != 1:
        print("usage: x_source.py [--skip-status <id>] <output-image-path>", file=sys.stderr)
        sys.exit(1)
    return skip_status, rest[0]


def main():
    skip_status, out_path = parse_args(sys.argv[1:])

    config = load_config()
    if config is None:
        log(f"This source is not configured: {CONFIG_PATH} is absent or unusable, so "
            f"there are no endpoints to read. Skipping it.")
        sys.exit(10)

    status_id, reachable = discover_status_id(config)
    if status_id is None:
        if not reachable:
            log(f"ERROR: none of the {len(config['rss_bases'])} configured mirror(s) "
                f"served a readable feed. Edit rss_bases in {CONFIG_PATH} if they have "
                f"gone for good.")
            sys.exit(1)
        log("A mirror answered but listed no schedule post. Nothing to do this run.")
        sys.exit(10)

    if skip_status and status_id == skip_status:
        log(f"Status {status_id} is the one handled last - no new schedule post. "
            f"Skipping the download and the claude call.")
        sys.exit(10)

    post = fetch_post(config, status_id)
    if post is None:
        sys.exit(1)

    # format=jpg pins the encoding so build_input.py's media_type is always
    # right; name=orig asks for the upload's full resolution rather than the
    # 1200px-wide copy the timeline shows.
    image_url = post["photos"][0]["url"] + "?format=jpg&name=orig"
    try:
        image = fetch(image_url, binary=True)
    except (urllib.error.URLError, OSError) as exc:
        log(f"ERROR: could not download {image_url}: {exc}")
        sys.exit(1)
    if not image.startswith(b"\xff\xd8"):
        log(f"ERROR: {image_url} did not return a JPEG.")
        sys.exit(1)
    with open(out_path, "wb") as f:
        f.write(image)

    # post_url is for the local log only. It is deliberately not handed to
    # sync_apply.py: the .ics it writes is published, and a permalink there
    # would put the source's address in a public file.
    permalink = (config.get("post_url") or "").format(account=ACCOUNT, id=status_id)
    print(json.dumps({
        "status_id": status_id,
        "created_at": post.get("created_at"),
        "permalink": permalink,
        "image_url": image_url,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
