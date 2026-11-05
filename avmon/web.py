"""Scrolling feed over the monitor database. Localhost only."""

from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from avmon.db import DB_PATH, connect

SITES = (
    ("youtube", "YouTube", "Uploads from the channel RSS"),
    ("x", "X", "Posts on the logged-out profile"),
    ("oshiseku", "Oshiseku", "Works embedded on the profile page"),
    ("javforum", "Jav-forum", "Posts in her thread"),
    ("avfan", "avfan", "Titles on the tag page"),
    ("tiktok", "TikTok", "Posts from the public profile"),
    ("instagram", "Instagram", "Posts from the public profile"),
)

AUTHOR = {
    "youtube": "ゆあちゃんねる",
    "x": "@yua_mikami",
    "oshiseku": "推しセク",
    "javforum": "Jav-forum",
    "avfan": "AV女優FAN",
    "tiktok": "@yua_mikami",
    "instagram": "@yua_mikami",
}

MARK = {
    "youtube": "YT",
    "x": "X",
    "oshiseku": "推",
    "javforum": "掲",
    "avfan": "FAN",
    "tiktok": "TT",
    "instagram": "IG",
}

KIND = {
    "upload": "Upload",
    "post": "Post",
    "forum_post": "Forum post",
    "work_sighting": "Work",
}


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"feed   http://{host}:{port}/")
    print(f"data   {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


class Missing(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        wanted = parse_qs(split.query).get("site", [""])[0]
        try:
            if path == "/":
                body = render_feed(wanted)
            elif path.startswith("/b/"):
                body = render_feed(path.removeprefix("/b/").strip("/"))
            elif path.startswith("/t/"):
                body = render_thread(path.removeprefix("/t/"))
            else:
                self._send(404, page("Not found", "<p class='miss'>That page is not on this feed.</p>"))
                return
        except Missing:
            self._send(404, page("Not found", "<p class='miss'>That page is not on this feed.</p>"))
            return
        except Exception as exc:
            self._send(500, page("Error", f"<p class='miss'>{escape(str(exc))}</p>"))
            return
        self._send(200, body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def export_static(dest: str) -> int:
    """Write the current feed as files another person can open without the server."""
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    for path in root.rglob("*.html"):
        path.unlink()
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        if not any(path.iterdir()):
            path.rmdir()

    data = _load()
    pages: list[tuple[Path, str, int]] = [(root / "index.html", render_feed(""), 0)]
    for site, _label, _blurb in SITES:
        pages.append((root / site / "index.html", render_feed(site), 1))
    for row in data["rows"]:
        pages.append((
            root / "t" / row["site"] / row["external_id"] / "index.html",
            render_thread(f"{row['site']}/{row['external_id']}"),
            3,
        ))
    for path, html, depth in pages:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_relativize(html, depth), encoding="utf-8")
    (root / ".nojekyll").write_text("", encoding="utf-8")
    return len(pages)


def _relativize(html: str, depth: int) -> str:
    prefix = "../" * depth if depth else "./"

    def site_link(match: re.Match[str]) -> str:
        return f"href='{prefix}{match.group(1)}/'"

    def thread_link(match: re.Match[str]) -> str:
        return f"href='{prefix}t/{match.group(1)}/{match.group(2)}/'"

    html = re.sub(r"href='/\?site=([a-z]+)'", site_link, html)
    html = re.sub(r"href='/t/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)'", thread_link, html)
    html = html.replace("href='/'", f"href='{prefix}'")
    html = html.replace('href="/"', f'href="{prefix}"')
    return html


def render_feed(site: str) -> str:
    if site and _site(site) is None:
        raise Missing()
    data = _load()
    rows = [row for row in data["rows"] if not site or row["site"] == site]
    dated = [row for row in rows if row["published_at"]]
    undated = [row for row in rows if not row["published_at"]]
    parts = [_profile(data)]
    if site:
        account = data["accounts"].get(site)
        if account and account["last_error"]:
            parts.append(f"<p class='note'>{escape(account['last_error'])}</p>")
    if not rows:
        label = _site(site)[0] if site else "this feed"
        parts.append(f"<p class='miss'>No posts stored for {escape(label)}.</p>")
    else:
        parts.append("<div class='feed'>")
        parts.extend(_card(row, data) for row in dated)
        if dated and undated:
            parts.append("<h2 class='split'>Works with no publish date</h2>")
        elif undated:
            parts.append("<p class='note'>These rows have no publish date.</p>")
        parts.extend(_card(row, data) for row in undated)
        parts.append("</div>")
    title = _site(site)[0] if site else data["name"]
    return page(title, "".join(parts), nav=_filters(data, site))


def render_thread(rest: str) -> str:
    if "/" not in rest:
        raise Missing()
    site, external_id = rest.split("/", 1)
    if _site(site) is None or not external_id or "/" in external_id:
        raise Missing()
    data = _load()
    row = next(
        (item for item in data["rows"] if item["site"] == site and item["external_id"] == external_id),
        None,
    )
    if row is None:
        raise Missing()
    label = _site(site)[0]
    related = _related(row, data["rows"])
    html = (
        f"<p class='crumb'><a href='{_query(site)}'>{escape(label)}</a></p>"
        + _card(row, data)
        + "<h2 class='split'>Same media on another source</h2>"
        + (
            "".join(_card(item, data) for item in related)
            or "<p class='quiet'>No other stored post points at this one.</p>"
        )
    )
    return page(_clip(_title(row), 48), html, nav=_filters(data, site))


def page(title: str, inner: str, nav: str = "") -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · 三上悠亜</title>
<style>
:root {{
  --ink: #1c1915;
  --muted: #5e584f;
  --line: #ddd6c8;
  --paper: #f6f1e7;
  --card: #fffdf8;
  --bar: #241f1a;
  --seal: #8d2f2a;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 16px/1.5 "Hiragino Sans", "Yu Gothic", "Noto Sans JP", sans-serif;
}}
a {{ color: var(--seal); text-underline-offset: 2px; }}
a:focus-visible, button:focus-visible {{ outline: 2px solid var(--seal); outline-offset: 2px; }}
.top {{
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--bar);
  color: var(--paper);
}}
.top-inner {{ max-width: 640px; margin: 0 auto; padding: 12px 16px 0; }}
.brand {{ color: inherit; text-decoration: none; font-weight: 650; font-size: 18px; }}
.filters {{
  display: flex;
  flex-wrap: wrap;
  gap: 0 2px;
  padding: 6px 0 0;
}}
.filters a {{
  flex: 0 0 auto;
  color: #f6f1e7;
  text-decoration: none;
  padding: 8px 10px 10px;
  font-size: 14px;
  border-bottom: 2px solid transparent;
}}
.filters a[aria-current="page"] {{ border-bottom-color: #f6f1e7; font-weight: 650; }}
.filters .count {{ color: #c8bfb2; font-size: 12px; margin-left: 4px; }}
.wrap {{ max-width: 640px; margin: 0 auto; padding: 14px 16px 64px; }}
.whoami {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin: 0 0 8px;
  color: var(--muted);
  font-size: 14px;
}}
.whoami strong {{ color: var(--ink); font-size: 18px; font-weight: 650; }}
.crumb {{ margin: 0 0 8px; font-size: 14px; }}
.note, .miss {{
  background: #fff8e8;
  border: 1px solid #e4d3a4;
  padding: 10px 12px;
  margin: 8px 0 12px;
}}
.quiet {{ color: var(--muted); }}
.feed {{ display: flex; flex-direction: column; }}
.card {{
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 10px;
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
}}
.mark {{
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  background: var(--bar);
  color: var(--paper);
  font-size: 13px;
  font-weight: 700;
}}
.mark.youtube {{ background: #8d2f2a; }}
.mark.x {{ background: #1c1915; }}
.mark.oshiseku {{ background: #6b5344; }}
.mark.javforum {{ background: #3e5344; }}
.mark.avfan {{ background: #3e4a5c; }}
.head {{
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  align-items: baseline;
}}
.head strong {{ font-weight: 650; }}
.head time {{ margin-left: auto; color: var(--muted); font-size: 13px; }}
.kind {{ color: var(--muted); font-size: 13px; }}
.tag {{
  display: inline-block;
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--ink);
  text-decoration: none;
  font-size: 12px;
  line-height: 1.5;
  padding: 0 6px;
}}
.tag.youtube {{ color: #8d2f2a; border-color: #e2c4c1; }}
.tag.x {{ color: #1c1915; }}
.tag.oshiseku {{ color: #6b5344; border-color: #e0d2c6; }}
.tag.javforum {{ color: #3e5344; border-color: #cdd6ce; }}
.tag.avfan {{ color: #3e4a5c; border-color: #cfd5de; }}
.text {{
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 8px 0;
}}
.shots {{
  display: grid;
  gap: 4px;
  margin: 8px 0;
}}
.shots.n1 {{ grid-template-columns: minmax(0, 1fr); }}
.shots.n2, .shots.n3 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.shots.n3 a:first-child {{ grid-column: 1 / -1; }}
.shots a {{
  display: block;
  min-width: 0;
  border: 1px solid var(--line);
  background: var(--bar);
}}
.shots img, .shots video {{
  width: 100%;
  height: auto;
  display: block;
  background: var(--bar);
}}
.shots a.video, .shots .video {{
  position: relative;
}}
.play {{
  position: absolute;
  left: 8px;
  bottom: 8px;
  background: rgba(28, 25, 21, .82);
  color: var(--paper);
  font-size: 12px;
  line-height: 1.4;
  padding: 2px 8px;
}}
.player {{
  margin: 8px 0;
  border: 1px solid var(--line);
  background: var(--bar);
}}
.player iframe {{
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  border: 0;
}}
.player video {{
  display: block;
  width: 100%;
  max-height: 640px;
  background: #000;
}}
.player-caption {{
  display: block;
  padding: 8px 10px;
  color: var(--paper);
  text-decoration: none;
}}
.clip {{
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 8px 0;
  padding: 12px;
  border: 1px solid var(--line);
  background: var(--bar);
  color: var(--paper);
  text-decoration: none;
}}
.clip .markplay {{
  width: 36px;
  height: 36px;
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  border: 1px solid var(--paper);
  border-radius: 50%;
  font-size: 12px;
}}
.clip span:last-child {{ display: block; }}
.clip small {{ color: #c8bfb2; }}
.media {{
  display: grid;
  grid-template-columns: 168px minmax(0, 1fr);
  border: 1px solid var(--line);
  background: var(--card);
  color: inherit;
  text-decoration: none;
  margin: 8px 0;
  min-width: 0;
}}
.media img {{
  width: 100%;
  height: 100%;
  min-height: 94px;
  aspect-ratio: 16 / 9;
  object-fit: cover;
  background: var(--bar);
  display: block;
}}
.media.work {{ grid-template-columns: 112px minmax(0, 1fr); }}
.media.plain {{ grid-template-columns: minmax(0, 1fr); }}
.code {{
  display: grid;
  place-items: center;
  padding: 8px;
  background: var(--bar);
  color: var(--paper);
  font-weight: 700;
  text-align: center;
  overflow-wrap: anywhere;
}}
.media-copy {{
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px 12px;
  min-width: 0;
}}
.media-copy strong {{ overflow-wrap: anywhere; }}
.domain {{ color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
.also, .links {{ margin: 4px 0 0; font-size: 14px; }}
.also a, .links a {{ margin-right: 10px; }}
.split {{ font-size: 15px; font-weight: 650; margin: 18px 0 0; }}
@media (max-width: 720px) {{
  .head time {{ margin-left: 0; }}
  .media, .media.work {{ grid-template-columns: minmax(0, 1fr); }}
  .code {{ min-height: 56px; }}
}}
</style>
</head>
<body>
<div class="top">
  <div class="top-inner">
    <a class="brand" href="/">三上悠亜</a>
    {nav}
  </div>
</div>
<div class="wrap">
{inner}
</div>
</body>
</html>
"""


def _load() -> dict:
    con = connect()
    person = con.execute("SELECT * FROM person ORDER BY id LIMIT 1").fetchone()
    facts = {
        row["field"]: row["value"]
        for row in con.execute("SELECT field, value FROM profile_fact")
    }
    accounts = {
        row["site"]: row for row in con.execute("SELECT * FROM account")
    }
    rows = con.execute(
        """
        SELECT * FROM activity
        ORDER BY CASE WHEN published_at IS NULL OR published_at = '' THEN 1 ELSE 0 END,
                 published_at DESC, id DESC
        """
    ).fetchall()
    con.close()
    videos = {row["external_id"]: row for row in rows if row["site"] == "youtube"}
    posted_on_x: dict[str, list] = {}
    for row in rows:
        if row["site"] != "x" or not row["outbound_url"]:
            continue
        video = _youtube_id(row["outbound_url"])
        if video:
            posted_on_x.setdefault(video, []).append(row)
    counts = {site: 0 for site, _label, _blurb in SITES}
    for row in rows:
        counts[row["site"]] = counts.get(row["site"], 0) + 1
    return {
        "name": person["name_jp"] if person else "三上悠亜",
        "en": person["name_en"] if person else "",
        "facts": facts,
        "accounts": accounts,
        "rows": rows,
        "videos": videos,
        "posted_on_x": posted_on_x,
        "counts": counts,
    }


def _profile(data: dict) -> str:
    facts = data["facts"]
    bits = [
        f"<strong>{escape(data['name'])}</strong>",
        f"<span>{escape(data['en'])}</span>" if data["en"] else "",
    ]
    for bit in (
        facts.get("よみがな"),
        facts.get("誕生日"),
        f"{facts['身長']} cm" if facts.get("身長") else None,
        _sizes(facts),
    ):
        if bit:
            bits.append(f"<span>{escape(bit)}</span>")
    return f"<p class='whoami'>{''.join(bits)}</p>"


def _filters(data: dict, current: str) -> str:
    total = sum(data["counts"].values())
    links = [
        _filter_link("/", "All", total, current == ""),
    ]
    for site, label, _blurb in SITES:
        links.append(_filter_link(_query(site), label, data["counts"].get(site, 0), current == site))
    return "<nav class='filters' aria-label='Source'>" + "".join(links) + "</nav>"


def _filter_link(href: str, label: str, count: int, on: bool) -> str:
    current = " aria-current='page'" if on else ""
    return (
        f"<a href='{href}'{current}>{escape(label)}"
        f"<span class='count'>{count}</span></a>"
    )


def _card(row, data: dict) -> str:
    site = row["site"]
    label = _site(site)[0]
    extra = _extra(row["extra_json"])
    text = _body_text(row)
    media = _media_html(row, extra, data)
    also = _also_html(row, data)
    return f"""
    <article class='card' id='post-{escape(site)}-{escape(row['external_id'])}' data-site='{escape(site)}'>
      <div class='mark {escape(site)}' aria-hidden='true'>{escape(MARK.get(site, site[:2].upper()))}</div>
      <div class='main'>
        <header class='head'>
          <strong>{escape(_author(site, row))}</strong>
          <a class='tag {escape(site)}' href='{_query(site)}'>{escape(label)}</a>
          <span class='kind'>{escape(KIND.get(row['kind'], row['kind']))}</span>
          <time datetime='{escape(row['published_at'] or '')}'>{escape(_when(row['published_at']))}</time>
        </header>
        {f"<p class='text'>{escape(text)}</p>" if text else ""}
        {media}
        {also}
        <p class='links'><a href='{escape(row['url'])}'>Open original</a></p>
      </div>
    </article>
    """


def _body_text(row) -> str:
    body = (row["body"] or "").strip()
    title = (row["title"] or "").strip()
    if not body or body == title:
        return ""
    return body


def _media_html(row, extra: dict, data: dict) -> str:
    site = row["site"]
    if site == "youtube":
        return _yt_player(row["external_id"], row["title"] or row["external_id"], row["url"])
    parts: list[str] = []
    gallery = _gallery_html(_fit_media(_media_items(extra)))
    if gallery:
        parts.append(gallery)
    video = _youtube_id(row["outbound_url"] or "")
    if video:
        known = data["videos"].get(video)
        title = known["title"] if known and known["title"] else video
        parts.append(_yt_player(video, title, f"https://www.youtube.com/watch?v={video}"))
        return "".join(parts)
    if row["outbound_url"]:
        parts.append(
            _plain_media(row["outbound_url"], _domain(row["outbound_url"]) or "Link", "Link", row["outbound_url"])
        )
        return "".join(parts)
    if row["kind"] == "work_sighting":
        code = str(extra.get("code_key") or "No code")
        parts.append(_work_media(row["url"], code, row["title"] or row["external_id"], site))
        return "".join(parts)
    if extra.get("clip"):
        play = extra.get("play") if isinstance(extra.get("play"), str) else ""
        parts.append(_clip_html(row["url"], play))
    if parts:
        return "".join(parts)
    if site == "x" and "t.co/" in (row["body"] or ""):
        return _plain_media(
            row["url"],
            "Photo on the original post",
            "X",
            "The logged-out page did not include the image file.",
        )
    if extra.get("has_media"):
        return _plain_media(
            row["url"],
            "Media attached on the original post",
            "Jav-forum",
            "The file stays on the forum.",
        )
    return ""


def _image_urls(value) -> list[str]:
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        url = item.strip()
        if url.startswith("https://") and url not in urls:
            urls.append(url)
        if len(urls) == 8:
            break
    return urls


def _media_items(extra: dict) -> list[dict]:
    raw = extra.get("media")
    if isinstance(raw, list) and raw:
        items = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind == "image" and isinstance(item.get("url"), str):
                items.append(item)
            elif kind == "video":
                items.append(item)
        if items:
            return items
    return [{"kind": "image", "url": url} for url in _image_urls(extra.get("images"))]


def _fit_media(items: list[dict], limit: int = 8) -> list[dict]:
    kept = list(items)
    while len(kept) > limit:
        image_at = next((index for index in range(len(kept) - 1, -1, -1) if kept[index].get("kind") == "image"), None)
        if image_at is None:
            kept.pop()
        else:
            kept.pop(image_at)
    return kept


def _gallery_html(items: list[dict]) -> str:
    if not items:
        return ""
    kind = "n1" if len(items) == 1 else "n3" if len(items) == 3 else "n2"
    cells = "".join(_gallery_cell(item) for item in items)
    if not cells:
        return ""
    return f"<div class='shots {kind}'>{cells}</div>"


def _gallery_cell(item: dict) -> str:
    if item.get("kind") == "video":
        poster = item.get("poster") if isinstance(item.get("poster"), str) else ""
        play = item.get("play") if isinstance(item.get("play"), str) else ""
        href = item.get("href") if isinstance(item.get("href"), str) else ""
        if play.startswith("https://") and not _blocked_video(play):
            poster_attr = f" poster='{escape(poster)}'" if poster.startswith("https://") else ""
            return (
                "<div class='video'>"
                f"<video controls playsinline preload='none' referrerpolicy='no-referrer'{poster_attr}>"
                f"<source src='{escape(play)}'>"
                "</video>"
                "<span class='play'>Video</span>"
                "</div>"
            )
        if poster.startswith("https://"):
            target = href if href.startswith("https://") else poster
            seconds = item.get("seconds")
            label = "Video"
            if isinstance(seconds, int) and seconds > 0:
                label = f"Video {seconds // 60}:{seconds % 60:02d}"
            return (
                f"<a class='video' href='{escape(target)}'>"
                f"<img src='{escape(poster)}' alt='Video' loading='lazy' referrerpolicy='no-referrer'>"
                f"<span class='play'>{escape(label)}</span></a>"
            )
        return ""
    url = item.get("url") if isinstance(item.get("url"), str) else ""
    if not url.startswith("https://"):
        return ""
    return (
        f"<a href='{escape(url)}'>"
        f"<img src='{escape(url)}' alt='Photo' loading='lazy' referrerpolicy='no-referrer'>"
        "</a>"
    )


def _blocked_video(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host.endswith("dmm.co.jp") or "dmm.com" in host


def _yt_player(video_id: str, title: str, href: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id or ""):
        return _plain_media(href, title, "YouTube", "youtube.com")
    src = f"https://www.youtube-nocookie.com/embed/{video_id}"
    return (
        "<div class='player'>"
        f"<iframe src='{escape(src)}' title='{escape(title)}' loading='lazy' "
        "allow='accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share' "
        "allowfullscreen referrerpolicy='strict-origin-when-cross-origin'></iframe>"
        f"<a class='player-caption' href='{escape(href)}'>{escape(title)}</a>"
        "</div>"
    )


def _clip_html(href: str, play: str = "") -> str:
    if play.startswith("https://") and not _blocked_video(play):
        return (
            "<div class='player'>"
            f"<video controls playsinline preload='metadata' referrerpolicy='no-referrer' src='{escape(play)}'></video>"
            f"<a class='player-caption' href='{escape(play)}'>Play the video</a>"
            "</div>"
        )
    return (
        f"<a class='clip' href='{escape(href)}'>"
        "<span class='markplay'>Play</span>"
        "<span><strong>Video on the original post</strong><br>"
        "<small>The file stays on the forum.</small></span></a>"
    )


def _work_media(href: str, code: str, title: str, site: str) -> str:
    label = _site(site)[0]
    return (
        f"<a class='media work' href='{escape(href)}'>"
        f"<span class='code'>{escape(code)}</span>"
        f"<span class='media-copy'><span class='tag {escape(site)}'>{escape(label)}</span>"
        f"<strong>{escape(title)}</strong>"
        f"<span class='domain'>{escape(_domain(href))}</span></span></a>"
    )


def _plain_media(href: str, title: str, source: str, detail: str) -> str:
    return (
        f"<a class='media plain' href='{escape(href)}'>"
        f"<span class='media-copy'><span class='tag'>{escape(source)}</span>"
        f"<strong>{escape(title)}</strong>"
        f"<span class='domain'>{escape(detail)}</span></span></a>"
    )


def _also_html(row, data: dict) -> str:
    if row["site"] != "youtube":
        return ""
    posts = data["posted_on_x"].get(row["external_id"]) or []
    if not posts:
        return ""
    links = "".join(
        f"<a href='/t/x/{escape(post['external_id'])}'>{escape(_when(post['published_at']))}</a>"
        for post in posts
    )
    return f"<p class='also'>Same video on X {links}</p>"


def _related(row, rows) -> list:
    if row["site"] == "youtube":
        return [
            item for item in rows
            if item["site"] == "x" and row["external_id"] in (item["outbound_url"] or "")
        ]
    video = _youtube_id(row["outbound_url"] or "")
    if not video:
        return []
    return [item for item in rows if item["site"] == "youtube" and item["external_id"] == video]


def _query(site: str) -> str:
    return f"/?site={site}" if site else "/"


def _site(site: str) -> tuple[str, str] | None:
    for key, label, blurb in SITES:
        if key == site:
            return label, blurb
    return None


def _title(row) -> str:
    extra = _extra(row["extra_json"]) if "extra_json" in row.keys() else {}
    code = extra.get("code_key")
    title = (row["title"] or "").strip()
    if code and title:
        return f"{code}  {title}"
    if title:
        return title
    body = (row["body"] or "").strip().splitlines()
    return body[0] if body else row["external_id"]


def _author(site: str, row) -> str:
    extra = _extra(row["extra_json"]) if "extra_json" in row.keys() else {}
    if extra.get("author"):
        return str(extra["author"])
    return AUTHOR.get(site, site)


def _extra(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _sizes(facts: dict) -> str | None:
    bust, waist, hip = facts.get("バスト"), facts.get("ウェスト"), facts.get("ヒップ")
    if not any((bust, waist, hip)):
        return None
    return " / ".join(bit for bit in (bust, waist, hip) if bit)


def _when(value: str | None) -> str:
    if not value:
        return "No date"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    return parsed.strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _youtube_id(url: str) -> str | None:
    if "youtu.be/" in url:
        return url.split("youtu.be/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    if "v=" in url:
        return url.split("v=", 1)[1].split("&", 1)[0]
    return None


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host.removeprefix("www.")
