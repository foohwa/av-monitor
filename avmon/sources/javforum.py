"""Jav-forum thread: latest page, plus page 10 when the sample post is missing."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlsplit, urlunsplit

from avmon.http import FetchError, get
from avmon.sources.htmltext import clip, plain
from avmon.sources.model import Item, Parsed

SITE = "javforum"
LABEL = "Jav-forum"
BLURB = "Posts in her thread"
MARK = "掲"
AUTHOR = "Jav-forum"


def fetch(url: str, native_id: str | None) -> Parsed:
    del native_id
    latest = url.rstrip("/") + "/latest"
    status, final_url, body = get(latest)
    items = _posts(body, final_url)
    if not items:
        raise FetchError(f"Jav-forum latest page had no posts ({final_url})", status)
    note = f"latest page {final_url}"
    given = url.rstrip("/") + "/page-10"
    if "post-108578" not in body:
        status2, final2, body2 = get(given)
        extra = _posts(body2, final2)
        known = {item.external_id for item in items}
        items.extend(item for item in extra if item.external_id not in known)
        note += f"; also read {final2} (HTTP {status2}) for post 108578"
        status = status2 if status2 else status
    return Parsed(status, final_url, items, note=note)


def _posts(html: str, page_url: str) -> list[Item]:
    parts = re.split(r'id="js-post-(\d+)"', html)
    items: list[Item] = []
    for index in range(1, len(parts), 2):
        post_id = parts[index]
        chunk = parts[index + 1]
        published = None
        stamp = re.search(r'itemprop="datePublished"[^>]*datetime="([^"]+)"', chunk)
        if stamp is None:
            stamp = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        if stamp:
            published = _loose_time(stamp.group(1))
        author_match = re.search(r'itemprop="name">([^<]+)', chunk)
        author = author_match.group(1).strip() if author_match else None
        body_html = ""
        start = chunk.find('class="bbWrapper"')
        if start >= 0:
            start = chunk.find(">", start) + 1
            end_match = re.search(r"<div[^>]*js-selectToQuoteEnd", chunk[start:])
            end = start + end_match.start() if end_match else start + 4000
            body_html = chunk[start:end]
        has_media = "<video" in body_html or "<img" in body_html
        body_html = re.sub(r"<blockquote[\s\S]*?</blockquote>", " ", body_html, flags=re.I)
        images = _images(body_html)
        play = _clip_url(body_html)
        body_html = re.sub(r"<video[\s\S]*?</video>", " ", body_html, flags=re.I)
        text = plain(body_html)
        extra: dict = {"author": author, "page": page_url, "has_media": has_media}
        if images:
            extra["images"] = images
        if play:
            extra["clip"] = True
            extra["play"] = play
        items.append(
            Item(
                kind="forum_post",
                external_id=post_id,
                url=f"https://www.jav-forum.com/posts/{post_id}/",
                title=None,
                body=clip(text, 500),
                published_at=published,
                extra=extra,
            )
        )
    return items


def _images(html: str) -> list[str]:
    """Attachment photos. Quoted posts, link previews, and video files are left out."""
    images: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", html, flags=re.I):
        if re.search(r'class="[^"]*\bbbImage\b', tag) is None:
            continue
        src = ""
        for attr in ("src", "data-src", "data-url"):
            found = re.search(rf'{attr}="([^"]+)"', tag, flags=re.I)
            if found and found.group(1).strip():
                src = unescape(found.group(1).strip())
                break
        url = _photo_url(src)
        if url and url not in images:
            images.append(url)
        if len(images) == 8:
            break
    return images


def _clip_url(html: str) -> str | None:
    """Address of a video attached to this post. The file stays on the forum."""
    for tag in re.findall(r"<video\b[\s\S]*?</video>", html, flags=re.I):
        for src in re.findall(r'(?:src|data-src)="([^"]+)"', tag, flags=re.I):
            url = _video_url(unescape(src.strip()))
            if url:
                return url
    return None


def _video_url(src: str) -> str | None:
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = "https://www.jav-forum.com" + src
    elif not src.startswith("https://"):
        return None
    parts = urlsplit(src)
    host = parts.netloc.lower()
    if host not in ("www.jav-forum.com", "jav-forum.com"):
        return None
    if not parts.path.lower().endswith((".mp4", ".webm", ".mov", ".m4v")):
        return None
    return urlunsplit(("https", "www.jav-forum.com", parts.path, "", ""))


def _photo_url(src: str) -> str | None:
    if not src or src.startswith("data:"):
        return None
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = "https://www.jav-forum.com" + src
    elif not src.startswith("https://"):
        return None
    parts = urlsplit(src)
    if parts.scheme != "https" or not parts.netloc:
        return None
    path = parts.path.lower()
    if path.endswith((".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi")):
        return None
    if any(piece in path for piece in ("/smilies/", "/emoji/", "/styles/", "favicon")):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _loose_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
