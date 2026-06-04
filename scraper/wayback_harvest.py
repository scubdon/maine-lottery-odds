#!/usr/bin/env python3
"""
One-off: recover tickets-printed data for games whose maine.gov article page
the lottery has taken down, by reading the pages from the Wayback Machine.

For each archived Lottery_Scratch article id it walks snapshots newest->oldest
until one parses to a Game # + Tickets Printed (and the other article fields),
then writes a {game_number -> article} map to wayback_articles.json. Results are
merged with any existing file so good captures survive Wayback rate-limiting
across runs; re-run it a couple of times to fill any games a single pass missed.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

CDX = "http://web.archive.org/cdx/search/cdx"
session = requests.Session()
session.headers.update({"User-Agent": "maine-lottery-odds wayback recovery (GitHub issues)"})


def cdx_snapshots_by_id() -> dict[str, list[tuple[str, str]]]:
    """article id -> [(timestamp, original_url), ...] newest first."""
    params = {
        "url": "www.maine.gov/tools/whatsnew/index.php?",
        "matchType": "prefix",
        "filter": ["original:.*Lottery_Scratch.*", "statuscode:200"],
        "output": "json",
        "limit": "-20000",  # newest first
    }
    r = session.get(CDX, params=params, timeout=120)
    r.raise_for_status()
    by: dict[str, list[tuple[str, str]]] = {}
    for row in json.loads(r.text)[1:]:
        ts, orig = row[1], row[2]
        m = re.search(r"id=(\d+)", orig.replace("&amp;", "&"))
        if m:
            by.setdefault(m.group(1), []).append((ts, orig))
    return by


def fetch_snapshot_html(ts: str, raw: str) -> str | None:
    """Fetch one archived snapshot, retrying through Wayback rate-limiting."""
    snap = f"http://web.archive.org/web/{ts}id_/{raw}"
    for attempt in range(4):
        try:
            r = session.get(snap, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            return r.text
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    return None


def parse_snapshot(ts: str, original: str) -> dict | None:
    raw = original.replace("&amp;", "&")
    snap = f"http://web.archive.org/web/{ts}id_/{raw}"
    html = fetch_snapshot_html(ts, raw)
    if html is None:
        return None

    text = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))
    gm = re.search(r"Game\s*#\s*(\d+)", text)
    tp = re.search(r"Tickets?\s+Printed\s*[:\-]?\s*([\d,]+)", text, re.IGNORECASE)
    if not gm or not tp:
        return None

    def grab(pat):
        m = re.search(pat, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    title = BeautifulSoup(html, "html.parser").find(["h1", "h2", "title"])
    return {
        "game_number": int(gm.group(1)),
        "tickets_printed": int(tp.group(1).replace(",", "")),
        "max_award": grab(r"Maximum Award\s*[:\-]?\s*(\$[\d,]+)"),
        "overall_odds": grab(r"OVERALL ODDS OF WINNING\s*1:\s*([\d,.]+)"),
        "top_prize_odds": grab(r"HIGHEST INSTANT PRIZE ODDS\s*1:\s*([\d,.]+)"),
        "on_sale": grab(r"On Sale\s*[:\-]?\s*([A-Za-z]+ \d{1,2}, \d{4})"),
        "article_url": raw,
        "wayback_url": snap,
        "wayback_timestamp": ts,
        "page_title": title.get_text(strip=True) if title else None,
    }


def resolve_id(snaps: list[tuple[str, str]], max_tries: int = 12) -> dict | None:
    """Walk snapshots newest->oldest until one parses to a game+tickets."""
    for ts, orig in snaps[:max_tries]:
        res = parse_snapshot(ts, orig)
        if res:
            return res
    return None


def main() -> None:
    out = os.path.join(os.path.dirname(__file__), "wayback_articles.json")
    # merge with prior runs so good captures accumulate despite transient failures
    best: dict[int, dict] = {}
    if os.path.exists(out):
        best = {int(k): v for k, v in json.load(open(out, encoding="utf-8")).items()}
        print(f"loaded {len(best)} games from previous run", file=sys.stderr)

    by_id = cdx_snapshots_by_id()
    print(f"{len(by_id)} unique article ids archived", file=sys.stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for res in pool.map(resolve_id, by_id.values()):
            if not res:
                continue
            gn = res["game_number"]
            if gn not in best or res["wayback_timestamp"] > best[gn]["wayback_timestamp"]:
                best[gn] = res

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(best, fh, indent=2, ensure_ascii=False, sort_keys=True)
    print(f"parsed {len(best)} games with tickets-printed -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
