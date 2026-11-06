# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-person activity monitor for 三上悠亜 (Yua Mikami). It scrapes public pages from several sites into a local SQLite database, then renders the stored rows as a scrolling HTML feed. The feed is served on localhost or exported as a static site to `docs/`, which is committed and published (`docs/.nojekyll` is there for GitHub Pages).

The code uses only the Python standard library (`urllib`, `sqlite3`, `http.server`, `xml.etree`). There are no dependencies, build step, test suite, or linter config. It is written for Python 3.10+ (`X | None` types, `from __future__ import annotations`).

## Commands

All commands run from the repo root through the package entry point:

```sh
python3 -m avmon fetch            # seed accounts, scrape every enabled account, then print the report
python3 -m avmon show             # print the report from the DB without fetching
python3 -m avmon serve [--port 8765]   # localhost feed at http://127.0.0.1:8765/
python3 -m avmon export [--out docs]   # regenerate the static site
```

`export` deletes every `*.html` file, `feed.json` and `cards/*.json` under the output directory, then removes empty directories, before it writes. Run it against `docs/` only on purpose. To check a single reader without touching the DB, call it directly, e.g. `python3 -c "from avmon.sources import x; print(x.fetch('https://x.com/yua_mikami', None))"`.

## Architecture

Data flows **readers → `run.py` → SQLite → `web.py`**.

- **`avmon/db.py`**: schema (`person`, `account`, `activity`, `profile_fact`, `fetch_log`), the hard-coded `ACCOUNTS` seed list, and the upserts. The DB lives at `data/monitor.sqlite` (gitignored). `connect()` runs the schema on every call, so a schema change takes effect on the next connection. There are no migrations, so `CREATE TABLE IF NOT EXISTS` will not alter existing tables. `activity` is unique on `(site, external_id)`, and a re-fetch updates the row in place.
- **`avmon/sources/`**: one module per site. Each module exposes the constants `SITE`, `LABEL`, `BLURB`, `MARK`, and `AUTHOR`, plus a `fetch(url, native_id) -> Parsed` function. `sources/__init__.py` collects the modules in `READERS`. Their order is the order of the filter chips on the feed, and the `SITES`/`AUTHOR`/`MARK` lookups that `web.py` imports are built from it. **To add a site:** write the module, add it to `READERS`, and add a row to `ACCOUNTS` in `db.py`. For its badge icon, add a glyph to `SITE_ICONS` and a `--c-<site>` colour in `web.py`. Without them the badge shows the label's first letter on a grey tile.
  - `model.py` defines the one return shape: `Parsed` (status, items, profile `facts`, discovered `native_id`, and a `note` that is stored as `account.last_error` and shown on that site's feed page) and `Item`. `Item.extra` is serialized to `activity.extra_json`, which `web.py` reads for media galleries, DMM codes (`code_key`), and similar fields.
  - `dmm.py` and `htmltext.py` are shared helpers, not readers.
  - Some readers deliberately store no items: TikTok hits a WAF challenge, and Instagram's logged-out page has no post list. They only confirm that the profile exists and record a note. X only gets the roughly 5 posts embedded in the logged-out profile HTML.
- **`avmon/http.py`**: the single `get()` used by every reader. It sends a browser UA and percent-encodes IRIs, since the account URLs contain Japanese characters. It raises `FetchError(status=...)`. Per the module docstring, it does not try to get past bot challenges.
- **`avmon/run.py`**: `fetch_all()` loops over accounts, catches every exception per account (so one broken site never stops the run), writes `fetch_log`, and updates `account.last_status`, which is `ok`, `empty`, or `error`.
- **`avmon/web.py`**: a forum-style feed. The HTML is built with f-strings, while the `CSS` and `JS` module strings are inlined into every page by `page()`. `_load()` reads the whole DB on each request.
  - Routes: `/` (Discover, all actors), `/<site>/` (one source; `/?site=` still works on `serve`), `/a/<person_id>/` (actor profile and feed), `/t/<site>/<external_id>/` (thread page; linked posts from `_linked()`, e.g. an X post pointing at a stored YouTube video, show as replies).
  - Infinite scroll: the server renders the first `FIRST_PAGE` posts. The script then reads `feed.json`, an index of every post as `[key, person_id, site, dated]`, filters it in the browser by tab, site and actor, and loads the rendered cards from `cards/<n>.json` (`CHUNK` cards per file). `serve` returns these files dynamically, and `export` writes them out.
  - The **Following / Discover** tabs and follows exist only in the browser (`localStorage` keys `avmon.follows`, `avmon.tab`, plus the URL hash). The server has no follow state. With no follows, the Following tab shows a "follow actors" panel.
  - **Link rule:** every internal link is root-absolute, single-quoted and ends in a slash, e.g. `href='/a/1/'`. On static pages, `_relativize()` rewrites that form by depth. For cards loaded later, the script rewrites `a[href^="/"]` against `data-root`. A link in any other form breaks the static export. The `JS` string must never contain the text `href='/`.
  - **Two languages: Chinese (default) and English.** Every piece of interface text is written in both. Use `_t(zh, en)` for plain text, `_th()` for text containing markup, and `_attr(name, zh, en)` for `aria-label`, `title` and `alt`. CSS shows the language picked in the 中文/EN toggle (`html[data-lang]`, saved as `avmon.lang`). `HEAD_JS` applies the saved choice before the first paint, and the script swaps attribute text from `data-<attr>-zh` / `data-<attr>-en`. Posts and actor data are marked `lang='ja'`, so kanji render with Japanese glyphs. Chinese source descriptions are in `BLURB_ZH`. A new visible string written without `_t` would show in only one language.
  - Data is ready for multiple actors: posts, avatars and follows are keyed by `person_id`. The scrapers and `ACCOUNTS` seed still cover one actor, though.
