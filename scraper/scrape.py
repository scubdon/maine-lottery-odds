#!/usr/bin/env python3
"""
Maine Lottery scratch-ticket odds scraper.

Pulls three things from the Maine State Lottery site and joins them by game number:

  1. The "Unclaimed Prizes" table  -> percent unsold + per-top-prize counts remaining
       https://www.mainelottery.com/players_info/unclaimed_prizes.html
  2. The per-price-point index pages -> ticket image + link to each game's article
       https://www.mainelottery.com/instant/index.html
  3. Each game's article page (on maine.gov) -> total tickets printed, overall odds, etc.

From these it computes, for each top prize level of each game, the real odds of
winning that prize on a single ticket bought today:

    tickets_remaining = tickets_printed * (percent_unsold / 100)
    odds_one_in       = tickets_remaining / prizes_of_that_level_remaining

Output: site/data.json  (consumed by the static front-end)

Stdlib + requests + beautifulsoup4 only, so it runs cheaply in CI.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.mainelottery.com"
UNCLAIMED_URL = f"{BASE}/players_info/unclaimed_prizes.html"
INDEX_URL = f"{BASE}/instant/index.html"
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "site", "data.json")

HEADERS = {
    "User-Agent": (
        "maine-lottery-odds/1.0 (public-data aggregator; "
        "contact via GitHub repo issues)"
    )
}

session = requests.Session()
session.headers.update(HEADERS)


def get(url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"failed to fetch {url}: {last}")


def money_to_int(text: str) -> int | None:
    """'$1,560,000.00' / '$1000' -> int dollars, ignoring cents."""
    if not text:
        return None
    m = re.search(r"\$?\s*([\d,]+)", text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


# --------------------------------------------------------------------------- #
# 1. Unclaimed prizes table
# --------------------------------------------------------------------------- #
def parse_unclaimed() -> dict[int, dict]:
    """game_number -> {price, name, percent_unsold, total_unclaimed, prizes:[...]}"""
    soup = BeautifulSoup(get(UNCLAIMED_URL), "html.parser")
    table = soup.find("table", class_="tbstriped") or soup.find("table")
    games: dict[int, dict] = {}
    current: dict | None = None

    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 7:
            continue  # header / malformed
        price, gameno, name, pct, total, level, count = cells[:7]

        if gameno:  # new game row
            try:
                gn = int(gameno)
            except ValueError:
                continue
            current = {
                "game_number": gn,
                "price": money_to_int(price),
                "name": name,
                "percent_unsold": float(pct) if pct else None,
                "total_unclaimed": money_to_int(total),
                "prizes": [],
            }
            games[gn] = current

        if current is not None and level:
            prize_value = money_to_int(level)
            try:
                remaining = int(re.sub(r"[^\d]", "", count)) if count else None
            except ValueError:
                remaining = None
            if prize_value is not None and remaining is not None:
                current["prizes"].append(
                    {"prize": prize_value, "remaining": remaining}
                )

    return games


# --------------------------------------------------------------------------- #
# 2. Price-point index pages -> per-game article link + ticket image
# --------------------------------------------------------------------------- #
def parse_price_pages() -> list[dict]:
    """Returns [{name, article_url, image_url}] for every game currently listed."""
    index = BeautifulSoup(get(INDEX_URL), "html.parser")
    price_pages = set()
    for a in index.find_all("a", href=True):
        if re.search(r"scratch\d+dollar\.html", a["href"]):
            price_pages.add(urljoin(INDEX_URL, a["href"]))

    games: list[dict] = []
    for purl in sorted(price_pages):
        try:
            page = BeautifulSoup(get(purl), "html.parser")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! skipping {purl}: {exc}", file=sys.stderr)
            continue
        for a in page.find_all("a", href=True):
            if "Lottery_Scratch" not in a["href"]:
                continue
            h2 = a.find("h2")
            img = a.find("img")
            games.append(
                {
                    "name": h2.get_text(strip=True) if h2 else None,
                    "article_url": urljoin(purl, a["href"].replace("&amp;", "&")),
                    "image_url": urljoin(purl, img["src"].replace("&amp;", "&"))
                    if img and img.get("src")
                    else None,
                }
            )
    return games


# --------------------------------------------------------------------------- #
# 3. Individual game article page
# --------------------------------------------------------------------------- #
def parse_article(entry: dict) -> dict | None:
    try:
        html = get(entry["article_url"])
    except Exception as exc:  # noqa: BLE001
        print(f"  ! article fetch failed {entry['article_url']}: {exc}", file=sys.stderr)
        return None

    text = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))

    gm = re.search(r"Game\s*#\s*(\d+)", text)
    tp = re.search(r"Tickets\s+Printed\s*[:\-]?\s*([\d,]+)", text, re.IGNORECASE)
    if not gm or not tp:
        return None

    def grab(pat):
        m = re.search(pat, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    return {
        "game_number": int(gm.group(1)),
        "tickets_printed": int(tp.group(1).replace(",", "")),
        "max_award": money_to_int(grab(r"Maximum Award\s*[:\-]?\s*(\$[\d,]+)")),
        "overall_odds": grab(r"OVERALL ODDS OF WINNING\s*1:\s*([\d,.]+)"),
        "top_prize_odds": grab(r"HIGHEST INSTANT PRIZE ODDS\s*1:\s*([\d,.]+)"),
        "on_sale": grab(
            r"On Sale\s*[:\-]?\s*([A-Za-z]+ \d{1,2}, \d{4})"
        ),
        "name": entry.get("name"),
        "image_url": entry.get("image_url"),
        "article_url": entry["article_url"],
    }


def fetch_articles(entries: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(parse_article, entries):
            if res:
                out[res["game_number"]] = res  # later price pages win ties; fine
    return out


# --------------------------------------------------------------------------- #
# Join + compute odds
# --------------------------------------------------------------------------- #
def build() -> dict:
    print("Fetching unclaimed-prizes table…", file=sys.stderr)
    unclaimed = parse_unclaimed()
    print(f"  {len(unclaimed)} games in table", file=sys.stderr)

    print("Fetching price-point pages…", file=sys.stderr)
    listed = parse_price_pages()
    print(f"  {len(listed)} game links found", file=sys.stderr)

    print("Fetching individual game articles…", file=sys.stderr)
    articles = fetch_articles(listed)
    print(f"  {len(articles)} articles with tickets-printed", file=sys.stderr)

    games = []
    for gn, t in unclaimed.items():
        art = articles.get(gn)
        if not art or not art.get("tickets_printed"):
            continue  # can't compute odds without printed count -> skip

        printed = art["tickets_printed"]
        pct = t["percent_unsold"]
        tickets_remaining = printed * (pct / 100.0) if pct is not None else None

        prizes = []
        for p in t["prizes"]:
            odds = None
            if tickets_remaining and p["remaining"]:
                odds = round(tickets_remaining / p["remaining"])
            prizes.append(
                {
                    "prize": p["prize"],
                    "remaining": p["remaining"],
                    "odds_one_in": odds,
                }
            )
        prizes.sort(key=lambda p: p["prize"], reverse=True)

        games.append(
            {
                "game_number": gn,
                "name": t["name"] or art.get("name"),
                "price": t["price"],
                "percent_unsold": pct,
                "total_unclaimed": t["total_unclaimed"],
                "tickets_printed": printed,
                "tickets_remaining": round(tickets_remaining) if tickets_remaining else None,
                "max_award": art.get("max_award"),
                "overall_odds": float(art["overall_odds"].replace(",", "")) if art.get("overall_odds") else None,
                "on_sale": art.get("on_sale"),
                "image_url": art.get("image_url"),
                "article_url": art.get("article_url"),
                "prizes": prizes,
            }
        )

    games.sort(key=lambda g: (-(g["price"] or 0), g["game_number"]))

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "unclaimed_prizes": UNCLAIMED_URL,
            "instant_index": INDEX_URL,
        },
        "counts": {
            "in_unclaimed_table": len(unclaimed),
            "matched_with_printed": len(games),
        },
        "games": games,
    }


def main() -> None:
    data = build()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print(
        f"Wrote {os.path.relpath(OUT_PATH)} — {len(data['games'])} games "
        f"({data['counts']['in_unclaimed_table']} in table)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
