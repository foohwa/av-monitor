"""YouTube channel RSS."""

from __future__ import annotations

from xml.etree import ElementTree

from avmon.http import get
from avmon.sources.htmltext import clip
from avmon.sources.model import Item, Parsed

SITE = "youtube"
LABEL = "YouTube"
BLURB = "Uploads from the channel RSS"
MARK = "YT"
AUTHOR = "ゆあちゃんねる"

ATOM = "http://www.w3.org/2005/Atom"
YT = "http://www.youtube.com/xml/schemas/2015"
MEDIA = "http://search.yahoo.com/mrss/"


def fetch(url: str, native_id: str | None) -> Parsed:
    del url
    channel_id = native_id or ""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    status, final_url, body = get(feed_url, accept="application/atom+xml,application/xml,text/xml")
    root = ElementTree.fromstring(body)
    author = _text(root, f"{{{ATOM}}}author/{{{ATOM}}}name")
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
                body=clip(description, 240),
                published_at=published,
                extra={"author": author} if author else {},
            )
        )
    return Parsed(status, final_url, items, native_id=channel_id)


def _text(node: ElementTree.Element, path: str) -> str | None:
    found = node.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()
