"""One reader module per site. The runner calls fetch_account only."""

from __future__ import annotations

from avmon.http import FetchError
from avmon.sources.model import Item, Parsed

from . import avfan, instagram, javforum, oshiseku, tiktok, x, youtube

# Chip order on the feed. A new site is a module plus one entry here.
READERS = (youtube, x, oshiseku, javforum, avfan, tiktok, instagram)

SITES = tuple((reader.SITE, reader.LABEL, reader.BLURB) for reader in READERS)
AUTHOR = {reader.SITE: reader.AUTHOR for reader in READERS}
MARK = {reader.SITE: reader.MARK for reader in READERS}

_BY_SITE = {reader.SITE: reader for reader in READERS}


def fetch_account(site: str, url: str, native_id: str | None) -> Parsed:
    reader = _BY_SITE.get(site)
    if reader is None:
        raise FetchError(f"no parser for {site}")
    return reader.fetch(url, native_id)


__all__ = [
    "AUTHOR",
    "Item",
    "MARK",
    "Parsed",
    "READERS",
    "SITES",
    "fetch_account",
]
