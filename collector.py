#!/usr/bin/env python3
"""
RADIO ARKADY public post collector.

Collects public X/Twitter search results from FxTwitter's public API:
    https://api.fxtwitter.com/2/search

The collector:
- searches the configured hashtags
- follows cursor.bottom pagination
- removes duplicate posts by ID
- preserves previous results in archive.json
- writes a collection report
- uses only Python's standard library

This is a best-effort public archive. The source may omit deleted, private,
unavailable, or non-indexed posts.
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

ARCHIVE_FILE = Path("archive.json")
REPORT_FILE = Path("collection-report.json")

PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.35
MAX_RETRIES = 5
USER_AGENT = (
    "Mozilla/5.0 (compatible; RadioArkadyArchive/1.0; "
    "+https://github.com/)"
)

HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_Α-Ωα-ωΆ-ώ]+)", re.UNICODE)


class SearchExhausted(Exception):
    """Raised when the public search source has no more available results."""



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
                response_status = response.status

            code = int(payload.get("code", response_status))

            if code == 404:
                raise SearchExhausted(
                    payload.get("message") or "No more results are available."
                )

            if code != 200:
                raise RuntimeError(
                    f"API returned code {code}: {payload.get('message', '')}"
                )

            return payload

        except urllib.error.HTTPError as exc:
            # FxTwitter may answer 404 when a search has no results or a
            # pagination cursor has reached the end. This is not a fatal error.
            if exc.code == 404:
                raise SearchExhausted(
                    "The search source returned 404: no more available results."
                ) from exc

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"Request failed after {attempt} attempts: {exc}"
                ) from exc

            wait = min(30.0, (2 ** attempt) + random.random())
            print(
                f"  Request error ({attempt}/{MAX_RETRIES}): {exc}. "
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
                f"  Request error ({attempt}/{MAX_RETRIES}): {exc}. "
                f"Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)

    raise RuntimeError("Unreachable request state")


def media_items(post: dict[str, Any]) -> list[dict[str, Any]]:
    media = post.get("media")
    if not isinstance(media, dict):
        return []

    combined = media.get("all")
    if isinstance(combined, list):
        return [item for item in combined if isinstance(item, dict)]

    items: list[dict[str, Any]] = []

    for key in ("photos", "videos"):
        values = media.get(key)
        if isinstance(values, list):
            items.extend(item for item in values if isinstance(item, dict))

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

    for item in media_items(post):
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


def normalize_post(post: dict[str, Any], searched_tag: str) -> dict[str, Any] | None:
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

    normalized_media = [
        normalize_media(item)
        for item in media_items(post)
    ]

    normalized_media = [item for item in normalized_media if item]

    return {
        "id": post_id,
        "url": post.get("url") or (
            f"https://x.com/{author.get('screen_name')}/status/{post_id}"
            if author.get("screen_name")
            else f"https://x.com/i/status/{post_id}"
        ),
        "text": text,
        "created_at": post.get("created_at"),
        "created_timestamp": post.get("created_timestamp"),
        "author": {
            "id": author.get("id"),
            "name": author.get("name"),
            "screen_name": author.get("screen_name"),
            "avatar_url": author.get("avatar_url"),
        },
        "likes": post.get("likes", 0),
        "reposts": post.get("reposts", 0),
        "quotes": post.get("quotes", 0),
        "replies": post.get("replies", 0),
        "hashtags": found_hashtags,
        "matched_queries": [searched_tag],
        "types": detect_types(post),
        "media": normalized_media,
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

    old_queries = previous.get("matched_queries", [])
    new_queries = current.get("matched_queries", [])

    merged["matched_queries"] = sorted(
        {
            str(value)
            for value in [*old_queries, *new_queries]
            if value
        }
    )

    old_hashtags = previous.get("hashtags", [])
    new_hashtags = current.get("hashtags", [])

    merged["hashtags"] = sorted(
        {
            str(value)
            for value in [*old_hashtags, *new_hashtags]
            if value
        }
    )

    return merged


def timestamp_value(post: dict[str, Any]) -> float:
    raw = post.get("created_timestamp")

    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def collect_tag(
    tag: str,
    max_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    print(f"\nSearching #{tag}")

    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages_completed = 0
    error: str | None = None

    for page_number in range(1, max_pages + 1):
        params: dict[str, str | int] = {
            "q": f"#{tag}",
            "feed": "latest",
            "count": PAGE_SIZE,
        }

        if cursor:
            params["cursor"] = cursor

        try:
            payload = request_json(params)
        except SearchExhausted as exc:
            print(f"  End of available results: {exc}")
            break
        except RuntimeError as exc:
            error = str(exc)
            print(f"  WARNING: {error}")
            break

        results = payload.get("results")
        if not isinstance(results, list):
            results = []

        pages_completed += 1
        print(
            f"  Page {page_number}/{max_pages}: "
            f"{len(results)} results"
        )

        for raw_post in results:
            if not isinstance(raw_post, dict):
                continue

            normalized = normalize_post(raw_post, tag)
            if normalized is not None:
                collected.append(normalized)

        cursor_object = payload.get("cursor")
        next_cursor: str | None = None

        if isinstance(cursor_object, dict):
            bottom = cursor_object.get("bottom")
            if bottom:
                next_cursor = str(bottom)

        if not results or not next_cursor:
            print("  Reached the end of available results.")
            break

        if next_cursor == cursor or next_cursor in seen_cursors:
            print("  Pagination cursor repeated; stopping safely.")
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(REQUEST_DELAY_SECONDS)

    return collected, {
        "hashtag": tag,
        "pages_completed": pages_completed,
        "posts_received": len(collected),
        "last_cursor_available": bool(cursor),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect public RADIO ARKADY posts."
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=25,
        help="Maximum pages per hashtag (1–1000).",
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
    reports: list[dict[str, Any]] = []

    for tag in HASHTAGS:
        collected, report = collect_tag(tag, max_pages)
        reports.append(report)

        for post in collected:
            post_id = str(post["id"])
            posts_by_id[post_id] = merge_post(
                posts_by_id.get(post_id),
                post,
            )

    archive = sorted(
        posts_by_id.values(),
        key=timestamp_value,
        reverse=True,
    )

    write_json(ARCHIVE_FILE, archive)

    type_counts = {
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
                type_counts[content_type] += 1

    report = {
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "max_pages_per_hashtag": max_pages,
        "previous_archive_count": initial_count,
        "current_archive_count": len(archive),
        "new_unique_posts": max(0, len(archive) - initial_count),
        "counts": type_counts,
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
    print(f"Counts: {type_counts}")

    failed = [item for item in reports if item.get("error")]

    if failed:
        print(
            f"Warnings: {len(failed)} search queries had temporary errors. "
            "The archive and report were still saved."
        )

    # Always return success after archive.json and collection-report.json
    # have been written. This allows the GitHub Action to commit partial
    # results instead of discarding them when one query fails.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
