"""Oshiseku profile: facts plus the DMM works embedded on that page."""

from __future__ import annotations

import re

from avmon.http import FetchError, get
from avmon.sources.dmm import code_key, package_url
from avmon.sources.htmltext import clip, link_label, plain
from avmon.sources.model import Item, Parsed

SITE = "oshiseku"
LABEL = "Oshiseku"
BLURB = "Works embedded on the profile page"
MARK = "推"
AUTHOR = "推しセク"


def fetch(url: str, native_id: str | None) -> Parsed:
    del native_id
    status, final_url, body = get(url)
    facts = _profile_table(body)
    summary = _section_paragraph(body, "三上悠亜とは")
    if summary:
        facts.append(("summary", clip(summary, 500)))
    items = _dmm_works(body)
    if not facts and not items:
        raise FetchError("Oshiseku page had neither the profile table nor DMM works", status)
    return Parsed(status, final_url, items, facts)


def _profile_table(html: str) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    for table in re.findall(r"<table[\s\S]*?</table>", html, re.I):
        if "よみがな" not in table:
            continue
        for label_html, value_html in re.findall(
            r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", table, re.S | re.I
        ):
            label = plain(label_html)
            value = plain(value_html)
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
    return plain(match.group(1))


def _dmm_works(html: str) -> list[Item]:
    found: dict[str, Item] = {}
    for href, inner in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        match = re.search(r"(?:cid=|id%3D)([a-z0-9]+)", href, re.I)
        if match is None:
            continue
        cid = match.group(1).lower()
        title = link_label(inner)
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
                    "code_key": code_key(cid),
                    "listed_on": "oshiseku",
                    "images": [package_url(cid)],
                },
            )
    return list(found.values())
