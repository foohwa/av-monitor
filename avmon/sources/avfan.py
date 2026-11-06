"""avfan tag page, then each movie page for the real DMM content id."""

from __future__ import annotations

import re

from avmon.http import FetchError, get
from avmon.sources.dmm import code_key, package_url
from avmon.sources.htmltext import plain
from avmon.sources.model import Item, Parsed

SITE = "avfan"
LABEL = "avfan"
BLURB = "Titles on the tag page"
MARK = "FAN"
AUTHOR = "AV女優FAN"


def fetch(url: str, native_id: str | None) -> Parsed:
    del native_id
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
        title = plain(inner)
        if not title:
            continue
        seen.add(match.group(1))
        page_url = href if href.startswith("http") else "https://avfan.org" + href
        cover = _abs_https(image.strip("\"' "))
        cid = _cid(page_url)
        extra: dict = {"listed_on": "avfan"}
        if cid:
            extra["code_key"] = code_key(cid)
            extra["images"] = [package_url(cid)]
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


def _cid(page_url: str) -> str | None:
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
