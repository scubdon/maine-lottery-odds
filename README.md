# The Real Odds — Maine Scratch Tickets

A small, self-updating website that shows the **actual odds of winning a top prize**
on Maine State Lottery scratch tickets — the number the lottery doesn't print on the
ticket. It's calculated from the prizes still unclaimed and the tickets still unsold.

The lottery only advertises the *overall* odds of winning **anything** (usually a free
ticket or a couple of dollars back). This site works out, per game and per prize level,
how unlikely you are to win a prize actually worth more than the ticket.

## How it works

```
┌─ scraper/scrape.py ─┐        ┌─ site/ (static) ─┐
│  Maine Lottery site │ ──────▶│  index.html      │
│  → site/data.json   │  JSON  │  app.js + chart  │
└─────────────────────┘        └──────────────────┘
        ▲ run daily in CI            ▲ served by GitHub Pages
```

Three sources are scraped and joined on the game number:

| Source | Provides |
| --- | --- |
| [Unclaimed prizes table](https://www.mainelottery.com/players_info/unclaimed_prizes.html) | percent of tickets unsold, and how many of each top prize remain unclaimed |
| [Instant-game index](https://www.mainelottery.com/instant/index.html) → price-point pages | each game's ticket image and link to its page |
| Each game's article page (on maine.gov) | **total tickets printed**, overall odds, on-sale date |

The odds shown are then:

```
odds of winning a prize = (tickets printed × percent unsold) ÷ that prize still unclaimed
```

When the lottery takes down a game's individual page, its printed count is instead read
from an **archived copy of that page on the [Wayback Machine](https://web.archive.org/)**
(the printed count never changes, so an old snapshot stays valid while the live unsold-%
keeps updating). Those games are marked "archived" on the site. Only games that were never
archived at all are omitted.

## Project layout

```
scraper/
  scrape.py              # the whole pipeline → writes site/data.json
  wayback_harvest.py     # one-off: rebuild wayback_articles.json from the Wayback Machine
  wayback_articles.json  # archived printed-ticket counts for delisted games (committed)
  requirements.txt
site/                # the deployable static site (no build step)
  index.html
  styles.css
  app.js
  data.json          # generated; committed so the site works before the first CI run
.github/workflows/
  update.yml         # daily scrape + deploy to GitHub Pages
```

## Run it locally

```bash
python3 -m venv .venv
./.venv/bin/pip install -r scraper/requirements.txt

# refresh the data
./.venv/bin/python scraper/scrape.py

# preview the site
./.venv/bin/python -m http.server -d site 8765
# open http://localhost:8765
```

## Deploying (free + low-maintenance)

The site is **plain static files** — no framework, no build step — so it can be hosted
anywhere. The included workflow uses **GitHub Pages**:

1. Push this repo to GitHub.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. The `Refresh data & deploy` workflow runs on push, daily at 11:17 UTC, and on demand
   (Actions tab → *Run workflow*). Each run re-scrapes and redeploys, so the published
   odds stay current with zero ongoing effort or cost.

To host elsewhere (Cloudflare Pages, Netlify, S3, …) just serve the `site/` directory and
run `scraper/scrape.py` on a schedule to refresh `site/data.json`.

## Caveats

These are estimates. They assume the unsold tickets are spread through the pool the same
way the printed pool is (the lottery doesn't guarantee that), and "unclaimed" prizes may
already be sitting in a winner's drawer. Not affiliated with or endorsed by the Maine
State Lottery.
