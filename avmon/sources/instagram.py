"""Instagram profile check. A logged-out page has no public post list."""

from __future__ import annotations

from avmon.http import get
from avmon.sources.htmltext import page_title
from avmon.sources.model import Parsed

SITE = "instagram"
LABEL = "Instagram"
BLURB = "Posts from the public profile"
MARK = "IG"
AUTHOR = "@yua_mikami"


def fetch(url: str, native_id: str | None) -> Parsed:
    del native_id
    status, final_url, body = get(url)
    title = page_title(body)
    has_posts = "shortcode" in body and "edge_owner_to_timeline_media" in body
    if not has_posts:
        note = (
            f"Instagram returned title {title!r} and no public post list. "
            "No posts were stored."
        )
        return Parsed(status, final_url, [], note=note)
    return Parsed(status, final_url, [], note="Instagram post markup was present but this check does not parse it yet")
