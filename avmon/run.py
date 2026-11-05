"""Fetch every saved account and print the rows that landed."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from avmon.db import connect, dumps, seed, upsert_activity, upsert_fact
from avmon.http import FetchError
from avmon.sources import fetch_account


def fetch_all() -> None:
    con = connect()
    seed(con)
    accounts = con.execute(
        "SELECT * FROM account WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    print(f"database  { _db_path() }")
    print(f"person    三上悠亜")
    print("")
    for account in accounts:
        _fetch_one(con, account)
    con.commit()
    print("")
    report(con)


def report(con: sqlite3.Connection | None = None) -> None:
    own = con is None
    if con is None:
        con = connect()
    print("accounts")
    rows = con.execute(
        """
        SELECT a.site, a.kind, a.native_id, a.handle, a.last_status, a.last_error,
               (SELECT COUNT(*) FROM activity WHERE account_id = a.id) AS rows,
               (SELECT items_new FROM fetch_log WHERE account_id = a.id ORDER BY id DESC LIMIT 1) AS new_rows
        FROM account a
        ORDER BY a.id
        """
    ).fetchall()
    for row in rows:
        ident = row["native_id"] or row["handle"] or ""
        err = f"  {row['last_error']}" if row["last_error"] else ""
        print(
            f"  {row['site']:<10} {row['last_status'] or '-':<8} "
            f"rows {row['rows']:<4} new {row['new_rows'] if row['new_rows'] is not None else '-':<4} "
            f"{ident}{err}"
        )

    print("\nprofile facts")
    facts = con.execute(
        "SELECT field, value FROM profile_fact ORDER BY id"
    ).fetchall()
    if not facts:
        print("  none")
    for fact in facts:
        value = fact["value"].replace("\n", " ")
        if len(value) > 160:
            value = value[:159] + "…"
        print(f"  {fact['field']}: {value}")

    print("\nrows")
    for site, order in (
        ("youtube", "COALESCE(published_at, '') DESC"),
        ("x", "COALESCE(published_at, '') DESC"),
        ("javforum", "COALESCE(published_at, '') DESC"),
        ("oshiseku", "external_id"),
        ("avfan", "CAST(external_id AS INTEGER) DESC"),
    ):
        items = con.execute(
            f"""
            SELECT external_id, title, body, published_at, outbound_url, extra_json
            FROM activity
            WHERE site = ?
            ORDER BY {order}
            LIMIT 3
            """,
            (site,),
        ).fetchall()
        print(f"  [{site}]")
        if not items:
            print("    no rows")
            continue
        for item in items:
            label = item["title"] or item["body"] or ""
            label = label.replace("\n", " ")
            if len(label) > 90:
                label = label[:89] + "…"
            when = item["published_at"] or "no date"
            extra = f"  -> {item['outbound_url']}" if item["outbound_url"] else ""
            code = ""
            if item["extra_json"] and "code_key" in item["extra_json"]:
                code = item["extra_json"].split('"code_key":"', 1)[-1].split('"', 1)[0] + "  "
            print(f"    {when}  {code}{item['external_id']}  {label}{extra}")

    print("\ncross-posts  X posts that point at a stored YouTube video")
    matches = con.execute(
        """
        SELECT x.published_at, x.external_id, x.body, y.external_id AS video_id, y.title
        FROM activity x
        JOIN activity y
          ON y.site = 'youtube'
         AND x.outbound_url LIKE '%' || y.external_id || '%'
        WHERE x.site = 'x'
        ORDER BY x.published_at DESC
        """
    ).fetchall()
    if not matches:
        print("  none")
    for match in matches:
        body = (match["body"] or "").replace("\n", " ")
        print(f"  {match['published_at']}  tweet {match['external_id']}  video {match['video_id']}  {body[:70]}")
    if own:
        con.close()


def _fetch_one(con: sqlite3.Connection, account: sqlite3.Row) -> None:
    started = _now()
    status = None
    seen = 0
    new = 0
    error = None
    try:
        parsed = fetch_account(account["site"], account["url"], account["native_id"])
        status = parsed.http_status
        seen = len(parsed.items)
        fetched_at = _now()
        for item in parsed.items:
            inserted = upsert_activity(
                con,
                {
                    "account_id": account["id"],
                    "person_id": account["person_id"],
                    "site": account["site"],
                    "kind": item.kind,
                    "external_id": item.external_id,
                    "url": item.url,
                    "title": item.title,
                    "body": item.body,
                    "published_at": item.published_at,
                    "fetched_at": fetched_at,
                    "outbound_url": item.outbound_url,
                    "extra_json": dumps(item.extra) if item.extra else None,
                },
            )
            new += int(inserted)
        for field_name, value in parsed.facts:
            upsert_fact(con, account["person_id"], account["site"], field_name, value)
        con.execute(
            """
            UPDATE account
            SET native_id = COALESCE(?, native_id),
                last_fetched_at = ?, last_status = ?, last_error = ?
            WHERE id = ?
            """,
            (parsed.native_id, fetched_at, "empty" if seen == 0 else "ok", parsed.note, account["id"]),
        )
        error = parsed.note
        state = "empty" if seen == 0 else "ok"
    except (FetchError, Exception) as exc:
        status = getattr(exc, "status", None)
        error = str(exc)
        state = "error"
        con.execute(
            """
            UPDATE account
            SET last_fetched_at = ?, last_status = ?, last_error = ?
            WHERE id = ?
            """,
            (_now(), "error", error, account["id"]),
        )
    finished = _now()
    con.execute(
        """
        INSERT INTO fetch_log (
            account_id, started_at, finished_at, http_status, items_seen, items_new, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (account["id"], started, finished, status, seen, new, error),
    )
    print(f"{account['site']:<10} {state:<6} seen {seen:<3} new {new:<3} {error or ''}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_path() -> str:
    from avmon.db import DB_PATH
    return str(DB_PATH)
