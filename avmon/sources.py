"""Public-page parsers for the seven saved links of one actress."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from avmon.http import FetchError, get

ATOM = "http://www.w3.org/2005/Atom"
YT = "http://www.youtube.com/xml/schemas/2015"
MEDIA = "http://search.yahoo.com/mrss/"


@dataclass
class Item:
    kind: str
    external_id: str
    url: str
    title: str | None = None
    body: str | None = None
    published_at: str | None = None
    outbound_url: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Parsed:
    http_status: int
    final_url: str
    items: list[Item] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)
    native_id: str | None = None
    note: str | None = None


def fetch_account(site: str, url: str, native_id: str | None) -> Parsed:
    if site == "youtube":
        return fetch_youtube(native_id or "")
    if site == "x":
        return fetch_x(url)
    if site == "tiktok":
        return fetch_tiktok(url)
    if site == "instagram":
        return fetch_instagram(url)
    if site == "oshiseku":
        return fetch_oshiseku(url)
    if site == "javforum":
        return fetch_javforum(url)
    if site == "avfan":
        return fetch_avfan(url)
    raise FetchError(f"no parser for {site}")


def fetch_youtube(channel_id: str) -> Parsed:
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    status, final_url, body = get(feed_url, accept="application/atom+xml,application/xml,text/xml")
    root = ElementTree.fromstring(body)
    items: list[Item] = []
    for entry in root.findall(f"{{{ATOM}}}entry"):
        video_id = _text(entry, f"{{{YT}}}videoId")
        if not video_id:
            continue
        title = _text(entry, f"{{{ATOM}}}title")
        published = _text(entry, f"{{{ATOM}}}published")
        description = _text(entry, f"{{{MEDIA}}}group/{{{MEDIA}}}description")
        items.append(
            Item(
                kind="upload",
                external_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title,
                body=_clip(description, 240),
                published_at=published,
            )
        )
    return Parsed(status, final_url, items, native_id=channel_id)


def fetch_x(url: str) -> Parsed:
    status, final_url, body = get(url)
    user_id, items = parse_x_html(body)
    if not items:
        raise FetchError(
            "X returned a page with no embedded posts. The logged-out profile is the source, and it did not include a timeline.",
            status,
        )
    note = f"logged-out profile embedded {len(items)} posts, not the full archive"
    return Parsed(status, final_url, items, native_id=user_id, note=note)


def parse_x_html(body: str) -> tuple[str | None, list[Item]]:
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
        media = _x_media(after, tweet_id)
        extra = _media_extra(media)
        items.append(
            Item(
                kind="post",
                external_id=tweet_id,
                url=f"https://x.com/yua_mikami/status/{tweet_id}",
                body=text.strip(),
                published_at=published,
                outbound_url=_x_outbound(after),
                extra=extra,
            )
        )
    return user_id, items


def fetch_tiktok(url: str) -> Parsed:
    status, final_url, body = get(
        "https://www.tiktok.com/oembed?url=" + url,
        accept="application/json",
    )
    if "Please wait" in body or "wafchallenge" in body:
        raise FetchError("TikTok returned a WAF challenge instead of the profile", status)
    payload = json.loads(body)
    author = payload.get("author_name") or ""
    handle = payload.get("author_url") or url
    if "yua_mikami" not in handle and "yua_mikami" not in url:
        raise FetchError(f"TikTok oEmbed pointed at {handle}", status)
    note = (
        f"oEmbed confirms the profile ({author}). "
        "The video page is a WAF challenge, so no posts were stored."
    )
    return Parsed(status, final_url, [], note=note)


def fetch_instagram(url: str) -> Parsed:
    status, final_url, body = get(url)
    title = _title(body)
    has_posts = "shortcode" in body and "edge_owner_to_timeline_media" in body
    if not has_posts:
        note = (
            f"Instagram returned title {title!r} and no public post list. "
            "No posts were stored."
        )
        return Parsed(status, final_url, [], note=note)
    return Parsed(status, final_url, [], note="Instagram post markup was present but this check does not parse it yet")


def fetch_oshiseku(url: str) -> Parsed:
    status, final_url, body = get(url)
    facts = _profile_table(body)
    summary = _section_paragraph(body, "三上悠亜とは")
    if summary:
        facts.append(("summary", _clip(summary, 500)))
    items = _dmm_works(body)
    if not facts and not items:
        raise FetchError("Oshiseku page had neither the profile table nor DMM works", status)
    return Parsed(status, final_url, items, facts)


def fetch_javforum(url: str) -> Parsed:
    latest = url.rstrip("/") + "/latest"
    status, final_url, body = get(latest)
    items = _forum_posts(body, final_url)
    if not items:
        raise FetchError(f"Jav-forum latest page had no posts ({final_url})", status)
    note = f"latest page {final_url}"
    given = url.rstrip("/") + "/page-10"
    if "post-108578" not in body:
        status2, final2, body2 = get(given)
        extra = _forum_posts(body2, final2)
        known = {item.external_id for item in items}
        items.extend(item for item in extra if item.external_id not in known)
        note += f"; also read {final2} (HTTP {status2}) for post 108578"
        status = status2 if status2 else status
    return Parsed(status, final_url, items, note=note)


def fetch_avfan(url: str) -> Parsed:
    status, final_url, body = get(url)
    items: list[Item] = []
    seen: set[str] = set()
    boxes = re.findall(
        r'<div class="halfimage"[^>]*style="background-image:url\(([^)]+)\)"[^>]*>\s*'
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        body,
        re.S | re.I,
    )
    for image, href, inner in boxes:
        match = re.search(r"/movie/.+/(\d+)\.html", href)
        if match is None or match.group(1) in seen:
            continue
        title = _plain(inner)
        if not title:
            continue
        seen.add(match.group(1))
        page_url = href if href.startswith("http") else "https://avfan.org" + href
        cover = _abs_https(image.strip("\"' "))
        cid = _avfan_cid(page_url)
        extra: dict = {"listed_on": "avfan"}
        if cid:
            extra["code_key"] = _code_key(cid)
            extra["images"] = [_package_url(cid)]
        elif cover:
            extra["images"] = [cover]
        items.append(
            Item(
                kind="work_sighting",
                external_id=match.group(1),
                url=page_url,
                title=title,
                extra=extra,
            )
        )
    if not items:
        raise FetchError("avfan tag page had no movie links", status)
    coded = sum(1 for item in items if item.extra.get("code_key"))
    return Parsed(
        status,
        final_url,
        items,
        note=f"tag page plus {len(items)} movie pages, {coded} have a product id",
    )


def _forum_posts(html: str, page_url: str) -> list[Item]:
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
        images = _forum_images(body_html)
        play = _forum_clip_url(body_html)
        body_html = re.sub(r"<video[\s\S]*?</video>", " ", body_html, flags=re.I)
        text = _plain(body_html)
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
                body=_clip(text, 500),
                published_at=published,
                extra=extra,
            )
        )
    return items


def _profile_table(html: str) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    for table in re.findall(r"<table[\s\S]*?</table>", html, re.I):
        if "よみがな" not in table:
            continue
        for label_html, value_html in re.findall(
            r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", table, re.S | re.I
        ):
            label = _plain(label_html)
            value = _plain(value_html)
            if label and value:
                facts.append((label, value))
        break
    return facts


def _section_paragraph(html: str, heading: str) -> str | None:
    index = html.find(heading)
    if index < 0:
        return None
    match = re.search(r"<p>(.*?)</p>", html[index : index + 4000], re.S)
    if match is None:
        return None
    return _plain(match.group(1))


def _dmm_works(html: str) -> list[Item]:
    found: dict[str, Item] = {}
    for href, inner in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        match = re.search(r"(?:cid=|id%3D)([a-z0-9]+)", href, re.I)
        if match is None:
            continue
        cid = match.group(1).lower()
        title = _link_label(inner)
        if len(title) < 8:
            continue
        current = found.get(cid)
        if current is None or len(title) > len(current.title or ""):
            found[cid] = Item(
                kind="work_sighting",
                external_id=cid,
                url=f"https://video.dmm.co.jp/av/content/?id={cid}",
                title=title,
                extra={
                    "code_key": _code_key(cid),
                    "listed_on": "oshiseku",
                    "images": [_package_url(cid)],
                },
            )
    return list(found.values())


def _x_media(after: str, tweet_id: str) -> list[dict]:
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


def _forum_images(html: str) -> list[str]:
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
        url = _forum_photo_url(src)
        if url and url not in images:
            images.append(url)
        if len(images) == 8:
            break
    return images


def _forum_clip_url(html: str) -> str | None:
    """Address of a video attached to this post. The file stays on the forum."""
    for tag in re.findall(r"<video\b[\s\S]*?</video>", html, flags=re.I):
        for src in re.findall(r'(?:src|data-src)="([^"]+)"', tag, flags=re.I):
            url = _forum_video_url(unescape(src.strip()))
            if url:
                return url
    return None


def _forum_video_url(src: str) -> str | None:
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


def _forum_photo_url(src: str) -> str | None:
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


def _x_outbound(after: str) -> str | None:
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


def _avfan_cid(page_url: str) -> str | None:
    """Product id from the sample player on the movie page. A miss keeps the cover only."""
    try:
        _, _, body = get(page_url)
    except FetchError:
        return None
    for cid in re.findall(r"cid=([a-z0-9]+)", body, re.I):
        if re.fullmatch(r"[a-z]+\d+", cid, re.I):
            return cid.lower()
    return None


def _abs_https(src: str) -> str | None:
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = "https://avfan.org" + src
    if not src.startswith("https://"):
        return None
    return src.split("?", 1)[0]


def _package_url(cid: str) -> str:
    """Public package image. The path is fixed from the content id."""
    return f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg"


def _code_key(cid: str) -> str:
    match = re.fullmatch(r"([a-z]+)(\d+)", cid.lower())
    if match is None:
        return cid.upper()
    return f"{match.group(1).upper()}-{int(match.group(2))}"


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


def _link_label(inner: str) -> str:
    text = _plain(inner)
    labelled = re.findall(r'(?:alt|title)="([^"]+)"', inner)
    labelled = [unescape(value).strip() for value in labelled if value.strip()]
    if not labelled:
        return text
    best = max(labelled, key=len)
    return best if len(best) > len(text) else text


def _plain(html: str) -> str:
    text = unescape(html)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return _plain(match.group(1)) if match else ""


def _text(node: ElementTree.Element, path: str) -> str | None:
    found = node.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loose_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
