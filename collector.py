#!/usr/bin/env python3
"""
RADIO ARKADY public archive collector — free accumulator edition.

What this version does:
- Uses only the query parameters documented by FxEmbed:
  q, feed, count and cursor.
- Searches every RADIO ARKADY hashtag in all three feeds:
  latest, top and media.
- Follows available pagination cursors.
- Saves every successful page immediately.
- Preserves all posts already stored in archive.json.
- Removes duplicates by post ID.
- Can be run repeatedly; the archive grows whenever the public search
  source returns posts not seen before.

Important limitation:
This is a best-effort free public archive. FxTwitter does not document a
full historical date-range search. Deleted, private, unavailable and
non-indexed posts may be missing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "https://api.fxtwitter.com/2/search"

HASHTAGS = [
    "RADIO_ARKADY",
    "ASK_ARKADY",
    "NAI_POIOS_EINAI",
    "ARKADIOS_ARKADYO",
]

FEEDS = ["latest", "top", "media"]

ARCHIVE_FILE = Path("archive.json")
REPORT_FILE = Path("collection-report.json")

PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.6
MAX_RETRIES = 4

USER_AGENT = (
    "Mozilla/5.0 (compatible; RadioArkadyArchive/3.0; "
    "+https://github.com/)"
)

HASHTAG_RE = re.compile(
    r"(?<!\w)#([A-Za-z0-9_Α-Ωα-ωΆ-ώ]+)",
    re.UNICODE,
)


class NoMoreResults(Exception):
    """The public search returned 404 or no further timeline results."""


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Could not read {path}: {exc}", file=sys.stderr)
        return default


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temporary.replace(path)


def request_json(params: dict[str, str | int]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{API_URL}?{query}"

    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
                status = response.status

            code = int(payload.get("code", status))

            if code == 404:
                raise NoMoreResults(
                    payload.get("message")
                    or "No more available results."
                )

            if code != 200:
                raise RuntimeError(
                    f"API returned code {code}: "
                    f"{payload.get('message', '')}"
                )

            return payload

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NoMoreResults(
                    "The search source returned 404."
                ) from exc

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"HTTP request failed after {attempt} attempts: {exc}"
                ) from exc

            wait = min(30.0, (2 ** attempt) + random.random())
            print(
                f"    Temporary HTTP error "
                f"({attempt}/{MAX_RETRIES}): {exc}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"Request failed after {attempt} attempts: {exc}"
                ) from exc

            wait = min(30.0, (2 ** attempt) + random.random())
            print(
                f"    Temporary request error "
                f"({attempt}/{MAX_RETRIES}): {exc}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)

    raise RuntimeError("Unreachable request state")


def raw_media_items(post: dict[str, Any]) -> list[dict[str, Any]]:
    media = post.get("media")

    if not isinstance(media, dict):
        return []

    combined = media.get("all")

    if isinstance(combined, list):
        return [
            item for item in combined
            if isinstance(item, dict)
        ]

    items: list[dict[str, Any]] = []

    for key in ("photos", "videos"):
        values = media.get(key)

        if isinstance(values, list):
            items.extend(
                item for item in values
                if isinstance(item, dict)
            )

    return items


def normalize_media(item: dict[str, Any]) -> dict[str, Any]:
    formats = item.get("formats")
    normalized_formats: list[dict[str, Any]] = []

    if isinstance(formats, list):
        for variant in formats:
            if not isinstance(variant, dict):
                continue

            normalized_formats.append(
                {
                    key: variant.get(key)
                    for key in (
                        "container",
                        "codec",
                        "bitrate",
                        "url",
                        "size",
                        "height",
                        "width",
                    )
                    if variant.get(key) is not None
                }
            )

    return {
        key: value
        for key, value in {
            "id": item.get("id"),
            "type": item.get("type"),
            "format": item.get("format"),
            "url": item.get("url"),
            "thumbnail_url": item.get("thumbnail_url"),
            "transcode_url": item.get("transcode_url"),
            "width": item.get("width"),
            "height": item.get("height"),
            "duration": item.get("duration"),
            "filesize": item.get("filesize"),
            "alt_text": item.get("altText"),
            "formats": normalized_formats,
        }.items()
        if value not in (None, [], "")
    }


def detect_types(post: dict[str, Any]) -> list[str]:
    types: set[str] = set()

    for item in raw_media_items(post):
        item_type = str(item.get("type", "")).lower()

        if item_type == "photo":
            types.add("IMAGE")
        elif item_type == "video":
            types.add("VIDEO")
        elif item_type == "gif":
            types.add("GIF")

    if not types:
        types.add("TEXT")

    return sorted(types)


def normalize_post(
    post: dict[str, Any],
    searched_tag: str,
    feed: str,
    query_variant: str,
) -> dict[str, Any] | None:
    post_id = str(post.get("id", "")).strip()

    if not post_id:
        return None

    author = post.get("author")

    if not isinstance(author, dict):
        author = {}

    text = str(post.get("text", ""))

    found_hashtags = sorted(
        {match.upper() for match in HASHTAG_RE.findall(text)}
    )

    media = [
        normalize_media(item)
        for item in raw_media_items(post)
    ]
    media = [item for item in media if item]

    username = author.get("screen_name")

    return {
        "id": post_id,
        "url": post.get("url") or (
            f"https://x.com/{username}/status/{post_id}"
            if username
            else f"https://x.com/i/status/{post_id}"
        ),
        "text": text,
        "created_at": post.get("created_at"),
        "created_timestamp": post.get("created_timestamp"),
        "author": {
            "id": author.get("id"),
            "name": author.get("name"),
            "screen_name": username,
            "avatar_url": author.get("avatar_url"),
        },
        "likes": post.get("likes", 0),
        "reposts": post.get("reposts", 0),
        "quotes": post.get("quotes", 0),
        "replies": post.get("replies", 0),
        "hashtags": found_hashtags,
        "matched_queries": [searched_tag],
        "matched_feeds": [feed],
        "matched_query_variants": [query_variant],
        "types": detect_types(post),
        "media": media,
        "collected_at": utc_now_iso(),
    }


def merge_post(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    if previous is None:
        return current

    merged = dict(previous)
    merged.update(current)

    for key in (
        "matched_queries",
        "matched_feeds",
        "matched_query_variants",
        "hashtags",
    ):
        old_values = previous.get(key, [])
        new_values = current.get(key, [])

        if not isinstance(old_values, list):
            old_values = []

        if not isinstance(new_values, list):
            new_values = []

        merged[key] = sorted(
            {
                str(value)
                for value in [*old_values, *new_values]
                if value
            }
        )

    return merged


def timestamp_value(post: dict[str, Any]) -> float:
    try:
        return float(post.get("created_timestamp"))
    except (TypeError, ValueError):
        return 0.0


def archive_list(
    posts_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        posts_by_id.values(),
        key=timestamp_value,
        reverse=True,
    )


def save_archive(
    posts_by_id: dict[str, dict[str, Any]],
) -> None:
    write_json(ARCHIVE_FILE, archive_list(posts_by_id))


def count_types(
    archive: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {
        "ALL": len(archive),
        "GIF": 0,
        "IMAGE": 0,
        "TEXT": 0,
        "VIDEO": 0,
    }

    for post in archive:
        post_types = post.get("types", [])

        if not isinstance(post_types, list):
            continue

        for content_type in ("GIF", "IMAGE", "TEXT", "VIDEO"):
            if content_type in post_types:
                counts[content_type] += 1

    return counts


def collect_query(
    tag: str,
    query_variant: str,
    feed: str,
    max_pages: int,
    posts_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pages_completed = 0
    raw_posts_received = 0
    unique_added = 0
    cursor: str | None = None
    seen_cursors: set[str] = set()
    stopped_by_404 = False
    error: str | None = None

    print(f"\n  Query {query_variant!r} | feed={feed}")

    for page_number in range(1, max_pages + 1):
        params: dict[str, str | int] = {
            "q": query_variant,
            "feed": feed,
            "count": PAGE_SIZE,
        }

        if cursor:
            params["cursor"] = cursor

        try:
            payload = request_json(params)
        except NoMoreResults:
            stopped_by_404 = True
            print("    End of available results.")
            break
        except RuntimeError as exc:
            error = str(exc)
            print(f"    WARNING: {error}")
            break

        results = payload.get("results")

        if not isinstance(results, list):
            results = []

        pages_completed += 1
        raw_posts_received += len(results)

        print(
            f"    Page {page_number}/{max_pages}: "
            f"{len(results)} results"
        )

        page_changed = False

        for raw_post in results:
            if not isinstance(raw_post, dict):
                continue

            normalized = normalize_post(
                raw_post,
                tag,
                feed,
                query_variant,
            )

            if normalized is None:
                continue

            post_id = str(normalized["id"])
            was_new = post_id not in posts_by_id

            posts_by_id[post_id] = merge_post(
                posts_by_id.get(post_id),
                normalized,
            )

            if was_new:
                unique_added += 1

            page_changed = True

        # Save after every successful page, even if all posts were duplicates.
        if page_changed:
            save_archive(posts_by_id)

        cursor_object = payload.get("cursor")
        next_cursor: str | None = None

        if isinstance(cursor_object, dict):
            bottom = cursor_object.get("bottom")

            if bottom:
                next_cursor = str(bottom)

        if not results or not next_cursor:
            break

        if next_cursor == cursor or next_cursor in seen_cursors:
            print("    Cursor repeated; stopping safely.")
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(REQUEST_DELAY_SECONDS)

    return {
        "hashtag": tag,
        "query": query_variant,
        "feed": feed,
        "pages_completed": pages_completed,
        "raw_posts_received": raw_posts_received,
        "unique_posts_added": unique_added,
        "stopped_by_404": stopped_by_404,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Accumulate public RADIO ARKADY posts from supported "
            "FxTwitter search feeds."
        )
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=250,
        help="Maximum pages per query and feed (1–1000).",
    )
    args = parser.parse_args()

    max_pages = max(1, min(args.pages, 1000))
    started_at = utc_now_iso()

    existing = load_json(ARCHIVE_FILE, [])

    if not isinstance(existing, list):
        existing = []

    posts_by_id: dict[str, dict[str, Any]] = {
        str(post.get("id")): post
        for post in existing
        if isinstance(post, dict) and post.get("id")
    }

    initial_count = len(posts_by_id)
    query_reports: list[dict[str, Any]] = []

    print("RADIO ARKADY free accumulator")
    print(f"Previous archive count: {initial_count}")
    print(f"Maximum pages per query/feed: {max_pages}")

    for tag in HASHTAGS:
        print(f"\n=== #{tag} ===")

        # Both forms are used because public search behavior may differ.
        query_variants = [f"#{tag}", tag]

        for query_variant in query_variants:
            for feed in FEEDS:
                report = collect_query(
                    tag,
                    query_variant,
                    feed,
                    max_pages,
                    posts_by_id,
                )
                query_reports.append(report)
                time.sleep(REQUEST_DELAY_SECONDS)

    archive = archive_list(posts_by_id)

    # Preserve an existing non-empty archive even when a run returns nothing.
    if archive or not ARCHIVE_FILE.exists():
        write_json(ARCHIVE_FILE, archive)

    counts = count_types(archive)

    report = {
        "status": (
            "ok"
            if archive
            else "no_results_or_source_unavailable"
        ),
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "search_mode": "free_accumulator",
        "feeds": FEEDS,
        "previous_archive_count": initial_count,
        "current_archive_count": len(archive),
        "new_unique_posts": max(
            0,
            len(archive) - initial_count,
        ),
        "counts": counts,
        "queries": query_reports,
        "source": API_URL,
        "notice": (
            "Best-effort public archive. The source does not document "
            "full historical date-range search. Deleted, private, "
            "unavailable or non-indexed posts may be absent."
        ),
    }

    write_json(REPORT_FILE, report)

    print("\nCollection complete")
    print(f"Previous unique posts: {initial_count}")
    print(f"Current unique posts:  {len(archive)}")
    print(f"New unique posts:      {report['new_unique_posts']}")
    print(f"Counts: {counts}")

    # Always allow GitHub Actions to commit the report and any partial data.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
