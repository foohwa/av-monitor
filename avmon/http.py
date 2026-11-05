"""Small HTTP getter for public pages. It does not solve challenges."""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def get(url: str, accept: str = "text/html,application/xhtml+xml") -> tuple[int, str, str]:
    """Return status, final URL, and decoded body."""
    request = urllib.request.Request(
        _ascii_url(url),
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "ja,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(4_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, response.geturl(), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(200_000).decode("utf-8", errors="replace")
        raise FetchError(f"HTTP {exc.code} for {url}: {body[:180]}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"request failed for {url}: {exc.reason}") from exc


def _ascii_url(url: str) -> str:
    """Percent-encode non-ASCII path characters. urllib rejects a raw IRI."""
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%")
    query = quote(parts.query, safe="=&?/%")
    return urlunsplit((parts.scheme, parts.netloc.encode("idna").decode("ascii"), path, query, ""))
