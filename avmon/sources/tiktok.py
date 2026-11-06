"""TikTok profile check. The video page is a WAF challenge, so no posts are stored."""

from __future__ import annotations

import json

from avmon.http import FetchError, get
from avmon.sources.model import Parsed

SITE = "tiktok"
LABEL = "TikTok"
BLURB = "Posts from the public profile"
MARK = "TT"
AUTHOR = "@yua_mikami"


def fetch(url: str, native_id: str | None) -> Parsed:
    del native_id
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
