#!/usr/bin/env python3
"""
RADIO ARKADY public archive collector — hashtag variants edition.

Why this version exists:
The public FxTwitter search source may return different results depending
on the exact capitalization of a hashtag. This collector tries several
forms for every RADIO ARKADY hashtag, with and without #, and across the
latest, top and media feeds.

It:
- preserves the existing archive.json
- removes duplicates by post ID
- saves after every successful page
- records which canonical hashtag and exact query found each post
- writes honest per-hashtag coverage counts to collection-report.json

This remains a best-effort free archive. A zero result for a query means
the public search source did not return data; it does not prove that no
posts exist on X.
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
ARCHIVE_FILE = Path("archive.json")
REPORT_FILE = Path("collection-report.json")

FEEDS = ["latest", "top", "media"]
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.55
MAX_RETRIES = 4

USER_AGENT = (
    "Mozilla/5.0 (compatible; RadioArkadyArchive/4.0; "
    "+https://github.com/)"
)

HASHTAG_SPECS = {
    "RADIO_ARKADY": [
        "#RADIO_ARKADY",
        "#radio_arkady",
        "#Radio_Arkady",
        "RADIO_ARKADY",
        "radio_arkady",
        "Radio_Arkady",
    ],
    "ASK_ARKADY": [
        "#ASK_ARKADY",
        "#ask_arkady",
        "#Ask_Arkady",
        "ASK_ARKADY",
        "ask_arkady",
        "Ask_Arkady",
    ],
    "NAI_POIOS_EINAI": [
        "#NAI_POIOS_EINAI",
        "#nai_poios_einai",
        "#Nai_poios_einai",
        "#Nai_Poios_Einai",
        "NAI_POIOS_EINAI",
        "nai_poios_einai",
        "Nai_poios_einai",
        "Nai_Poios_Einai",
    ],
    "ARKADIOS_ARKADYO": [
        "#ARKADIOS_ARKADYO",
        "#arkadios_arkadyo",
        "#Arkadios_Arkadyo",
        "ARKADIOS_ARKADYO",
        "arkadios_arkadyo",
        "Arkadios_Arkadyo",
    ],
}

KNOWN_TAGS = set(HASHTAG_SPECS)
HASHTAG_RE = re.compile(
    r"(?<!\w)#([A-Za-z0-9_Α-Ωα-ωΆ-ώ]+)",
    re.UNICODE,
)


class NoMoreResults(Exception):
    """The public search returned no results or no further timeline."""


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
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"

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
                    payload.get("message") or "No results available."
                )

            if code != 200:
                raise RuntimeError(
                    f"API returned code {code}: {payload.get('message', '')}"
                )

            return payload

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NoMoreResults("The search source returned 404.") from exc

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"HTTP request failed after {attempt} attempts: {exc}"
                ) from exc

            wait = min(30.0, (2 ** attempt) + random.random())
            print(
                f"    Temporary HTTP error ({attempt}/{MAX_RETRIES}): "
                f"{exc}. Retrying in {wait:.1f}s..."
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
                f"    Temporary request error ({attempt}/{MAX_RETRIES}): "
                f"{exc}. Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)

    raise RuntimeError("Unreachable request state")


def raw_media_items(post: dict[str, Any]) -> list[dict[str, Any]]:
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

    for item in raw_media_items(post):
        item_type = str(item.get("type", "")).lower()

        if item_type in ("photo", "image"):
            types.add("IMAGE")
        elif item_type == "video":
            types.add("VIDEO")
        elif item_type == "gif":
            types.add("GIF")

    if not types:
        types.add("TEXT")

    return sorted(types)


def text_hashtags(text: str) -> list[str]:
    output: set[str] = set()

    for match in HASHTAG_RE.findall(text):
        canonical = match.upper()

        if canonical in KNOWN_TAGS:
            output.add(canonical)

    return sorted(output)


def normalize_post(
    post: dict[str, Any],
    canonical_tag: str,
    query_variant: str,
    feed: str,
) -> dict[str, Any] | None:
    post_id = str(post.get("id", "")).strip()

    if not post_id:
        return None

    author = post.get("author")

    if not isinstance(author, dict):
        author = {}

    text = str(post.get("text", ""))
    media = [
        normalize_media(item)
        for item in raw_media_items(post)
    ]
    media = [item for item in media if item]

    username = author.get("screen_name")

    hashtags = set(text_hashtags(text))
    hashtags.add(canonical_tag)

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
        "hashtags": sorted(hashtags),
        "matched_queries": [canonical_tag],
        "matched_query_variants": [query_variant],
        "matched_feeds": [feed],
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
        "hashtags",
        "matched_queries",
        "matched_query_variants",
        "matched_feeds",
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


def collect_query(
    canonical_tag: str,
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

    print(
        f"\n  {canonical_tag} | query={query_variant!r} | feed={feed}"
    )

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
            print("    End/no results from source.")
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
                canonical_tag,
                query_variant,
                feed,
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
        "hashtag": canonical_tag,
        "query": query_variant,
        "feed": feed,
        "pages_completed": pages_completed,
        "raw_posts_received": raw_posts_received,
        "unique_posts_added": unique_added,
        "stopped_by_404": stopped_by_404,
        "error": error,
    }


def count_content_types(
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
        if not isinstance(post, dict):
            continue

        types = post.get("types", [])

        if not isinstance(types, list):
            continue

        for content_type in ("GIF", "IMAGE", "TEXT", "VIDEO"):
            if content_type in types:
                counts[content_type] += 1

    return counts


def count_hashtags(
    archive: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {tag: 0 for tag in HASHTAG_SPECS}

    for post in archive:
        if not isinstance(post, dict):
            continue

        tags: set[str] = set()

        for field in ("hashtags", "matched_queries"):
            values = post.get(field, [])

            if isinstance(values, list):
                tags.update(str(value).upper() for value in values)

        for tag in counts:
            if tag in tags:
                counts[tag] += 1

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect RADIO ARKADY posts using hashtag case variants."
        )
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=250,
        help="Maximum pages per query/feed (1–1000).",
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

    print("RADIO ARKADY hashtag variants collector")
    print(f"Previous archive count: {initial_count}")
    print(f"Maximum pages per query/feed: {max_pages}")

    for canonical_tag, variants in HASHTAG_SPECS.items():
        print(f"\n=== {canonical_tag} ===")

        # dict.fromkeys removes accidental duplicate variants while
        # preserving their order.
        for query_variant in dict.fromkeys(variants):
            for feed in FEEDS:
                query_reports.append(
                    collect_query(
                        canonical_tag,
                        query_variant,
                        feed,
                        max_pages,
                        posts_by_id,
                    )
                )
                time.sleep(REQUEST_DELAY_SECONDS)

    archive = archive_list(posts_by_id)

    if archive or not ARCHIVE_FILE.exists():
        write_json(ARCHIVE_FILE, archive)

    content_counts = count_content_types(archive)
    hashtag_counts = count_hashtags(archive)

    query_success = {
        tag: {
            "queries_with_results": 0,
            "pages_completed": 0,
            "raw_posts_received": 0,
        }
        for tag in HASHTAG_SPECS
    }

    for item in query_reports:
        tag = str(item.get("hashtag", ""))

        if tag not in query_success:
            continue

        pages = int(item.get("pages_completed", 0) or 0)
        raw = int(item.get("raw_posts_received", 0) or 0)

        query_success[tag]["pages_completed"] += pages
        query_success[tag]["raw_posts_received"] += raw

        if raw > 0:
            query_success[tag]["queries_with_results"] += 1

    report = {
        "status": "ok" if archive else "no_results_or_source_unavailable",
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "search_mode": "hashtag_case_variants",
        "previous_archive_count": initial_count,
        "current_archive_count": len(archive),
        "new_unique_posts": max(0, len(archive) - initial_count),
        "counts": content_counts,
        "hashtag_counts_minimum": hashtag_counts,
        "hashtag_query_coverage": query_success,
        "queries": query_reports,
        "source": API_URL,
        "notice": (
            "The per-hashtag counts are minimum verified counts from "
            "the posts returned by the free public search source. They "
            "must not be interpreted as complete historical totals."
        ),
    }

    write_json(REPORT_FILE, report)

    print("\nCollection complete")
    print(f"Previous unique posts: {initial_count}")
    print(f"Current unique posts:  {len(archive)}")
    print(f"New unique posts:      {report['new_unique_posts']}")
    print(f"Content counts:        {content_counts}")
    print(f"Hashtag minimums:      {hashtag_counts}")
    print(f"Query coverage:        {query_success}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
