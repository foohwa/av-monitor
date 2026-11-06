"""Small HTML text helpers shared by more than one reader."""

from __future__ import annotations

import re
from html import unescape


def plain(html: str) -> str:
    text = unescape(html)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def page_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return plain(match.group(1)) if match else ""


def link_label(inner: str) -> str:
    text = plain(inner)
    labelled = re.findall(r'(?:alt|title)="([^"]+)"', inner)
    labelled = [unescape(value).strip() for value in labelled if value.strip()]
    if not labelled:
        return text
    best = max(labelled, key=len)
    return best if len(best) > len(text) else text
