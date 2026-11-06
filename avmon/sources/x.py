"""Logged-out X profile HTML. About five posts, not the archive."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from avmon.http import FetchError, get
from avmon.sources.model import Item, Parsed

SITE = "x"
LABEL = "X"
BLURB = "Posts on the logged-out profile"
MARK = "X"
AUTHOR = "@yua_mikami"


def fetch(url: str, native_id: str | None) -> Parsed:
    del native_id
    status, final_url, body = get(url)
    user_id, items = parse_html(body)
    if not items:
        raise FetchError(
            "X returned a page with no embedded posts. The logged-out profile is the source, and it did not include a timeline.",
            status,
        )
    note = f"logged-out profile embedded {len(items)} posts, not the full archive"
    return Parsed(status, final_url, items, native_id=user_id, note=note)


def parse_html(body: str) -> tuple[str | None, list[Item]]:
    """Read the posts embedded in the logged-out profile HTML."""
    user_ids = re.findall(r'\\"rest_id\\":\\"(\d+)\\"', body)
    if not user_ids or "full_text" not in body:
        return None, []
    user_id = Counter(user_ids).most_common(1)[0][0]
    items: list[Item] = []
    for match in re.finditer(r'\\"full_text\\":\\"(.*?)\\"', body):
        text = _js_string(match.group(1))
        before = body[max(0, match.start() - 280) : match.start()]
        after = body[match.end() : match.end() + 6000]
        boundary = after.find('\\"full_text\\"')
        if boundary > 0:
            after = after[:boundary]
        millis = re.findall(r'\\"created_at_ms\\":(\d+)', before)
        published = _ms(int(millis[-1])) if millis else None
        tweet_ids = [
            value for value in re.findall(r'\\"rest_id\\":\\"(\d+)\\"', after) if value != user_id
        ]
        if not tweet_ids:
            continue
        tweet_id = tweet_ids[0]
        media = _media(after, tweet_id)
        extra = _media_extra(media)
        items.append(
            Item(
                kind="post",
                external_id=tweet_id,
                url=f"https://x.com/yua_mikami/status/{tweet_id}",
                body=text.strip(),
                published_at=published,
                outbound_url=_outbound(after),
                extra=extra,
            )
        )
    return user_id, items


def _media(after: str, tweet_id: str) -> list[dict]:
    """Photos, plus a poster for a video or gif. The video file itself is left out."""
    items: list[dict] = []
    seen: set[str] = set()
    tweet_url = f"https://x.com/yua_mikami/status/{tweet_id}"
    for match in re.finditer(r'\\"media_url_https\\":\\"(https:[^\\"]+)\\"', after):
        url = _js_string(match.group(1)).split("?", 1)[0]
        if not url.startswith("https://pbs.twimg.com/"):
            continue
        tail = after[match.end() : match.end() + 240]
        kind = re.search(r'\\"type\\":\\"(photo|video|animated_gif)\\"', tail)
        if kind is None:
            continue
        head = after[max(0, match.start() - 700) : match.start()]
        expanded = re.findall(r'\\"expanded_url\\":\\"(https:[^\\"]+)\\"', head)
        if expanded and tweet_id not in _js_string(expanded[-1]):
            continue
        if kind.group(1) == "photo":
            if "/media/" not in url:
                continue
            item = {"kind": "image", "url": url}
            key = url
        else:
            path = urlsplit(url).path.lower()
            if path.endswith((".mp4", ".m3u8", ".mov", ".webm", ".m4v")):
                continue
            item = {"kind": "video", "poster": url, "href": tweet_url}
            key = url
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) == 8:
            break
    return items


def _media_extra(media: list[dict]) -> dict:
    images = [item["url"] for item in media if item.get("kind") == "image" and item.get("url")]
    extra: dict = {}
    if media:
        extra["media"] = media
    if images:
        extra["images"] = images
    return extra


def _outbound(after: str) -> str | None:
    urls = re.findall(r'\\"expanded_url\\":\\"(https:[^\\"]+)\\"', after)
    cleaned = []
    for url in urls:
        url = _js_string(url)
        if "/status/" in url or "/photo/" in url:
            continue
        cleaned.append(_strip_tracking(url))
    for url in cleaned:
        if "youtu.be" in url or "youtube.com" in url:
            return url
    return cleaned[0] if cleaned else None


def _strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "si"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _js_string(raw: str) -> str:
    # The profile payload escapes the tweet JSON twice.
    text = raw
    for _ in range(2):
        try:
            decoded = json.loads('"' + text + '"')
        except json.JSONDecodeError:
            break
        if not isinstance(decoded, str) or decoded == text:
            break
        text = decoded
    return text


def _ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
