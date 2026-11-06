"""SQLite schema, seed, and activity upserts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "monitor.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY,
    name_jp TEXT NOT NULL,
    name_en TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    site TEXT NOT NULL,
    kind TEXT NOT NULL,
    native_id TEXT,
    handle TEXT,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fetched_at TEXT,
    last_status TEXT,
    last_error TEXT,
    UNIQUE(site, url)
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id),
    person_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    body TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    outbound_url TEXT,
    extra_json TEXT,
    UNIQUE(site, external_id)
);

CREATE TABLE IF NOT EXISTS profile_fact (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    source_site TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE(person_id, source_site, field)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    http_status INTEGER,
    items_seen INTEGER,
    items_new INTEGER,
    error TEXT
);
"""

# One dict per actress. Each account is one public page the runner fetches.
# Jav-forum native_id 143 is 三上悠亜's thread. Page 10 and post 108578 are a point inside it.
PEOPLE = [
    {
        "name_jp": "三上悠亜",
        "name_en": "Yua Mikami",
        "notes": (
            "Request marker @foofoo6912. "
            "Jav-forum sample point was page-10#post-108578."
        ),
        "accounts": [
            {
                "site": "youtube",
                "kind": "channel",
                "native_id": "UC0BCfHbNORhd7dsw1R5rw9w",
                "handle": None,
                "url": "https://www.youtube.com/channel/UC0BCfHbNORhd7dsw1R5rw9w",
            },
            {
                "site": "x",
                "kind": "profile",
                "native_id": None,
                "handle": "yua_mikami",
                "url": "https://x.com/yua_mikami",
            },
            {
                "site": "tiktok",
                "kind": "profile",
                "native_id": None,
                "handle": "yua_mikami",
                "url": "https://www.tiktok.com/@yua_mikami",
            },
            {
                "site": "instagram",
                "kind": "profile",
                "native_id": None,
                "handle": "yua_mikami",
                "url": "https://www.instagram.com/yua_mikami",
            },
            {
                "site": "oshiseku",
                "kind": "profile",
                "native_id": None,
                "handle": None,
                "url": "https://oshiseku.com/actless/三上悠亜/",
            },
            {
                "site": "javforum",
                "kind": "thread",
                "native_id": "143",
                "handle": None,
                "url": "https://www.jav-forum.com/threads/yua-mikami-三上悠亜.143/",
            },
            {
                "site": "avfan",
                "kind": "tag_page",
                "native_id": "yua-mikami",
                "handle": None,
                "url": "https://avfan.org/movie/tag/yua-mikami",
            },
        ],
    },
    {
        "name_jp": "深田えいみ",
        "name_en": "Eimi Fukada",
        "notes": "Public pages only. avfan has no tag page for this name.",
        "accounts": [
            {
                "site": "youtube",
                "kind": "channel",
                "native_id": "UCprlj3Bvn0c3XsyvZsIrzEg",
                "handle": "Eimi_ch",
                "url": "https://www.youtube.com/@Eimi_ch",
            },
            {
                "site": "x",
                "kind": "profile",
                "native_id": None,
                "handle": "fukada0318",
                "url": "https://x.com/fukada0318",
            },
            {
                "site": "tiktok",
                "kind": "profile",
                "native_id": None,
                "handle": "fukada0318",
                "url": "https://www.tiktok.com/@fukada0318",
            },
            {
                "site": "instagram",
                "kind": "profile",
                "native_id": None,
                "handle": "eimi0318",
                "url": "https://www.instagram.com/eimi0318",
            },
            {
                "site": "oshiseku",
                "kind": "profile",
                "native_id": None,
                "handle": None,
                "url": "https://oshiseku.com/actless/深田えいみ/",
            },
            {
                "site": "javforum",
                "kind": "thread",
                "native_id": "192",
                "handle": None,
                "url": "https://www.jav-forum.com/threads/eimi-fukada-深田えいみ.192/",
            },
        ],
    },
]


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def seed(con: sqlite3.Connection) -> int:
    """Insert each actress and her accounts. Returns the last person id."""
    person_id = 0
    for person in PEOPLE:
        row = con.execute(
            "SELECT id FROM person WHERE name_jp = ?", (person["name_jp"],)
        ).fetchone()
        if row is None:
            cur = con.execute(
                """
                INSERT INTO person (name_jp, name_en, notes)
                VALUES (?, ?, ?)
                """,
                (person["name_jp"], person["name_en"], person["notes"]),
            )
            person_id = int(cur.lastrowid)
        else:
            person_id = int(row["id"])
            con.execute(
                "UPDATE person SET name_en = ?, notes = ? WHERE id = ?",
                (person["name_en"], person["notes"], person_id),
            )
        for account in person["accounts"]:
            con.execute(
                """
                INSERT INTO account (person_id, site, kind, native_id, handle, url)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(site, url) DO UPDATE SET
                    person_id = excluded.person_id,
                    kind = excluded.kind,
                    handle = excluded.handle,
                    native_id = COALESCE(account.native_id, excluded.native_id)
                """,
                (
                    person_id,
                    account["site"],
                    account["kind"],
                    account["native_id"],
                    account["handle"],
                    account["url"],
                ),
            )
    con.commit()
    return person_id


def upsert_activity(con: sqlite3.Connection, row: dict) -> bool:
    existing = con.execute(
        "SELECT id FROM activity WHERE site = ? AND external_id = ?",
        (row["site"], row["external_id"]),
    ).fetchone()
    if existing is None:
        con.execute(
            """
            INSERT INTO activity (
                account_id, person_id, site, kind, external_id, url, title, body,
                published_at, fetched_at, outbound_url, extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["account_id"],
                row["person_id"],
                row["site"],
                row["kind"],
                row["external_id"],
                row["url"],
                row.get("title"),
                row.get("body"),
                row.get("published_at"),
                row["fetched_at"],
                row.get("outbound_url"),
                row.get("extra_json"),
            ),
        )
        return True
    con.execute(
        """
        UPDATE activity
        SET title = ?, body = ?, url = ?, published_at = COALESCE(?, published_at),
            fetched_at = ?, outbound_url = ?, extra_json = ?
        WHERE id = ?
        """,
        (
            row.get("title"),
            row.get("body"),
            row["url"],
            row.get("published_at"),
            row["fetched_at"],
            row.get("outbound_url"),
            row.get("extra_json"),
            existing["id"],
        ),
    )
    return False


def upsert_fact(con: sqlite3.Connection, person_id: int, source: str, field: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO profile_fact (person_id, source_site, field, value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(person_id, source_site, field) DO UPDATE SET value = excluded.value
        """,
        (person_id, source, field, value),
    )


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
