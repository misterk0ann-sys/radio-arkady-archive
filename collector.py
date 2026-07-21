#!/usr/bin/env python3
"""
RADIO ARKADY public archive collector — monthly backfill edition.

This script searches the configured hashtags month by month from 2020
through today. Splitting the search into date windows gives the public
search source a better chance of returning older posts instead of only
the newest global results.

It preserves every post already stored in archive.json and never replaces
a non-empty archive with an empty result set.

Source:
    https://api.fxtwitter.com/2/search

Important:
This remains a best-effort public archive. Deleted, private, unavailable,
or non-indexed posts cannot be guaranteed.
"""

from __future__ import annotations

import argparse
import calendar
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
from typing import Any, Iterator

API_URL = "https://api.fxtwitter.com/2/search"

HASHTAGS = [
    "RADIO_ARKADY",
    "ASK_ARKADY",
    "NAI_POIOS_EINAI",
    "ARKADIOS_ARKADYO",
]

ARCHIVE_FILE = Path("archive.json")
REPORT_FILE = Path("collection-report.json")

START_DATE = dt.date(2020, 1, 1)
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.45
MAX_TRANSIENT_RETRIES = 4

USER_AGENT = (
    "Mozilla/5.0 (compatible; RadioArkadyArchive/2.0; "
    "+https://github.com/)"
)

HASHTAG_RE = re.compile(
    r"(?<!\w)#([A-Za-z0-9_Α-Ωα-ωΆ-ώ]+)",
    re.UNICODE,
)


class NoResults(Exception):
    """The API returned 404: no results or timeline unavailable."""


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


def month_windows(
    start: dt.date,
    end_exclusive: dt.date,
) -> Iterator[tuple[dt.date, dt.date]]:
    current = dt.date(start.year, start.month, 1)

    while current < end_exclusive:
        last_day = calendar.monthrange(current.year, current.month)[1]
        next_month = current + dt.timedelta(days=last_day)
        yield current, min(next_month, end_exclusive)
        current = next_month


def request_json(params: dict[str, str | int]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{API_URL}?{query}"

    for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
                response_status = response.status

            code = int(payload.get("code", response_status))

            if code == 404:
                raise NoResults(
                    payload.get("message")
                    or "No results or timeline unavailable."
                )

            if code != 200:
                raise RuntimeError(
                    f"API returned code {code}: "
                    f"{payload.get('message', '')}"
                )

            return payload

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NoResults(
                    "No results or timeline unavailable."
                ) from exc

            if attempt >= MAX_TRANSIENT_RETRIES:
                raise RuntimeError(
                    f"HTTP request failed after {attempt} attempts: {exc}"
                ) from exc

            wait = min(30.0, (2 ** attempt) + random.random())
            print(
                f"    Temporary HTTP error "
                f"({attempt}/{MAX_TRANSIENT_RETRIES}): {exc}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            if attempt >= MAX_TRANSIENT_RETRIES:
                raise RuntimeError(
                    f"Request failed after {attempt} attempts: {exc}"
                ) from exc

            wait = min(30.0, (2 ** attempt) + random.random())
            print(
                f"    Temporary request error "
                f"({attempt}/{MAX_TRANSIENT_RETRIES}): {exc}. "
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
    window_start: dt.date,
    window_end: dt.date,
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
        "matched_windows": [
            f"{window_start.isoformat()}..{window_end.isoformat()}"
        ],
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

    for key in ("matched_queries", "matched_windows", "hashtags"):
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


def save_archive(posts_by_id: dict[str, dict[str, Any]]) -> None:
    archive = sorted(
        posts_by_id.values(),
        key=timestamp_value,
        reverse=True,
    )
    write_json(ARCHIVE_FILE, archive)


def collect_window(
    tag: str,
    window_start: dt.date,
    window_end: dt.date,
    max_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = (
        f"#{tag} "
        f"since:{window_start.isoformat()} "
        f"until:{window_end.isoformat()}"
    )

    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages_completed = 0
    error: str | None = None
    no_results = False

    for page_number in range(1, max_pages + 1):
        params: dict[str, str | int] = {
            "q": query,
            "feed": "latest",
            "count": PAGE_SIZE,
        }

        if cursor:
            params["cursor"] = cursor

        try:
            payload = request_json(params)
        except NoResults:
            no_results = True
            break
        except RuntimeError as exc:
            error = str(exc)
            print(f"    WARNING: {error}")
            break

        results = payload.get("results")

        if not isinstance(results, list):
            results = []

        pages_completed += 1

        for raw_post in results:
            if not isinstance(raw_post, dict):
                continue

            normalized = normalize_post(
                raw_post,
                tag,
                window_start,
                window_end,
            )

            if normalized is not None:
                collected.append(normalized)

        cursor_object = payload.get("cursor")
        next_cursor: str | None = None

        if isinstance(cursor_object, dict):
            bottom = cursor_object.get("bottom")

            if bottom:
                next_cursor = str(bottom)

        if not results or not next_cursor:
            break

        if next_cursor == cursor or next_cursor in seen_cursors:
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(REQUEST_DELAY_SECONDS)

    return collected, {
        "hashtag": tag,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "pages_completed": pages_completed,
        "posts_received": len(collected),
        "no_results_or_unavailable": no_results,
        "error": error,
    }


def count_types(archive: list[dict[str, Any]]) -> dict[str, int]:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect public RADIO ARKADY posts month by month."
        )
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=25,
        help="Maximum pages per hashtag per month (1–250).",
    )
    args = parser.parse_args()

    max_pages = max(1, min(args.pages, 250))
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
    reports: list[dict[str, Any]] = []
    total_received = 0

    today = dt.datetime.now(dt.timezone.utc).date()
    end_exclusive = today + dt.timedelta(days=1)
    windows = list(month_windows(START_DATE, end_exclusive))

    print(
        f"Monthly backfill from {START_DATE.isoformat()} "
        f"through {today.isoformat()}"
    )
    print(f"Date windows: {len(windows)}")
    print(f"Maximum pages per hashtag/month: {max_pages}")

    for tag in HASHTAGS:
        print(f"\n=== #{tag} ===")

        for index, (window_start, window_end) in enumerate(
            windows,
            start=1,
        ):
            collected, report = collect_window(
                tag,
                window_start,
                window_end,
                max_pages,
            )
            reports.append(report)

            if collected:
                print(
                    f"  {window_start:%Y-%m}: "
                    f"{len(collected)} posts"
                )
                total_received += len(collected)

                for post in collected:
                    post_id = str(post["id"])
                    posts_by_id[post_id] = merge_post(
                        posts_by_id.get(post_id),
                        post,
                    )

                # Save immediately so partial progress is never lost.
                save_archive(posts_by_id)

            if index % 12 == 0:
                print(
                    f"  Progress: {index}/{len(windows)} months; "
                    f"{len(posts_by_id)} unique posts stored"
                )

            time.sleep(REQUEST_DELAY_SECONDS)

    archive = sorted(
        posts_by_id.values(),
        key=timestamp_value,
        reverse=True,
    )

    # Never replace a non-empty archive with an empty one.
    if archive or not ARCHIVE_FILE.exists():
        write_json(ARCHIVE_FILE, archive)

    counts = count_types(archive)

    windows_with_results = sum(
        1 for item in reports
        if item.get("posts_received", 0) > 0
    )

    windows_with_errors = sum(
        1 for item in reports
        if item.get("error")
    )

    report = {
        "status": (
            "ok"
            if archive
            else "no_results_or_source_unavailable"
        ),
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "search_mode": "monthly_backfill",
        "start_date": START_DATE.isoformat(),
        "end_date": today.isoformat(),
        "max_pages_per_hashtag_per_month": max_pages,
        "previous_archive_count": initial_count,
        "current_archive_count": len(archive),
        "new_unique_posts": max(
            0,
            len(archive) - initial_count,
        ),
        "raw_posts_received": total_received,
        "windows_checked": len(reports),
        "windows_with_results": windows_with_results,
        "windows_with_errors": windows_with_errors,
        "counts": counts,
        "queries": reports,
        "source": API_URL,
        "notice": (
            "Best-effort public archive. Deleted, private, unavailable, "
            "or non-indexed posts may be absent."
        ),
    }

    write_json(REPORT_FILE, report)

    print("\nCollection complete")
    print(f"Previous unique posts: {initial_count}")
    print(f"Current unique posts:  {len(archive)}")
    print(f"New unique posts:      {report['new_unique_posts']}")
    print(f"Windows with results:  {windows_with_results}")
    print(f"Windows with errors:   {windows_with_errors}")
    print(f"Counts: {counts}")

    # A zero-result run should be visibly reported as a failure so that
    # GitHub does not show a misleading green success.
    return 0 if archive else 2


if __name__ == "__main__":
    raise SystemExit(main())
