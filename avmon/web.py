"""Forum-style feed over the monitor database. Served on localhost or exported as static files."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from avmon.db import DB_PATH, connect
from avmon.sources import AUTHOR, SITES

KIND = {
    "upload": ("上传", "Upload"),
    "post": ("动态", "Post"),
    "forum_post": ("论坛帖子", "Forum post"),
    "work_sighting": ("作品", "Work"),
}

# Chinese source descriptions. The English ones live in each reader module as BLURB.
BLURB_ZH = {
    "youtube": "频道 RSS 中的上传视频",
    "x": "未登录主页上的帖子",
    "oshiseku": "主页上收录的作品",
    "javforum": "专帖中的回复",
    "avfan": "标签页上的作品",
    "tiktok": "公开主页上的动态",
    "instagram": "公开主页上的动态",
}


def _t(zh: str, en: str) -> str:
    """Interface text in both languages. CSS shows the one picked in the language toggle."""
    return _th(escape(zh), escape(en))


def _th(zh_html: str, en_html: str) -> str:
    """Like _t, for text that already contains markup."""
    return f"<span class='i18n' lang='zh-Hans'>{zh_html}</span><span class='i18n' lang='en'>{en_html}</span>"


def _attr(name: str, zh: str, en: str) -> str:
    """An attribute (aria-label, title, alt) in Chinese, with both versions kept for the script to swap."""
    return f"{name}='{escape(zh)}' data-{name}-zh='{escape(zh)}' data-{name}-en='{escape(en)}'"

# The server renders the first page of every feed so it reads without the script.
# The script then pages through /feed.json and /cards/<n>.json for infinite scroll.
FIRST_PAGE = 12
CHUNK = 24
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

# Every internal link is root-absolute, single-quoted, and ends in a slash: href='/x/'.
# _relativize (static pages) and the script (cards loaded later) both rely on that form.


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
        try:
            if path == "/feed.json":
                self._send(200, feed_index(_load()), "application/json")
                return
            chunk = re.fullmatch(r"/cards/(\d+)\.json", path)
            if chunk:
                self._send(200, feed_chunk(int(chunk.group(1)), _load()), "application/json")
                return
            body = route(path, parse_qs(split.query).get("site", [""])[0])
        except Missing:
            self._send(404, _not_found())
            return
        except Exception as exc:
            self._send(500, page("出错了", f"<p class='quiet'>{escape(str(exc))}</p>", title_en="Error"))
            return
        self._send(200, body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, text: str, kind: str = "text/html") -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def route(path: str, site: str = "", data: dict | None = None) -> str:
    parts = [part for part in path.split("/") if part]
    if not parts:
        return render_feed(site, data)
    if len(parts) == 1:
        return render_feed(parts[0], data)
    if len(parts) == 2 and parts[0] == "a":
        return render_actor(parts[1], data)
    if len(parts) == 3 and parts[0] == "t":
        return render_thread(parts[1], parts[2], data)
    raise Missing()


def export_static(dest: str) -> int:
    """Write the current feed as files another person can open without the server."""
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    stale = [*root.rglob("*.html"), *(root / "cards").glob("*.json"), root / "feed.json"]
    for path in stale:
        if path.is_file():
            path.unlink()
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        if not any(path.iterdir()):
            path.rmdir()

    data = _load()
    pages: list[tuple[str, str]] = [("", render_feed("", data))]
    pages += [(f"{site}/", render_feed(site, data)) for site, _label, _blurb in SITES]
    pages += [(f"a/{pid}/", render_actor(str(pid), data)) for pid in data["people"]]
    pages += [
        (f"t/{row['site']}/{row['external_id']}/", render_thread(row["site"], row["external_id"], data))
        for row in data["rows"]
    ]
    for rel, html in pages:
        path = root / rel / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_relativize(html, rel.count("/")), encoding="utf-8")

    (root / "feed.json").write_text(feed_index(data), encoding="utf-8")
    cards = root / "cards"
    cards.mkdir(exist_ok=True)
    for number in range(_chunk_count(data)):
        (cards / f"{number}.json").write_text(feed_chunk(number, data), encoding="utf-8")
    (root / ".nojekyll").write_text("", encoding="utf-8")
    return len(pages)


def _relativize(html: str, depth: int) -> str:
    """Root links become relative, so the export works under any path (a GitHub Pages project)."""
    prefix = "../" * depth or "./"
    return html.replace("href='/", f"href='{prefix}").replace("data-root='/'", f"data-root='{prefix}'")


# ---------------------------------------------------------------- feed data


def feed_index(data: dict) -> str:
    """Every post in feed order: [key, person id, site, has a date]. The script filters this."""
    return _json({"chunk": CHUNK, "posts": data["index"]})


def feed_chunk(number: int, data: dict) -> str:
    if number >= _chunk_count(data):
        raise Missing()
    rows = data["rows"][number * CHUNK:(number + 1) * CHUNK]
    return _json([_post(row, data) for row in rows])


def _chunk_count(data: dict) -> int:
    return max(1, math.ceil(len(data["rows"]) / CHUNK))


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- pages


def render_feed(site: str, data: dict | None = None) -> str:
    if site and _site(site) is None:
        raise Missing()
    data = data or _load()
    rows = [row for row in data["rows"] if not site or row["site"] == site]
    head: list[str] = []
    if site:
        label, blurb = _site(site)
        head.append(
            f"<header class='page-head' data-site='{escape(site)}'><div class='page-head-row'>"
            f"<h1>{_site_icon(site, 'lg')}{escape(label)}</h1>"
            f"<a class='chip' href='/'>{ICON_CLOSE}{_t('全部来源', 'All sources')}</a></div>"
            f"<p>{_t(BLURB_ZH.get(site, blurb), blurb)}</p></header>"
        )
        notes = [a["last_error"] for a in data["accounts"] if a["site"] == site and a["last_error"]]
        head.extend(f"<p class='note'>{escape(note)}</p>" for note in dict.fromkeys(notes))
    head.append(_people_strip(data))
    body = "".join(head) + _feed(rows, data, site=site, tabbed=True)
    if site:
        return page(_site(site)[0], body, rail=_rail(data, site), tab="discover")
    return page("发现", body, rail=_rail(data, site), tab="discover", title_en="Discover")


def render_actor(raw: str, data: dict | None = None) -> str:
    if not raw.isdigit():
        raise Missing()
    data = data or _load()
    pid = int(raw)
    person = data["people"].get(pid)
    if person is None:
        raise Missing()
    name = person["name_jp"]
    facts = data["facts"].get(pid, {})
    rows = [row for row in data["rows"] if row["person_id"] == pid]
    known = [
        ("读音", "Reading", facts.get("よみがな")),
        ("生日", "Born", facts.get("誕生日")),
        ("身高", "Height", f"{facts['身長']} cm" if facts.get("身長") else None),
        ("三围", "Sizes", _sizes(facts)),
        ("爱好", "Hobby", facts.get("趣味")),
    ]
    fact_html = "".join(
        f"<div><dt>{_t(zh, en)}</dt><dd>{escape(value)}</dd></div>" for zh, en, value in known if value
    )
    accounts = "".join(
        f"<a class='chip' data-site='{escape(a['site'])}' href='{escape(a['url'])}' rel='noopener'>"
        f"{_site_icon(a['site'])}{escape(_site(a['site'])[0] if _site(a['site']) else a['site'])}"
        f"{ICON_OUT}</a>"
        for a in data["accounts"]
        if a["person_id"] == pid
    )
    summary = facts.get("summary")
    count = _t(f"{len(rows)} 条动态", f"{len(rows)} posts")
    sub = f"{escape(person['name_en'])} · {count}" if person["name_en"] else count
    profile = (
        f"<section class='profile' lang='ja' style='--hue:{_hue(pid)}'>"
        "<div class='profile-cover' aria-hidden='true'></div>"
        "<div class='profile-in'>"
        f"<div class='profile-top'>{_avatar(pid, name, size='xl', link=False)}{_follow(pid, name)}</div>"
        f"<h1>{escape(name)}</h1><p class='sub'>{sub}</p>"
        + (f"<p class='bio'>{escape(summary)}</p>" if summary else "")
        + (f"<dl class='facts'>{fact_html}</dl>" if fact_html else "")
        + (f"<div class='accounts'>{accounts}</div>" if accounts else "")
        + "</div></section>"
        f"<h2 class='section-label'>{_t('动态', 'Posts')}</h2>"
    )
    body = profile + _feed(rows, data, person=str(pid))
    return page(name, body, rail=_rail(data))


def render_thread(site: str, external_id: str, data: dict | None = None) -> str:
    if _site(site) is None or not SAFE_ID.fullmatch(external_id):
        raise Missing()
    data = data or _load()
    row = next(
        (item for item in data["rows"] if item["site"] == site and item["external_id"] == external_id),
        None,
    )
    if row is None:
        raise Missing()
    pid = row["person_id"]
    name = _name(data, pid)
    linked = _linked(row, data)
    label = _site(site)[0]
    if linked:
        count = len(linked)
        replies = (
            "<h2 class='replies-head'>"
            + _t(f"{count} 条关联帖子", f"{count} linked {'post' if count == 1 else 'posts'}")
            + f"<span>{_t('其他平台上的相同内容', 'Same media on another source')}</span></h2>"
            "<ol class='replies'>" + "".join(f"<li>{_post(item, data)}</li>" for item in linked) + "</ol>"
        )
    else:
        replies = f"<p class='replies-none'>{_t('暂无其他平台的关联帖子。', 'No other stored post links to this one.')}</p>"
    body = (
        f"<nav class='crumbs' {_attr('aria-label', '导航路径', 'Breadcrumb')}>"
        f"<a href='/'>{_t('发现', 'Discover')}</a><span aria-hidden='true'>/</span>"
        f"<a href='/{escape(site)}/'>{escape(label)}</a><span aria-hidden='true'>/</span>"
        f"<span aria-current='page'>{_t('帖子', 'Thread')}</span></nav>"
        f"<div class='thread'>{_post(row, data, full=True)}{replies}</div>"
        f"<a class='back' href='/a/{pid}/'>"
        + _th(f"查看 <span lang='ja'>{escape(name)}</span> 的更多动态", f"More from <span lang='ja'>{escape(name)}</span>")
        + " <span aria-hidden='true'>→</span></a>"
    )
    return page(_clip(_title(row), 48), body, rail=_rail(data))


def _not_found() -> str:
    return page(
        "页面不存在",
        f"<div class='quiet'><p>{_t('本站没有这个页面。', 'That page is not on this feed.')}</p>"
        f"<a class='btn' href='/'>{_t('返回首页', 'Back to the feed')}</a></div>",
        title_en="Not found",
    )


def page(title: str, inner: str, *, rail: str = "", tab: str = "", title_en: str | None = None) -> str:
    tabs = "".join(
        f"<a href='/#{key}' data-tab='{key}'" + (" aria-current='page'" if tab == key else "") + f">{_t(zh, en)}</a>"
        for key, zh, en in (("following", "关注", "Following"), ("discover", "发现", "Discover"))
    )
    aside = f"<aside class='rail' {_attr('aria-label', '演员与来源', 'Actors and sources')}>{rail}</aside>" if rail else ""
    title_zh = f"{title} · 社区"
    title_en = f"{title_en or title} · Forum"
    return (
        "<!doctype html>\n<html lang='zh-Hans' data-lang='zh'>\n<head>\n<meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>\n"
        "<meta name='color-scheme' content='light dark'>\n"
        f"<title data-zh='{escape(title_zh)}' data-en='{escape(title_en)}'>{escape(title_zh)}</title>\n"
        f"<script>{HEAD_JS}</script>\n"
        f"<style>{CSS}</style>\n</head>\n"
        f"<body data-view='{tab}'>\n"
        "<header class='bar'><div class='bar-in'>"
        "<a class='brand' href='/'><span class='brand-mark' aria-hidden='true'>社</span>"
        f"<span class='brand-word'>{_t('社区', 'Forum')}</span></a>"
        f"<nav class='tabs' {_attr('aria-label', '动态', 'Feed')} data-active='{tab}'>{tabs}"
        "<span class='tabs-pill' aria-hidden='true'></span></nav>"
        "<div class='lang needs-js' role='group' aria-label='语言 / Language'>"
        "<button type='button' data-lang='zh' aria-pressed='true' lang='zh-Hans'>中文</button>"
        "<button type='button' data-lang='en' aria-pressed='false' lang='en'>EN</button>"
        "</div></div></header>\n"
        f"<div class='shell{'' if rail else ' solo'}'><main class='col'>{inner}</main>{aside}</div>\n"
        f"<script>{JS}</script>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------- feed pieces


def _feed(rows: list, data: dict, *, site: str = "", person: str = "", tabbed: bool = False) -> str:
    first = rows[:FIRST_PAGE]
    cards: list[str] = []
    previous = None
    for row in first:
        dated = bool(row["published_at"])
        if previous and not dated:
            cards.append(DIVIDER)
        previous = dated
        cards.append(_post(row, data))
    state = "empty" if not rows else "end" if len(rows) <= FIRST_PAGE else "idle"
    noscript = (
        "<noscript><p>"
        + _t(f"仅显示最新 {FIRST_PAGE} 条。开启 JavaScript 以继续浏览。",
             f"Showing the latest {FIRST_PAGE}. Turn on JavaScript to keep scrolling.")
        + "</p></noscript>"
        if len(rows) > FIRST_PAGE
        else ""
    )
    return (
        f"<div class='feed' id='feed' data-root='/' data-version='{data['version']}' "
        f"data-site='{escape(site)}' data-person='{escape(person)}' data-tabs='{1 if tabbed else 0}' "
        f"data-rendered='{len(first)}'>{''.join(cards)}</div>"
        f"<template id='divider-tpl'>{DIVIDER}</template>"
        f"<div class='foot' id='foot' data-state='{state}'>"
        f"<div data-show='loading'><span class='spin' role='status' {_attr('aria-label', '正在加载更多', 'Loading more posts')}></span></div>"
        f"<p data-show='end' class='end'>{_t('已经到底了', 'You’re all caught up')}</p>"
        f"<p data-show='empty' class='quiet'>{_t('这里还没有内容。', 'Nothing here yet.')}</p>"
        f"<p data-show='error'>{_t('加载失败。', 'Couldn’t load more posts.')} "
        f"<button type='button' class='link' data-retry>{_t('重试', 'Try again')}</button></p>"
        + (_nofollow(data) if tabbed else "")
        + noscript
        + "</div>"
    )


DIVIDER = f"<p class='divider'>{_t('无发布日期的作品', 'Works with no publish date')}</p>"


def _post(row, data: dict, *, full: bool = False) -> str:
    site = row["site"]
    eid = row["external_id"]
    pid = row["person_id"]
    name = _name(data, pid)
    label = _site(site)[0] if _site(site) else site
    extra = _extra(row["extra_json"])
    thread = f"/t/{escape(site)}/{escape(eid)}/"

    author = _author(site, row)
    handle = f"<span class='handle'>{escape(author)}</span>" if author and author != name else ""
    head = (
        "<header class='post-head'>"
        f"<a class='who' href='/a/{pid}/'>{escape(name)}</a>{_follow(pid, name, 'post-follow')}"
        f"<a class='src' href='/{escape(site)}/'>{_site_icon(site)}{escape(label)}</a>{handle}"
        f"<a class='when' href='{thread}'>{_time(row['published_at'])}</a>"
        "</header>"
    )

    title_html = ""
    headline = _headline(row, extra)
    if headline:
        code, title = headline
        inner = (f"<span class='code'>{escape(code)}</span>" if code else "") + escape(title)
        tag = "h1" if full else "h2"
        title_html = f"<{tag} class='post-title'>" + (inner if full else f"<a href='{thread}'>{inner}</a>") + f"</{tag}>"

    text = _body_text(row)
    long = not full and _is_long(text)
    text_html = ""
    if text:
        text_html = f"<p class='text{' long' if long else ''}'>{escape(text)}</p>"
        if long:
            text_html += f"<a class='more-link' href='{thread}'>{_t('阅读全文', 'Read the full post')}</a>"

    actions = []
    if not full:
        count = len(_linked(row, data))
        word = _t(f"{count} 条关联", f"{count} linked") if count else _t("帖子", "Thread")
        actions.append(f"<a class='act' href='{thread}'>{ICON_THREAD}<span>{word}</span></a>")
    actions.append(f"<a class='act' href='{escape(row['url'])}' rel='noopener'>{ICON_OUT}<span>{_t('原帖', 'Original')}</span></a>")
    kind_zh, kind_en = KIND.get(row["kind"], (row["kind"], row["kind"]))
    actions.append(f"<span class='kind'>{_t(kind_zh, kind_en)}</span>")

    return (
        f"<article class='post{' op' if full else ''}' lang='ja' id='post-{escape(site)}-{escape(eid)}' "
        f"data-site='{escape(site)}' data-person='{pid}' data-dated='{1 if row['published_at'] else 0}'>"
        f"{_avatar(pid, name)}"
        f"<div class='post-main'>{head}{title_html}{text_html}{_media_html(row, extra, data, full=full)}"
        f"<footer class='post-foot'>{''.join(actions)}</footer></div></article>"
    )


def _person_item(pid: int, data: dict) -> str:
    person = data["people"][pid]
    name = person["name_jp"]
    count = data["person_counts"].get(pid, 0)
    sub = escape(person["name_en"]) if person["name_en"] else _t(f"{count} 条动态", f"{count} posts")
    return (
        "<li class='person'>"
        f"{_avatar(pid, name, size='sm')}"
        f"<a class='person-name' href='/a/{pid}/'><span lang='ja'>{escape(name)}</span><span class='ps'>{sub}</span></a>"
        f"{_follow(pid, name)}</li>"
    )


def _people(data: dict, extra_class: str = "") -> str:
    items = "".join(_person_item(pid, data) for pid in data["people"])
    return f"<ul class='people {extra_class}'>{items}</ul>"


def _people_strip(data: dict) -> str:
    return f"<section class='strip m-only' {_attr('aria-label', '演员', 'Actors')}>{_people(data, 'row')}</section>"


def _nofollow(data: dict) -> str:
    return (
        "<div data-show='nofollow' class='empty'>"
        f"<h2>{_t('关注演员，打造你的动态', 'Follow actors to build this feed')}</h2>"
        f"<p>{_t('你关注的演员在各平台发布的内容会汇集到这里，最新的在最前。', 'Posts from every source they are on land here, newest first.')}</p>"
        f"{_people(data)}"
        f"<a class='btn' href='/#discover' data-tab='discover'>{_t('去发现', 'Browse Discover')}</a>"
        "</div>"
    )


def _rail(data: dict, current: str | None = None) -> str:
    """current: '' on the all-sources feed, a site key on a source feed, None on other pages."""
    total = sum(data["site_counts"].values())
    sources = [_source_row("/", "all", _t("全部来源", "All sources"), total, current == "")]
    for site, label, _blurb in SITES:
        accounts = [a for a in data["accounts"] if a["site"] == site]
        states = {a["last_status"] or "unknown" for a in accounts}
        state = "error" if "error" in states else "ok" if "ok" in states else "empty"
        notes = "; ".join(dict.fromkeys(a["last_error"] for a in accounts if a["last_error"]))
        word_zh, word_en = {"ok": ("正常", "OK"), "empty": ("无内容", "Empty"), "error": ("出错", "Error")}[state]
        tail = f" · {notes}" if notes else ""
        status = (
            f"<span class='state s-{state}' role='img' "
            f"{_attr('aria-label', word_zh + tail, word_en + tail)} {_attr('title', word_zh + tail, word_en + tail)}></span>"
        )
        sources.append(
            _source_row(f"/{site}/", site, escape(label), data["site_counts"].get(site, 0), current == site, status)
        )
    latest = max((a["last_fetched_at"] or "" for a in data["accounts"]), default="")
    fetched = f"<p class='fine'>{_t('最近抓取', 'Last fetch')} · {_time(latest)}</p>" if latest else ""
    return (
        f"<section class='panel'><h2>{_t('演员', 'Actors')}</h2>{_people(data)}</section>"
        f"<section class='panel'><h2>{_t('来源', 'Sources')}</h2><ul class='sources'>{''.join(sources)}</ul></section>"
        f"{fetched}"
    )


def _source_row(href: str, site: str, label_html: str, count: int, on: bool, status: str = "") -> str:
    current = " aria-current='page'" if on else ""
    status = status or "<span class='state none'></span>"
    return (
        f"<li><a href='{href}'{current}>{_site_icon(site)}<span class='name'>{label_html}</span>"
        f"<span class='n'>{count}</span>{status}</a></li>"
    )


def _avatar(pid: int, name: str, *, size: str = "", link: bool = True) -> str:
    cls = f"avatar {size}".strip()
    initial = escape(name[:1] or "?")
    if not link:
        return f"<span class='{cls}' lang='ja' style='--hue:{_hue(pid)}' aria-hidden='true'>{initial}</span>"
    return (
        f"<a class='{cls}' lang='ja' href='/a/{pid}/' style='--hue:{_hue(pid)}' tabindex='-1' aria-hidden='true'>{initial}</a>"
    )


def _follow(pid: int, name: str, kind: str = "follow") -> str:
    return (
        f"<button class='{kind} needs-js' type='button' data-follow='{pid}' aria-pressed='false' "
        f"{_attr('aria-label', f'关注 {name}', f'Follow {name}')}>"
        f"<span class='f-off'>{_t('关注', 'Follow')}</span><span class='f-on'>{_t('已关注', 'Following')}</span>"
        f"<span class='f-un'>{_t('取消关注', 'Unfollow')}</span>"
        "</button>"
    )


def _site_icon(site: str, size: str = "") -> str:
    """Platform glyph on a tile in the platform colour. Unknown sites get their first letter."""
    glyph = SITE_ICONS.get(site)
    if glyph is None:
        label = _site(site)[0] if _site(site) else site
        glyph = f"<b>{escape(label[:1].upper())}</b>"
    cls = f"site-icon {size}".strip()
    return f"<span class='{cls}' data-site='{escape(site)}' aria-hidden='true'>{glyph}</span>"


def _hue(pid: int) -> int:
    return (340 + (pid - 1) * 67) % 360


def _time(value: str | None) -> str:
    if not value:
        return f"<time class='ago' datetime=''>{_t('无日期', 'No date')}</time>"
    return f"<time class='ago' datetime='{escape(value)}'>{escape(_when(value))}</time>"


# ---------------------------------------------------------------- data


def _load() -> dict:
    con = connect()
    people = {row["id"]: row for row in con.execute("SELECT * FROM person ORDER BY id")}
    facts: dict[int, dict[str, str]] = {}
    for row in con.execute("SELECT person_id, field, value FROM profile_fact ORDER BY id"):
        facts.setdefault(row["person_id"], {})[row["field"]] = row["value"]
    accounts = con.execute("SELECT * FROM account ORDER BY id").fetchall()
    rows = [
        row
        for row in con.execute(
            """
            SELECT * FROM activity
            ORDER BY CASE WHEN published_at IS NULL OR published_at = '' THEN 1 ELSE 0 END,
                     published_at DESC, id DESC
            """
        )
        if SAFE_ID.fullmatch(row["external_id"])
    ]
    con.close()
    videos = {row["external_id"]: row for row in rows if row["site"] == "youtube"}
    posted_on_x: dict[str, list] = {}
    for row in rows:
        if row["site"] != "x" or not row["outbound_url"]:
            continue
        video = _youtube_id(row["outbound_url"])
        if video:
            posted_on_x.setdefault(video, []).append(row)
    site_counts: dict[str, int] = {}
    person_counts: dict[int, int] = {}
    for row in rows:
        site_counts[row["site"]] = site_counts.get(row["site"], 0) + 1
        person_counts[row["person_id"]] = person_counts.get(row["person_id"], 0) + 1
    index = [
        [f"{row['site']}/{row['external_id']}", row["person_id"], row["site"], 1 if row["published_at"] else 0]
        for row in rows
    ]
    stamp = _json([index, [row["fetched_at"] for row in rows]]).encode("utf-8")
    return {
        "people": people,
        "facts": facts,
        "accounts": accounts,
        "rows": rows,
        "videos": videos,
        "posted_on_x": posted_on_x,
        "site_counts": site_counts,
        "person_counts": person_counts,
        "index": index,
        "version": hashlib.sha1(stamp).hexdigest()[:10],
    }


def _name(data: dict, pid: int) -> str:
    person = data["people"].get(pid)
    return person["name_jp"] if person else "Unknown"


def _linked(row, data: dict) -> list:
    """Posts of the same media on another source, shown as the thread's replies."""
    if row["site"] == "youtube":
        return data["posted_on_x"].get(row["external_id"], [])
    video = _youtube_id(row["outbound_url"] or "")
    if video and video in data["videos"]:
        return [data["videos"][video]]
    return []


# ---------------------------------------------------------------- media


def _media_html(row, extra: dict, data: dict, *, full: bool = False) -> str:
    site = row["site"]
    if site == "youtube":
        return _yt_player(row["external_id"], row["title"] or row["external_id"], row["url"], caption=False)
    thread = f"/t/{escape(site)}/{escape(row['external_id'])}/"
    gallery = _gallery_html(_fit_media(_media_items(extra)), limit=0 if full else 4, more_href=thread)
    if row["kind"] == "work_sighting":
        if gallery:
            return gallery
        code = escape(str(extra["code_key"])) if extra.get("code_key") else _t("无番号", "No code")
        return _work_media(row["url"], code, row["title"] or row["external_id"])
    parts: list[str] = [gallery] if gallery else []
    video = _youtube_id(row["outbound_url"] or "")
    if video:
        known = data["videos"].get(video)
        title = known["title"] if known and known["title"] else video
        parts.append(_yt_player(video, title, f"https://www.youtube.com/watch?v={video}"))
        return "".join(parts)
    if row["outbound_url"]:
        parts.append(
            _plain_media(
                row["outbound_url"],
                escape(_domain(row["outbound_url"])) or _t("链接", "Link"),
                _t("链接", "Link"),
                escape(row["outbound_url"]),
            )
        )
        return "".join(parts)
    if extra.get("clip"):
        play = extra.get("play") if isinstance(extra.get("play"), str) else ""
        parts.append(_clip_html(row["url"], play))
    if parts:
        return "".join(parts)
    if site == "x" and "t.co/" in (row["body"] or ""):
        return _plain_media(
            row["url"],
            _t("原帖中的图片", "Photo on the original post"),
            "X",
            _t("未登录页面不包含图片文件。", "The logged-out page did not include the image file."),
        )
    if extra.get("has_media"):
        return _plain_media(
            row["url"],
            _t("原帖附带的媒体", "Media attached on the original post"),
            "Jav-forum",
            _t("文件保留在论坛上。", "The file stays on the forum."),
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


def _gallery_html(items: list[dict], *, limit: int = 0, more_href: str = "") -> str:
    cells = [cell for cell in (_gallery_cell(item) for item in items) if cell]
    if not cells:
        return ""
    if limit and len(cells) > limit:
        hidden = len(cells) - limit + 1
        cells = cells[: limit - 1] + [
            f"<div class='cell-more'>{cells[limit - 1]}"
            f"<a class='shots-more' href='{more_href}' {_attr('aria-label', f'帖子中还有 {hidden} 项', f'{hidden} more in the thread')}>+{hidden}</a></div>"
        ]
    kind = "n1" if len(cells) == 1 else "n3" if len(cells) == 3 else "n2"
    return f"<div class='shots {kind}'>{''.join(cells)}</div>"


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
                "</video></div>"
            )
        if poster.startswith("https://"):
            target = href if href.startswith("https://") else poster
            seconds = item.get("seconds")
            label = _t("视频", "Video")
            if isinstance(seconds, int) and seconds > 0:
                label = f"{seconds // 60}:{seconds % 60:02d}"
            return (
                f"<a class='video' href='{escape(target)}'>"
                f"<img src='{escape(poster)}' {_attr('alt', '视频', 'Video')} loading='lazy' referrerpolicy='no-referrer'>"
                f"<span class='play'>{ICON_PLAY}{label}</span></a>"
            )
        return ""
    url = item.get("url") if isinstance(item.get("url"), str) else ""
    if not url.startswith("https://"):
        return ""
    return (
        f"<a href='{escape(url)}'>"
        f"<img src='{escape(url)}' {_attr('alt', '图片', 'Photo')} loading='lazy' referrerpolicy='no-referrer'>"
        "</a>"
    )


def _blocked_video(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host.endswith("dmm.co.jp") or "dmm.com" in host


def _yt_player(video_id: str, title: str, href: str, caption: bool = True) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id or ""):
        return _plain_media(href, escape(title), "YouTube", "youtube.com")
    src = f"https://www.youtube-nocookie.com/embed/{video_id}"
    under = (
        f"<a class='player-caption' href='{escape(href)}'>{_site_icon('youtube')}"
        f"{escape(title)}</a>"
        if caption
        else ""
    )
    return (
        "<div class='player'>"
        f"<iframe src='{escape(src)}' title='{escape(title)}' loading='lazy' "
        "allow='accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share' "
        "allowfullscreen referrerpolicy='strict-origin-when-cross-origin'></iframe>"
        f"{under}</div>"
    )


def _clip_html(href: str, play: str = "") -> str:
    if play.startswith("https://") and not _blocked_video(play):
        return (
            "<div class='player'>"
            f"<video controls playsinline preload='metadata' referrerpolicy='no-referrer' src='{escape(play)}'></video>"
            "</div>"
        )
    return (
        f"<a class='media clip' href='{escape(href)}'>"
        f"<span class='clip-play'>{ICON_PLAY}</span>"
        f"<span class='media-copy'><strong>{_t('原帖中的视频', 'Video on the original post')}</strong>"
        f"<span class='domain'>{_t('文件保留在论坛上。', 'The file stays on the forum.')}</span></span></a>"
    )


def _work_media(href: str, code_html: str, title: str) -> str:
    return (
        f"<a class='media work' href='{escape(href)}'>"
        f"<span class='code-block'>{code_html}</span>"
        f"<span class='media-copy'><strong>{escape(title)}</strong>"
        f"<span class='domain'>{escape(_domain(href))}</span></span></a>"
    )


def _plain_media(href: str, title_html: str, source_html: str, detail_html: str) -> str:
    """Link card. All three text arguments are markup: escape them or build them with _t."""
    return (
        f"<a class='media' href='{escape(href)}'>"
        f"<span class='media-copy'><span class='kicker'>{source_html}</span>"
        f"<strong>{title_html}</strong>"
        f"<span class='domain'>{detail_html}</span></span></a>"
    )


# ---------------------------------------------------------------- text helpers


def _site(site: str) -> tuple[str, str] | None:
    for key, label, blurb in SITES:
        if key == site:
            return label, blurb
    return None


def _headline(row, extra: dict) -> tuple[str, str] | None:
    title = (row["title"] or "").strip()
    if not title:
        return None
    code = str(extra.get("code_key") or "") if row["kind"] == "work_sighting" else ""
    return code, title


def _body_text(row) -> str:
    body = (row["body"] or "").strip()
    title = (row["title"] or "").strip()
    if not body or body == title:
        return ""
    return body


def _is_long(text: str) -> bool:
    return len(text) > 260 or text.count("\n") > 6


def _title(row) -> str:
    extra = _extra(row["extra_json"])
    code = extra.get("code_key")
    title = (row["title"] or "").strip()
    if code and title:
        return f"{code}  {title}"
    if title:
        return title
    body = (row["body"] or "").strip().splitlines()
    return body[0] if body else row["external_id"]


def _author(site: str, row) -> str:
    extra = _extra(row["extra_json"])
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
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10]
    return parsed.strftime("%Y-%m-%d")


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


# ---------------------------------------------------------------- assets

ICON_THREAD = (
    "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4 19.5l1.3-4.6A7.5 7.5 0 1 1 20 11.5Z'/></svg>"
)
ICON_OUT = (
    "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M14 5h5v5M19 5l-8 8M17 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h4'/></svg>"
)
# Brand-style glyphs for the big platforms; simple pictograms for the sites with no known mark.
SITE_ICONS = {
    "all": (
        "<svg viewBox='0 0 24 24' class='solid'><rect x='4' y='4' width='7' height='7' rx='2'/>"
        "<rect x='13' y='4' width='7' height='7' rx='2'/><rect x='4' y='13' width='7' height='7' rx='2'/>"
        "<rect x='13' y='13' width='7' height='7' rx='2'/></svg>"
    ),
    "youtube": "<svg viewBox='0 0 24 24' class='solid'><path d='M8.5 5.8v12.4L19 12 8.5 5.8Z'/></svg>",
    "x": (
        "<svg viewBox='0 0 24 24' class='solid'><path d='M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406"
        "l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z'/></svg>"
    ),
    "tiktok": (
        "<svg viewBox='0 0 24 24' class='solid'><path d='M16.6 2h-3.4v13.3a2.9 2.9 0 1 1-2.9-2.9c.3 0 .6 0 .9.1V9"
        "a6.3 6.3 0 1 0 5.4 6.3V8.6a7.9 7.9 0 0 0 4.4 1.3V6.5a4.4 4.4 0 0 1-4.4-4.5Z'/></svg>"
    ),
    "instagram": (
        "<svg viewBox='0 0 24 24'><rect x='3.5' y='3.5' width='17' height='17' rx='5'/><circle cx='12' cy='12' r='3.8'/>"
        "<circle cx='17.1' cy='6.9' r='1.1' fill='currentColor' stroke='none'/></svg>"
    ),
    "javforum": (
        "<svg viewBox='0 0 24 24'><path d='M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-7l-5 4v-4a2 2 0 0 1-2-2Z'/>"
        "<path d='M8.5 8.5h7M8.5 11.5h4.5'/></svg>"
    ),
    "oshiseku": (
        "<svg viewBox='0 0 24 24' class='solid'><path d='M12 20.3S3.8 15.4 3.8 9.4A4.4 4.4 0 0 1 12 7.2a4.4 4.4 0 0 1 8.2 2.2"
        "c0 6-8.2 10.9-8.2 10.9Z'/></svg>"
    ),
    "avfan": (
        "<svg viewBox='0 0 24 24'><rect x='3.5' y='5' width='17' height='14' rx='2.5'/>"
        "<path d='M8 5v14M16 5v14M3.5 9.7H8M3.5 14.3H8M16 9.7h4.5M16 14.3h4.5'/></svg>"
    ),
}
ICON_CLOSE = "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M7 7l10 10M17 7 7 17'/></svg>"
ICON_PLAY = "<svg viewBox='0 0 24 24' aria-hidden='true' class='solid'><path d='M8 5.5v13l11-6.5-11-6.5Z'/></svg>"

CSS = """
:root {
  --bg: #f5f4f1;
  --surface: #ffffff;
  --surface-2: #efeee9;
  --pill: #ffffff;
  --ink: #17161a;
  --ink-2: #45434b;
  --muted: #807e87;
  --line: rgb(23 22 26 / .08);
  --line-strong: rgb(23 22 26 / .15);
  --accent: #e0483c;
  --accent-ink: #ffffff;
  --accent-soft: rgb(224 72 60 / .1);
  --media: #0d0d0f;
  --shadow: 0 1px 2px rgb(23 22 26 / .04), 0 6px 20px -12px rgb(23 22 26 / .12);
  --radius: 16px;
  --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
  --ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);
  --c-youtube: #e5302a;
  --c-x: #17161a;
  --c-oshiseku: #e08a24;
  --c-javforum: #2b9a68;
  --c-avfan: #3b6dd6;
  --c-tiktok: #10b2c6;
  --c-instagram: #d23f8a;
  --ok: #2b9a68;
  --warn: #e0a024;
  --font-zh: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Noto Sans SC",
    "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  --font-ja: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Noto Sans JP",
    "Yu Gothic", "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e0e10;
    --surface: #17171a;
    --surface-2: #202024;
    --pill: #2c2c32;
    --ink: #ededf0;
    --ink-2: #c3c2c9;
    --muted: #8a8992;
    --line: rgb(255 255 255 / .07);
    --line-strong: rgb(255 255 255 / .13);
    --accent: #ff6a55;
    --accent-ink: #1a0b08;
    --accent-soft: rgb(255 106 85 / .14);
    --media: #000000;
    --shadow: none;
    --c-x: #ededf0;
  }
}
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-padding-top: 76px; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.55 var(--font-zh);
  -webkit-font-smoothing: antialiased;
  -webkit-tap-highlight-color: transparent;
}
a { color: var(--accent); text-underline-offset: 3px; text-decoration-thickness: 1px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 8px; }
::selection { background: var(--accent-soft); }
button { font: inherit; color: inherit; }
svg { width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.8;
  stroke-linecap: round; stroke-linejoin: round; }
svg.solid { fill: currentColor; stroke: none; }
html:not(.js) .needs-js { display: none !important; }

/* Interface text ships in both languages; the toggle picks one. Content stays Japanese (lang=ja). */
html:not([data-lang=en]) .i18n[lang=en], html[data-lang=en] .i18n[lang=zh-Hans] { display: none; }
:lang(ja) { font-family: var(--font-ja); }
:lang(zh-Hans) { font-family: var(--font-zh); }

[data-site=youtube] { --site: var(--c-youtube); }
[data-site=x] { --site: var(--c-x); }
[data-site=oshiseku] { --site: var(--c-oshiseku); }
[data-site=javforum] { --site: var(--c-javforum); }
[data-site=avfan] { --site: var(--c-avfan); }
[data-site=tiktok] { --site: var(--c-tiktok); }
[data-site=instagram] { --site: var(--c-instagram); }
.site-icon {
  width: 18px; height: 18px; border-radius: 6px; display: inline-grid; place-items: center; flex: none;
  background: var(--site, var(--muted)); color: #fff;
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / .1);
}
.site-icon svg { width: 11px; height: 11px; stroke-width: 2.3; }
.site-icon b { font-size: 10px; font-weight: 750; line-height: 1; }
.site-icon[data-site=x] svg { width: 9px; height: 9px; }
.site-icon.lg[data-site=x] svg { width: 15px; height: 15px; }
.site-icon[data-site=x], .site-icon[data-site=tiktok] { background: #111113; }
.site-icon[data-site=instagram] {
  background: radial-gradient(circle at 30% 110%, #ffd776 0%, #f7a34b 18%, #e9425f 45%, #c13584 65%, #6a4bd8 100%);
}
@media (prefers-color-scheme: dark) {
  .site-icon[data-site=x], .site-icon[data-site=tiktok] { background: #ededf0; color: #111113; }
}
.site-icon[data-site=all] { background: var(--ink); color: var(--bg); }
.site-icon.lg { width: 30px; height: 30px; border-radius: 9px; }
.site-icon.lg svg { width: 18px; height: 18px; }

/* top bar */
.bar {
  position: sticky; top: 0; z-index: 20;
  background: color-mix(in srgb, var(--bg) 80%, transparent);
  -webkit-backdrop-filter: saturate(1.6) blur(16px);
  backdrop-filter: saturate(1.6) blur(16px);
  border-bottom: 1px solid var(--line);
}
.bar-in {
  max-width: 1060px; margin: 0 auto; height: 58px; padding: 0 20px;
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 12px;
}
.brand {
  justify-self: start; display: inline-flex; align-items: center; gap: 9px;
  color: var(--ink); text-decoration: none; font-weight: 680; font-size: 16px; letter-spacing: -.015em;
}
.brand-mark {
  width: 28px; height: 28px; border-radius: 9px; display: grid; place-items: center;
  background: var(--accent); color: var(--accent-ink);
  font-size: 15px; font-weight: 700;
  box-shadow: inset 0 -1px 0 rgb(0 0 0 / .14);
}
.tabs {
  position: relative; display: grid; grid-template-columns: 1fr 1fr; padding: 3px;
  border-radius: 999px; background: var(--surface-2); box-shadow: inset 0 0 0 1px var(--line);
}
.tabs a {
  position: relative; z-index: 1; min-width: 108px; padding: 6px 16px; border-radius: 999px;
  text-align: center; font-size: 14px; font-weight: 600; color: var(--muted); text-decoration: none;
  transition: color 160ms ease;
}
.tabs a[aria-current] { color: var(--ink); }
.tabs-pill {
  position: absolute; top: 3px; bottom: 3px; left: 3px; width: calc(50% - 3px);
  border-radius: 999px; background: var(--pill);
  box-shadow: 0 1px 2px rgb(0 0 0 / .1), 0 0 0 1px var(--line);
  opacity: 0;
}
.tabs[data-active=following] .tabs-pill { opacity: 1; }
.tabs[data-active=discover] .tabs-pill { opacity: 1; transform: translateX(100%); }
.tabs.ready .tabs-pill { transition: transform 220ms var(--ease-in-out), opacity 150ms ease; }

.lang {
  justify-self: end; display: flex; gap: 2px; padding: 3px; border-radius: 999px;
  background: var(--surface-2); box-shadow: inset 0 0 0 1px var(--line);
}
.lang button {
  height: 26px; padding: 0 10px; border: 0; border-radius: 999px; background: transparent;
  color: var(--muted); font-size: 12.5px; font-weight: 650; cursor: pointer;
  transition: background-color 150ms ease, color 150ms ease, transform 140ms var(--ease-out);
}
.lang button[aria-pressed=true] { background: var(--pill); color: var(--ink); box-shadow: 0 1px 2px rgb(0 0 0 / .1), 0 0 0 1px var(--line); }
.lang button:active { transform: scale(0.96); }

/* layout */
.shell {
  max-width: 1060px; margin: 0 auto; padding: 22px 20px 96px;
  display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 28px; align-items: start;
}
.shell.solo { grid-template-columns: minmax(0, 1fr); max-width: 700px; }
.col { min-width: 0; width: 100%; max-width: 660px; justify-self: end; }
.shell.solo .col { justify-self: center; }
.rail { position: sticky; top: 80px; display: flex; flex-direction: column; gap: 14px; }

/* feed header */
.page-head { margin: 2px 0 14px; }
.page-head h1 {
  display: flex; align-items: center; gap: 10px; margin: 0;
  font-size: 24px; line-height: 1.2; letter-spacing: -.02em; font-weight: 700;
}
.page-head-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.page-head p { margin: 4px 0 0; color: var(--muted); }
.note {
  margin: 0 0 14px; padding: 10px 14px; border-radius: 12px;
  background: var(--surface); border: 1px solid var(--line);
  color: var(--ink-2); font-size: 13.5px; overflow-wrap: anywhere;
}
.strip { margin: 0 0 14px; }
.people.row { flex-direction: row; gap: 8px; overflow-x: auto; scrollbar-width: none; }
.people.row::-webkit-scrollbar { display: none; }
.people.row .person {
  flex: none; width: 260px; padding: 10px 12px; border-radius: 14px;
  background: var(--surface); border: 1px solid var(--line);
}
body[data-view=following] .strip { display: none; }

/* posts */
.feed { display: flex; flex-direction: column; gap: 10px; }
.post {
  display: grid; grid-template-columns: 40px minmax(0, 1fr); gap: 12px;
  padding: 16px 18px 8px; border-radius: var(--radius);
  background: var(--surface); border: 1px solid var(--line); box-shadow: var(--shadow);
}
.post.enter {
  transition: opacity 260ms var(--ease-out), transform 260ms var(--ease-out);
  transition-delay: calc(var(--i, 0) * 35ms);
}
@starting-style { .post.enter { opacity: 0; transform: translateY(8px); } }
.post-main { min-width: 0; }
.avatar {
  --hue: 340;
  width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; flex: none;
  font-size: 16px; font-weight: 680; text-decoration: none;
  color: oklch(0.99 0.01 var(--hue));
  background: linear-gradient(150deg, oklch(0.72 0.14 var(--hue)), oklch(0.54 0.16 calc(var(--hue) + 28)));
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / .14);
}
.avatar.sm { width: 36px; height: 36px; font-size: 14px; }
.avatar.xl { width: 84px; height: 84px; font-size: 34px; box-shadow: 0 0 0 4px var(--surface); }
.post-head {
  display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px;
  font-size: 14px; line-height: 1.45; min-height: 20px;
}
.who { color: var(--ink); font-weight: 660; text-decoration: none; }
/* Source badge: the icon sits 3px inside a 24px pill, so it is a circle to keep the radii concentric. */
.src {
  display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 10px 0 3px;
  border-radius: 999px; background: var(--surface-2); box-shadow: inset 0 0 0 1px var(--line);
  color: var(--ink-2); font-size: 12.5px; font-weight: 620; text-decoration: none; align-self: center;
  transition: background-color 150ms ease, color 150ms ease, transform 140ms var(--ease-out);
}
.src .site-icon { border-radius: 50%; }
.src:active { transform: scale(0.97); }
.handle { color: var(--muted); }
.when {
  margin-left: auto; color: var(--muted); font-size: 13px; text-decoration: none;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.post-title {
  margin: 4px 0 0; font-size: 16.5px; line-height: 1.4; font-weight: 660; letter-spacing: -.01em;
  text-wrap: pretty; overflow-wrap: anywhere;
}
.post-title a { color: inherit; text-decoration: none; }
.post.op .post-title { font-size: 21px; line-height: 1.35; letter-spacing: -.02em; }
.code {
  display: inline-block; margin-right: 8px; padding: 3px 7px; border-radius: 7px; vertical-align: 2px;
  background: var(--surface-2); color: var(--ink-2);
  font: 600 12px/1.2 ui-monospace, "SF Mono", Menlo, monospace; letter-spacing: 0;
}
.text { margin: 6px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; text-wrap: pretty; }
.text.long { display: -webkit-box; -webkit-line-clamp: 7; -webkit-box-orient: vertical; overflow: hidden; }
.post.op .text { font-size: 16px; }
.more-link { display: inline-block; margin-top: 4px; font-size: 14px; font-weight: 560; text-decoration: none; }

/* media */
.shots {
  display: grid; gap: 3px; margin-top: 12px; border-radius: 14px; overflow: hidden;
  border: 1px solid var(--line); background: var(--line);
}
.shots.n2, .shots.n3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.shots.n3 > :first-child { grid-column: 1 / -1; }
.shots > a, .shots > .video { position: relative; display: block; min-width: 0; background: var(--media); }
.shots img, .shots video { display: block; width: 100%; height: 100%; object-fit: cover; }
.shots.n2 > *, .shots.n3 > * { aspect-ratio: 4 / 3; }
.shots.n3 > :first-child { aspect-ratio: 16 / 9; }
.shots.n1 img, .shots.n1 video { height: auto; max-height: 560px; object-fit: contain; }
.cell-more { position: relative; min-width: 0; background: var(--media); }
.cell-more > :first-child { height: 100%; }
.shots-more {
  position: absolute; inset: 0; display: grid; place-items: center;
  background: rgb(0 0 0 / .52); color: #fff; text-decoration: none;
  font-size: 24px; font-weight: 650; letter-spacing: -.02em; font-variant-numeric: tabular-nums;
  transition: background-color 150ms ease;
}
.play {
  position: absolute; left: 10px; bottom: 10px; display: inline-flex; align-items: center; gap: 4px;
  padding: 4px 9px 4px 7px; border-radius: 999px;
  background: rgb(0 0 0 / .62); -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px);
  color: #fff; font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums;
}
.play svg { width: 12px; height: 12px; }
.player { margin-top: 12px; border-radius: 14px; overflow: hidden; background: var(--media); border: 1px solid var(--line); }
.player iframe { display: block; width: 100%; aspect-ratio: 16 / 9; border: 0; }
.player video { display: block; width: 100%; max-height: 640px; background: #000; }
.player-caption {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  background: var(--surface-2); color: var(--ink); font-size: 14px; font-weight: 560; text-decoration: none;
}
.media {
  display: grid; grid-template-columns: minmax(0, 1fr); margin-top: 12px; min-width: 0;
  border-radius: 14px; overflow: hidden; border: 1px solid var(--line);
  background: var(--surface-2); color: inherit; text-decoration: none;
  transition: border-color 150ms ease;
}
.media.work { grid-template-columns: 120px minmax(0, 1fr); }
.media.clip { grid-template-columns: auto minmax(0, 1fr); align-items: center; padding-left: 14px; }
.media-copy { display: flex; flex-direction: column; gap: 2px; padding: 12px 14px; min-width: 0; }
.media-copy strong { font-weight: 600; overflow-wrap: anywhere; }
.kicker { font-size: 12px; font-weight: 600; color: var(--muted); }
.domain { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.code-block {
  display: grid; place-items: center; padding: 10px; text-align: center; overflow-wrap: anywhere;
  background: var(--ink); color: var(--bg); font: 700 13px/1.2 ui-monospace, "SF Mono", Menlo, monospace;
}
.clip-play {
  width: 38px; height: 38px; border-radius: 50%; display: grid; place-items: center;
  background: var(--accent); color: var(--accent-ink);
}

/* post actions */
.post-foot { display: flex; align-items: center; gap: 2px; margin: 8px 0 0 -10px; }
.act {
  display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 10px; border-radius: 999px;
  color: var(--muted); font-size: 13px; font-weight: 560; text-decoration: none;
  transition: background-color 150ms ease, color 150ms ease, transform 140ms var(--ease-out);
}
.act:active { transform: scale(0.97); }
.kind { margin-left: auto; color: var(--muted); font-size: 12px; }

/* thread */
.crumbs {
  display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 12px;
  font-size: 13.5px; color: var(--muted);
}
.crumbs a { color: var(--ink-2); text-decoration: none; }
.replies-head {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px;
  margin: 26px 0 12px 4px; font-size: 15px; font-weight: 660;
}
.replies-head span { color: var(--muted); font-size: 13.5px; font-weight: 400; }
.replies {
  position: relative; list-style: none; margin: 0; padding: 0 0 0 28px;
  display: flex; flex-direction: column; gap: 10px;
}
.replies::before {
  content: ""; position: absolute; left: 11px; top: -8px; bottom: 34px; width: 2px;
  border-radius: 2px; background: var(--line-strong);
}
.replies > li { position: relative; }
.replies > li::before {
  content: ""; position: absolute; left: -17px; top: 34px; width: 17px; height: 2px;
  border-radius: 2px; background: var(--line-strong);
}
.replies-none { margin: 18px 4px 0; color: var(--muted); font-size: 14px; }
.back {
  display: inline-flex; gap: 6px; margin: 22px 4px 0; font-weight: 560; font-size: 14px; text-decoration: none;
}

/* actor profile */
.profile {
  margin-bottom: 22px; border-radius: var(--radius); overflow: hidden;
  background: var(--surface); border: 1px solid var(--line); box-shadow: var(--shadow);
}
.profile-cover {
  height: 112px;
  background:
    radial-gradient(120% 140% at 0% 0%, oklch(0.78 0.12 var(--hue)) 0%, transparent 60%),
    linear-gradient(120deg, oklch(0.64 0.15 var(--hue)), oklch(0.5 0.15 calc(var(--hue) + 40)));
}
.profile-in { padding: 0 22px 20px; }
.profile-top { display: flex; align-items: flex-end; justify-content: space-between; margin-top: -42px; }
.profile h1 { margin: 12px 0 0; font-size: 24px; line-height: 1.25; letter-spacing: -.02em; }
.profile .sub { margin: 2px 0 0; color: var(--muted); }
.bio {
  margin: 12px 0 0; color: var(--ink-2); font-size: 14px; text-wrap: pretty;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.facts {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 12px 18px;
  margin: 16px 0 0; padding-top: 16px; border-top: 1px solid var(--line);
}
.facts dt { color: var(--muted); font-size: 12px; font-weight: 560; }
.facts dd { margin: 1px 0 0; font-weight: 560; font-size: 14px; }
.accounts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 16px; }
.chip {
  display: inline-flex; align-items: center; gap: 7px; height: 32px; padding: 0 13px;
  border-radius: 999px; border: 1px solid var(--line-strong);
  color: var(--ink-2); font-size: 13.5px; font-weight: 560; text-decoration: none;
  transition: background-color 150ms ease, border-color 150ms ease, color 150ms ease, transform 140ms var(--ease-out);
}
.chip > svg { width: 13px; height: 13px; color: var(--muted); }
.chip:active { transform: scale(0.97); }
.section-label {
  margin: 0 0 10px 4px; color: var(--muted); font-size: 12px; font-weight: 650;
  letter-spacing: .06em; text-transform: uppercase;
}

/* rail */
.panel {
  padding: 14px 16px; border-radius: var(--radius);
  background: var(--surface); border: 1px solid var(--line); box-shadow: var(--shadow);
}
.panel h2 {
  margin: 0 0 12px; color: var(--muted); font-size: 12px; font-weight: 650;
  letter-spacing: .06em; text-transform: uppercase;
}
.people { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.person { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 10px; }
.person-name {
  min-width: 0; color: var(--ink); font-weight: 650; font-size: 14.5px; line-height: 1.3; text-decoration: none;
}
.person-name .ps {
  display: block; color: var(--muted); font-size: 12.5px; font-weight: 400;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sources { list-style: none; margin: 0 -8px; padding: 0; display: flex; flex-direction: column; gap: 1px; }
.sources a {
  display: flex; align-items: center; gap: 10px; padding: 6px 8px; border-radius: 10px;
  color: var(--ink); font-size: 14px; font-weight: 520; text-decoration: none;
  transition: background-color 150ms ease, transform 140ms var(--ease-out);
}
.sources a:active { transform: scale(0.98); }
.sources a[aria-current] { background: var(--surface-2); font-weight: 650; }
.sources .name { flex: 1; min-width: 0; }
.sources li:first-child { margin-bottom: 4px; padding-bottom: 4px; border-bottom: 1px solid var(--line); }
.sources .n { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.state { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.state.s-ok { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 18%, transparent); }
.state.s-empty { background: var(--warn); }
.state.s-error { background: var(--accent); }
.state.none { visibility: hidden; }
.fine { margin: 0 4px; color: var(--muted); font-size: 12.5px; }

/* buttons */
.follow {
  display: inline-grid; place-items: center; height: 32px; padding: 0 15px; border-radius: 999px;
  border: 1px solid var(--ink); background: var(--ink); color: var(--bg);
  font-size: 13px; font-weight: 650; cursor: pointer; white-space: nowrap;
  transition: background-color 160ms ease, color 160ms ease, border-color 160ms ease, transform 140ms var(--ease-out);
}
.follow > span { grid-area: 1 / 1; }
.follow .f-on, .follow .f-un { visibility: hidden; }
.follow[aria-pressed=true] { background: transparent; color: var(--ink); border-color: var(--line-strong); }
.follow[aria-pressed=true] .f-off { visibility: hidden; }
.follow[aria-pressed=true] .f-on { visibility: visible; }
.follow:active { transform: scale(0.96); }
.post-follow {
  display: inline-grid; place-items: center; height: 24px; padding: 0 10px; border: 0; border-radius: 999px;
  background: var(--accent-soft); color: var(--accent);
  font-size: 12.5px; font-weight: 650; cursor: pointer; align-self: center; white-space: nowrap;
  transition: background-color 150ms ease, color 150ms ease, transform 140ms var(--ease-out);
}
.post-follow > span { grid-area: 1 / 1; }
.post-follow .f-on, .post-follow .f-un { visibility: hidden; }
.post-follow[aria-pressed=true] { background: transparent; color: var(--muted); box-shadow: inset 0 0 0 1px var(--line-strong); }
.post-follow[aria-pressed=true] .f-off { visibility: hidden; }
.post-follow[aria-pressed=true] .f-on { visibility: visible; }
.post-follow[aria-pressed=true]:not([data-kept]) { display: none; }
.post-follow:active { transform: scale(0.96); }
.btn {
  display: inline-flex; align-items: center; justify-content: center; height: 38px; padding: 0 18px;
  border-radius: 999px; background: var(--ink); color: var(--bg);
  font-size: 14px; font-weight: 620; text-decoration: none;
  transition: transform 140ms var(--ease-out), opacity 150ms ease;
}
.btn:active { transform: scale(0.97); }
.link {
  padding: 0; border: 0; background: none; color: var(--accent); font-weight: 600; cursor: pointer;
  text-decoration: underline; text-underline-offset: 3px;
}

/* feed foot */
.foot { padding: 26px 0 8px; text-align: center; color: var(--muted); font-size: 14px; }
.foot > [data-show] { display: none; }
.foot[data-state=loading] > [data-show=loading],
.foot[data-state=end] > [data-show=end],
.foot[data-state=empty] > [data-show=empty],
.foot[data-state=error] > [data-show=error],
.foot[data-state=nofollow] > [data-show=nofollow] { display: block; }
.foot p { margin: 0; }
.end { display: flex; align-items: center; justify-content: center; gap: 12px; }
.foot[data-state=end] > .end { display: flex; }
.end::before, .end::after { content: ""; width: 40px; height: 1px; background: var(--line-strong); }
.spin {
  display: block; width: 20px; height: 20px; margin: 0 auto; border-radius: 50%;
  border: 2px solid var(--line-strong); border-top-color: var(--accent);
  animation: spin 600ms linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.divider {
  display: flex; align-items: center; gap: 12px; margin: 14px 4px 4px;
  color: var(--muted); font-size: 12px; font-weight: 650; letter-spacing: .05em; text-transform: uppercase;
}
.divider::before, .divider::after { content: ""; flex: 1; height: 1px; background: var(--line-strong); }
.empty, .quiet {
  padding: 32px 24px; border-radius: var(--radius); text-align: center;
  background: var(--surface); border: 1px dashed var(--line-strong); color: var(--muted);
}
.quiet p { margin: 0 0 14px; }
.empty h2 { margin: 0; color: var(--ink); font-size: 18px; letter-spacing: -.01em; }
.empty > p { margin: 6px auto 18px; max-width: 34ch; }
.empty .people { max-width: 340px; margin: 0 auto 18px; text-align: left; }

/* hover only where a real pointer can hover */
@media (hover: hover) and (pointer: fine) {
  .tabs a:hover { color: var(--ink); }
  .lang button[aria-pressed=false]:hover { color: var(--ink); }
  .act:hover { background: var(--surface-2); color: var(--ink); }
  .post-title a:hover, .who:hover, .person-name:hover, .crumbs a:hover { text-decoration: underline; }
  .src:hover { background: color-mix(in srgb, var(--ink) 9%, var(--surface)); color: var(--ink); }
  .media:hover { border-color: var(--line-strong); }
  .chip:hover { background: var(--surface-2); color: var(--ink); }
  .shots-more:hover { background: rgb(0 0 0 / .62); }
  .sources a:not([aria-current]):hover { background: color-mix(in srgb, var(--surface-2) 70%, transparent); }
  .btn:hover { opacity: .88; }
  .follow[aria-pressed=false]:hover { opacity: .88; }
  .follow[aria-pressed=true]:not([data-fresh]):hover {
    color: var(--accent); background: var(--accent-soft);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  }
  .follow[aria-pressed=true]:not([data-fresh]):hover .f-on { visibility: hidden; }
  .follow[aria-pressed=true]:not([data-fresh]):hover .f-un { visibility: visible; }
  .post-follow[aria-pressed=false]:hover { background: color-mix(in srgb, var(--accent) 18%, transparent); }
  .post-follow[aria-pressed=true]:not([data-fresh]):hover { color: var(--accent); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 45%, transparent); }
  .post-follow[aria-pressed=true]:not([data-fresh]):hover .f-on { visibility: hidden; }
  .post-follow[aria-pressed=true]:not([data-fresh]):hover .f-un { visibility: visible; }
}

@media (min-width: 961px) { .m-only { display: none !important; } }
@media (max-width: 960px) {
  .shell { grid-template-columns: minmax(0, 1fr); max-width: 700px; }
  .col { justify-self: stretch; max-width: none; }
  .rail { display: none; }
}
@media (max-width: 600px) {
  .bar-in { padding: 0 14px; height: 54px; }
  .shell { padding: 14px 10px 80px; }
  .strip { margin: 0 -10px 12px; }
  .people.row { padding: 0 10px; }
  .post { grid-template-columns: 36px minmax(0, 1fr); gap: 10px; padding: 14px 14px 6px; }
  .post > .avatar { width: 36px; height: 36px; font-size: 14px; }
  .post.op .post-title { font-size: 19px; }
  .handle { display: none; }
  .media.work { grid-template-columns: minmax(0, 1fr); }
  .code-block { min-height: 56px; }
  .replies { padding-left: 18px; }
  .replies::before { left: 6px; }
  .replies > li::before { left: -12px; width: 12px; }
  .profile-in { padding: 0 16px 18px; }
}
@media (max-width: 440px) {
  .brand-word { display: none; }
  .tabs a { min-width: 76px; padding: 6px 10px; }
  .lang button { padding: 0 8px; }
}
@media (prefers-reduced-motion: reduce) {
  .post.enter { transition: opacity 200ms ease; transition-delay: 0ms; }
  @starting-style { .post.enter { transform: none; } }
  .tabs.ready .tabs-pill { transition: opacity 150ms ease; }
  .spin { animation-duration: 1.4s; }
}
"""

# Runs in <head> before the first paint, so a reader who picked English never sees Chinese flash.
HEAD_JS = (
    "(function(){var d=document.documentElement;d.classList.add('js');"
    "try{if(JSON.parse(localStorage.getItem('avmon.lang')||'null')==='en'){d.dataset.lang='en';d.lang='en';}}"
    "catch(e){}})()"
)

# Plain string, not an f-string. It must never contain the text href='/ (see _relativize).
JS = """
(() => {
  const FOLLOWS = 'avmon.follows';
  const TAB = 'avmon.tab';
  const LANG = 'avmon.lang';
  const html = document.documentElement;
  const lang = () => (html.dataset.lang === 'en' ? 'en' : 'zh');
  const PAGE = 12;
  const store = {
    get(key, fallback) {
      try { const raw = localStorage.getItem(key); return raw === null ? fallback : JSON.parse(raw); }
      catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} },
  };
  const readFollows = () => {
    const saved = store.get(FOLLOWS, []);
    return new Set(Array.isArray(saved) ? saved.map(String) : []);
  };
  let follows = readFollows();

  const feed = document.getElementById('feed');
  const foot = document.getElementById('foot');
  const tabs = document.querySelector('.tabs');
  const tabbed = !!feed && feed.dataset.tabs === '1';
  const root = feed ? feed.dataset.root : '/';
  const version = feed ? feed.dataset.version : '';
  const site = feed ? feed.dataset.site : '';
  const person = feed ? feed.dataset.person : '';

  // Relative times. The page may be a static snapshot, so this runs in the reader's browser.
  const formats = {};
  const fmt = (code, kind) => {
    const locale = code === 'en' ? 'en' : 'zh-CN';
    const options = {
      short: { month: 'short', day: 'numeric' },
      long: { year: 'numeric', month: 'short', day: 'numeric' },
      exact: { dateStyle: 'medium', timeStyle: 'short' },
    }[kind];
    return (formats[code + kind] ||= new Intl.DateTimeFormat(locale, options));
  };
  const units = {
    zh: { now: '刚刚', m: (n) => n + '分钟前', h: (n) => n + '小时前', d: (n) => n + '天前' },
    en: { now: 'now', m: (n) => n + 'm', h: (n) => n + 'h', d: (n) => n + 'd' },
  };
  function ago(scope) {
    const code = lang();
    const u = units[code];
    scope.querySelectorAll('time.ago').forEach((node) => {
      if (!node.dateTime) return;
      const when = new Date(node.dateTime);
      if (Number.isNaN(when.getTime())) return;
      const s = (Date.now() - when) / 1000;
      node.textContent =
        s < 60 ? u.now
        : s < 3600 ? u.m(Math.floor(s / 60))
        : s < 86400 ? u.h(Math.floor(s / 3600))
        : s < 604800 ? u.d(Math.floor(s / 86400))
        : fmt(code, when.getFullYear() === new Date().getFullYear() ? 'short' : 'long').format(when);
      node.title = fmt(code, 'exact').format(when);
    });
  }

  // Attributes cannot hold two languages, so the server writes both into data-* and this picks one.
  const SWAPPED = ['aria-label', 'title', 'alt'];
  function localize(scope) {
    const code = lang();
    SWAPPED.forEach((name) => {
      scope.querySelectorAll('[data-' + name + '-en]').forEach((node) => {
        node.setAttribute(name, node.getAttribute('data-' + name + '-' + code));
      });
    });
  }

  function setLang(code) {
    html.dataset.lang = code;
    html.lang = code === 'en' ? 'en' : 'zh-Hans';
    store.set(LANG, code);
    const title = document.querySelector('title');
    if (title && title.dataset[code]) document.title = title.dataset[code];
    document.querySelectorAll('button[data-lang]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.lang === code));
    });
    localize(document);
    ago(document);
  }

  function syncFollow(scope) {
    (scope || document).querySelectorAll('[data-follow]').forEach((button) => {
      button.setAttribute('aria-pressed', String(follows.has(button.dataset.follow)));
    });
  }

  // Feed engine: filter the index, then render it a page at a time from the card chunks.
  let tab = tabbed ? 'discover' : 'all';
  let indexLoad = null;
  const chunkLoads = new Map();
  let list = null;
  let pos = 0;
  let lastDated = null;
  let busy = false;
  let done = true;
  let generation = 0;

  const getJSON = (path) =>
    fetch(root + path + '?v=' + version).then((response) => {
      if (!response.ok) throw new Error(response.status + ' ' + path);
      return response.json();
    });
  const loadIndex = () =>
    (indexLoad ||= getJSON('feed.json').catch((error) => { indexLoad = null; throw error; }));
  const loadChunk = (n) => {
    if (!chunkLoads.has(n)) {
      chunkLoads.set(n, getJSON('cards/' + n + '.json').catch((error) => { chunkLoads.delete(n); throw error; }));
    }
    return chunkLoads.get(n);
  };
  const wanted = (post) =>
    (!site || post[2] === site) &&
    (!person || String(post[1]) === person) &&
    (tab !== 'following' || follows.has(String(post[1])));
  const setState = (state) => { if (foot) foot.dataset.state = state; };
  const nearFoot = () => foot && foot.getBoundingClientRect().top < innerHeight + 800;

  function divider() {
    return document.getElementById('divider-tpl').content.firstElementChild.cloneNode(true);
  }

  async function more() {
    if (!feed || busy || done) return;
    busy = true;
    const mine = generation;
    setState('loading');
    try {
      const data = await loadIndex();
      if (mine !== generation) return;
      list ||= data.posts.reduce((found, post, i) => (wanted(post) && found.push(i), found), []);
      const slice = list.slice(pos, pos + PAGE);
      const html = await Promise.all(
        slice.map((i) => loadChunk(Math.floor(i / data.chunk)).then((cards) => cards[i % data.chunk]))
      );
      if (mine !== generation) return;
      const batch = document.createDocumentFragment();
      slice.forEach((i, k) => {
        const dated = data.posts[i][3] === 1;
        if (lastDated && !dated) batch.append(divider());
        lastDated = dated;
        const template = document.createElement('template');
        template.innerHTML = html[k] || '';
        const post = template.content.firstElementChild;
        if (!post) return;
        post.classList.add('enter');
        post.style.setProperty('--i', String(Math.min(k, 6)));
        if (root !== '/') {
          post.querySelectorAll('a[href^="/"]').forEach((a) => {
            a.setAttribute('href', root + a.getAttribute('href').slice(1));
          });
        }
        batch.append(post);
      });
      ago(batch);
      localize(batch);
      syncFollow(batch);
      feed.append(batch);
      pos += slice.length;
      done = pos >= list.length;
      setState(done ? (list.length ? 'end' : 'empty') : 'idle');
    } catch (error) {
      if (mine === generation) setState('error');
    } finally {
      if (mine === generation) busy = false;
    }
    if (mine === generation && foot.dataset.state === 'idle' && nearFoot()) requestAnimationFrame(more);
  }

  function reset() {
    if (!feed) return;
    generation += 1;
    busy = false;
    list = null;
    pos = 0;
    lastDated = null;
    feed.replaceChildren();
    if (tab === 'following' && follows.size === 0) {
      done = true;
      setState('nofollow');
      return;
    }
    done = false;
    setState('idle');
    more();
  }

  function showTab(name) {
    tab = name;
    document.body.dataset.view = name;
    if (!tabs) return;
    tabs.dataset.active = name;
    tabs.querySelectorAll('[data-tab]').forEach((link) => {
      if (link.dataset.tab === name) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function switchTab(name) {
    if (name === tab) return;
    showTab(name);
    store.set(TAB, name);
    history.replaceState(null, '', '#' + name);
    if (scrollY > 0) scrollTo(0, 0);
    reset();
  }

  document.addEventListener('click', (event) => {
    const langButton = event.target.closest('button[data-lang]');
    if (langButton) {
      setLang(langButton.dataset.lang);
      return;
    }
    const follow = event.target.closest('[data-follow]');
    if (follow) {
      const id = follow.dataset.follow;
      if (follows.has(id)) follows.delete(id);
      else follows.add(id);
      store.set(FOLLOWS, [...follows]);
      // The pointer is still on the button, so hold off the hover "Unfollow" until it leaves.
      follow.dataset.fresh = '';
      if (follow.classList.contains('post-follow')) follow.dataset.kept = '';
      follow.addEventListener('pointerleave', () => { delete follow.dataset.fresh; }, { once: true });
      syncFollow();
      if (tab === 'following') reset();
      return;
    }
    const tabLink = event.target.closest('a[data-tab]');
    if (tabLink && tabbed) {
      event.preventDefault();
      switchTab(tabLink.dataset.tab);
      return;
    }
    if (event.target.closest('[data-retry]')) {
      setState('idle');
      more();
    }
  });

  addEventListener('storage', (event) => {
    if (event.key !== FOLLOWS) return;
    follows = readFollows();
    syncFollow();
    if (tab === 'following') reset();
  });

  syncFollow();
  setLang(lang());

  if (feed) {
    pos = Number(feed.dataset.rendered || 0);
    const last = feed.querySelector('article.post:last-of-type');
    lastDated = last ? last.dataset.dated === '1' : null;
    done = foot.dataset.state === 'end' || foot.dataset.state === 'empty';
    if (tabbed) {
      const asked = location.hash.slice(1);
      const saved = store.get(TAB, null);
      const known = (name) => name === 'following' || name === 'discover';
      const first = known(asked) ? asked : known(saved) ? saved : follows.size ? 'following' : 'discover';
      showTab(first);
      // The server already rendered page one of Discover. Following is built here.
      if (first === 'following') reset();
    }
    new IntersectionObserver(
      (entries) => { if (entries[0].isIntersecting && foot.dataset.state === 'idle') more(); },
      { rootMargin: '0px 0px 800px 0px' }
    ).observe(foot);
  }

  // Let the first paint place the tab pill without sliding it in from the left.
  requestAnimationFrame(() => requestAnimationFrame(() => tabs && tabs.classList.add('ready')));
})();
"""
