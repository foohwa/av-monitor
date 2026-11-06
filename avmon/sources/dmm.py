"""Public DMM package image and product code. Shared by work pages."""

from __future__ import annotations

import re


def package_url(cid: str) -> str:
    """Public package image. The path is fixed from the content id."""
    return f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg"


def code_key(cid: str) -> str:
    match = re.fullmatch(r"([a-z]+)(\d+)", cid.lower())
    if match is None:
        return cid.upper()
    return f"{match.group(1).upper()}-{int(match.group(2))}"
