#!/usr/bin/env python3
"""
Single-file Google Scholar watcher with:
- FastAPI options panel (add/remove keywords, manual run)
- SerpAPI Google Scholar fetching (sort by date)
- SQLite persistence for keywords and deduplication
- APScheduler background job
- Discord webhook notifications
- Optional RSS feed endpoint for recently seen items

Usage
-----
1) Create a .env next to this script with:

DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXXX/XXXXXXXX
SERPAPI_KEY=your_serpapi_key
SCHEDULE_MINUTES=15
PER_KEYWORD_LIMIT=10
RSS_LIMIT=100

2) Install deps:
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install fastapi uvicorn[standard] requests python-dotenv apscheduler sqlite-utils pydantic scholarly

3) Run:
   uvicorn scholar_discord_watcher:app --host 0.0.0.0 --port 8080
   # open http://localhost:8080

Notes
-----
- SQLite database file: rssscholar.db (created automatically).
- If you change SCHEDULE_MINUTES in .env, restart the server to apply.
"""

from __future__ import annotations
from scholarly import scholarly

import hashlib
import html
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, Response

# ---------------------- Config ----------------------
load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
SCHEDULE_MINUTES = int(os.getenv("SCHEDULE_MINUTES", "15"))
PER_KEYWORD_LIMIT = int(os.getenv("PER_KEYWORD_LIMIT", "10"))
RSS_LIMIT = int(os.getenv("RSS_LIMIT", "100"))

KEYWORDS_ENV_LOCK = os.getenv("KEYWORDS_ENV_LOCK", "false").lower() in {"1","true","yes"}

DB_PATH = os.getenv("DB_PATH", "rssscholar.db")

KEYWORDS = os.getenv("KEYWORDS", "")
if KEYWORDS:
    DEFAULT_KEYWORDS = [kw.strip() for kw in KEYWORDS.split(",") if kw.strip()]
else:
    DEFAULT_KEYWORDS = []

USER_AGENT = (
    "ScholarWatcher/1.0 (+https://github.com/) Python-requests"
)

# ---------------------- App ----------------------
app = FastAPI(title="Scholar RSS → Discord", version="1.0.0")
scheduler = BackgroundScheduler()

# ---------------------- DB helpers ----------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keywords (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              term TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_papers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kw_term TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              title TEXT,
              url TEXT,
              authors TEXT,
              year TEXT,
              first_seen TEXT NOT NULL,
              UNIQUE(kw_term, fingerprint)
            )
            """
        )
        # helpful index
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_kw_first ON seen_papers(kw_term, first_seen DESC)"
        )


# ---------------------- Utilities ----------------------

def _fingerprint(title: str, authors: str, year: str, url: str) -> str:
    base = f"{title.strip()}|{authors.strip()}|{year.strip()}|{url.strip()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# ---------------------- Scholar (SerpAPI) ----------------------

def fetch_scholar_results(keyword: str, num: int = 10):
    search = scholarly.search_pubs(keyword)
    results = []
    for _ in range(num):
        try:
            pub = next(search)
        except StopIteration:
            break
        results.append({
            "title": pub.bib.get("title", ""),
            "url": pub.bib.get("url", ""),
            "authors": ", ".join(pub.bib.get("author", [])),
            "year": pub.bib.get("year", "")
        })
    return results



# ---------------------- Discord ----------------------

def send_to_discord(keyword: str, paper: Dict[str, str]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL missing; set it in .env")

    title = paper.get("title") or "Untitled"
    url = paper.get("url") or ""
    authors = paper.get("authors") or "Unknown authors"
    year = paper.get("year") or "Year n/a"

    content = (
        f"**New paper found** for **{keyword}**\n"
        f"**{title}** ({year})\n"
        f"*{authors}*\n"
        f"{url}"
    )
    payload = {"content": content}

    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers=headers, timeout=30)
    r.raise_for_status()


# ---------------------- Core watcher ----------------------

def process_keyword(keyword: str) -> int:
    new_count = 0
    rows = fetch_scholar_results(keyword, num=PER_KEYWORD_LIMIT)
    with db() as conn:
        for p in rows:
            fp = _fingerprint(p["title"], p["authors"], p["year"], p["url"]) 
            try:
                conn.execute(
                    (
                        "INSERT INTO seen_papers (kw_term, fingerprint, title, url, authors, year, first_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        keyword,
                        fp,
                        p.get("title"),
                        p.get("url"),
                        p.get("authors"),
                        p.get("year"),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                # If insert ok → not seen before
                try:
                    send_to_discord(keyword, p)
                except Exception as notify_err:
                    # If Discord fails, roll back this insert so it retries next cycle
                    conn.execute("DELETE FROM seen_papers WHERE kw_term=? AND fingerprint=?", (keyword, fp))
                    raise notify_err
                new_count += 1
            except sqlite3.IntegrityError:
                # Already seen
                pass
    return new_count


def run_cycle() -> int:
    total_new = 0
    with db() as conn:
        cur = conn.execute("SELECT term FROM keywords ORDER BY term ASC")
        terms = [r[0] for r in cur.fetchall()]
    for term in terms:
        try:
            total_new += process_keyword(term)
            time.sleep(1.0)  # gentle on APIs
        except requests.HTTPError as e:
            print(f"[{datetime.now()}] HTTP error for '{term}': {e}")
        except Exception as e:
            print(f"[{datetime.now()}] Error processing '{term}': {e}")
    if total_new:
        print(f"[{datetime.now()}] New papers this cycle: {total_new}")
    return total_new


# ---------------------- HTML UI ----------------------

HTML_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Scholar RSS → Discord | Options</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; }}
    .card {{ max-width: 980px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 14px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }}
    h1 {{ margin-top: 0; }}
    form {{ display: flex; gap: 8px; margin: 16px 0; }}
    input[type=text] {{ flex: 1; padding: 10px 12px; border: 1px solid #ccc; border-radius: 10px; }}
    button {{ padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; }}
    .add {{ background: #111; color: #fff; }}
    .del {{ background: #c62828; color: #fff; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #eee; text-align: left; }}
    .muted {{ color: #666; font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .cfg {{ background: #fafafa; padding: 12px; border: 1px dashed #ddd; border-radius: 10px; }}
    .badge {{ display: inline-block; padding: 4px 8px; border-radius: 999px; background: #eef; color: #224; font-size: 12px; }}
    .right {{ text-align: right; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Scholar RSS → Discord</h1>
    <p class="muted">Watches Google Scholar via SerpAPI for new papers matching your keywords and notifies a Discord channel.</p>

    <h2>Keywords</h2>
    <form method="post" action="/add">
      <input name="term" type="text" placeholder="Add a keyword, e.g. graph attention networks cybersecurity" required>
      <button class="add" type="submit">Add</button>
    </form>

    <table>
      <thead><tr><th>Keyword</th><th style="width:200px;" class="right">Actions</th></tr></thead>
      <tbody>
        {rows}
      </tbody>
    </table>

    <h2>Config</h2>
    <div class="grid">
      <div class="cfg">
        <div><span class="badge">Schedule</span> every <b>{schedule}</b> minutes</div>
        <div><span class="badge">Per-run cap</span> <b>{limit}</b> results / keyword</div>
      </div>
      <div class="cfg">
        <div><span class="badge">Discord</span> {discord}</div>
        <div><span class="badge">SerpAPI</span> {serp}</div>
      </div>
    </div>

    <h2>Manual</h2>
    <form method="post" action="/run-now">
      <button class="add" type="submit">Run a cycle now</button>
    </form>

    <h2>RSS</h2>
    <p class="muted">Expose recent items as RSS for other tools: <span class="mono">/rss</span> or <span class="mono">/rss?kw=your+keyword</span></p>

    <p class="muted">Health: <span class="mono"><a href="/health">/health</a></span> • Status: <span class="mono"><a href="/status">/status</a></span></p>
  </div>
</body>
</html>
"""

def ok_bad(flag: bool) -> str:
    return f"<b style='color:{'green' if flag else 'crimson'}'>{'OK' if flag else 'Missing'}</b>"


@app.get("/", response_class=HTMLResponse)
def index():
    with db() as conn:
        cur = conn.execute("SELECT id, term FROM keywords ORDER BY term")
        rows = cur.fetchall()
    rows_html = "".join(
        f"<tr><td>{html.escape(term)}</td>"
        f"<td class='right'><form method='post' action='/delete' style='display:inline'>"
        f"<input type='hidden' name='id' value='{kid}'/>"
        f"<button class='del' type='submit'>Delete</button>"
        f"</form></td></tr>"
        for (kid, term) in rows
    ) or "<tr><td colspan='2' class='muted'>No keywords yet.</td></tr>"
    html_page = HTML_PAGE.format(
        rows=rows_html,
        schedule=SCHEDULE_MINUTES,
        limit=PER_KEYWORD_LIMIT,
        discord=ok_bad(bool(DISCORD_WEBHOOK_URL)),
        serp=ok_bad(bool(SERPAPI_KEY)),
    )
    return HTMLResponse(html_page)


@app.post("/add")
def add_keyword(term: str = Form(...)):
    term = term.strip()
    if term:
        with db() as conn:
            try:
                conn.execute(
                    "INSERT INTO keywords (term, created_at) VALUES (?, ?)",
                    (term, datetime.now(timezone.utc).isoformat()),
                )
            except sqlite3.IntegrityError:
                pass
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete")
def delete_keyword(id: int = Form(...)):
    with db() as conn:
        cur = conn.execute("SELECT term FROM keywords WHERE id=?", (id,))
        r = cur.fetchone()
        if r:
            term = r[0]
            conn.execute("DELETE FROM keywords WHERE id=?", (id,))
            conn.execute("DELETE FROM seen_papers WHERE kw_term=?", (term,))
    return RedirectResponse(url="/", status_code=303)


@app.post("/run-now")
def run_now():
    run_cycle()
    return RedirectResponse(url="/", status_code=303)


@app.get("/health")
def health() -> PlainTextResponse:
    ok_bits = [
        ("discord", bool(DISCORD_WEBHOOK_URL)),
        ("serpapi", bool(SERPAPI_KEY)),
    ]
    status = all(v for _, v in ok_bits)
    lines = [f"{k}: {'ok' if v else 'missing'}" for k, v in ok_bits]
    return PlainTextResponse("OK\n" + "\n".join(lines) if status else "NOT OK\n" + "\n".join(lines), status_code=200 if status else 503)


@app.get("/status")
def status() -> HTMLResponse:
    with db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM keywords")
        kws = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM seen_papers")
        seen = cur.fetchone()[0]
    body = f"<h1>Status</h1><p>Keywords: <b>{kws}</b> • Seen papers: <b>{seen}</b></p>"
    return HTMLResponse(body)


# ---------------------- RSS Endpoint ----------------------

@app.get("/rss")
def rss(kw: Optional[str] = None, limit: int = RSS_LIMIT):
    limit = max(1, min(1000, int(limit)))
    with db() as conn:
        if kw:
            cur = conn.execute(
                """
                SELECT title, url, authors, year, first_seen
                FROM seen_papers
                WHERE kw_term = ?
                ORDER BY first_seen DESC
                LIMIT ?
                """,
                (kw, limit),
            )
        else:
            cur = conn.execute(
                """
                SELECT title, url, authors, year, first_seen
                FROM seen_papers
                ORDER BY first_seen DESC
                LIMIT ?
                """,
                (limit,),
            )
        items = cur.fetchall()

    now_iso = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    title = "Scholar Watcher" + (f" — {kw}" if kw else "")
    link = "http://localhost:8080/"  # adjust in reverse proxy
    description = "Recent items detected by Scholar Watcher"

    def esc(s: str) -> str:
        return html.escape(s or "")

    rss_items = []
    for row in items:
        t, u, a, y, fs = row
        pubdate = datetime.fromisoformat(fs).strftime("%a, %d %b %Y %H:%M:%S GMT")
        rss_items.append(
            f"""
            <item>
              <title>{esc(t)} ({esc(y)})</title>
              <link>{esc(u)}</link>
              <guid isPermaLink="false">{esc(hashlib.md5((t+u+a+y).encode()).hexdigest())}</guid>
              <description>{esc(a)}</description>
              <pubDate>{pubdate}</pubDate>
            </item>
            """
        )

    rss_xml = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>{esc(title)}</title>
        <link>{esc(link)}</link>
        <description>{esc(description)}</description>
        <language>en</language>
        <lastBuildDate>{now_iso}</lastBuildDate>
        {''.join(rss_items)}
      </channel>
    </rss>
    """.strip()

    return Response(content=rss_xml, media_type="application/rss+xml")


# ---------------------- Lifecycle ----------------------

@app.on_event("startup")
def on_startup():
    init_db()
    # If KEYWORDS are defined in .env, insert them automatically (once)
    if DEFAULT_KEYWORDS:
        with db() as conn:
            for kw in DEFAULT_KEYWORDS:
                try:
                    conn.execute(
                        "INSERT INTO keywords (term, created_at) VALUES (?, ?)",
                        (kw, datetime.now(timezone.utc).isoformat()),
                    )
                except sqlite3.IntegrityError:
                    # already exists
                    pass

    # Run once at startup
    scheduler.add_job(run_cycle, "date", run_date=datetime.now())
    # Then schedule future runs
    if SCHEDULE_MINUTES > 0:
        scheduler.add_job(
            run_cycle, "interval", minutes=SCHEDULE_MINUTES, id="cycle", replace_existing=True
        )
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass


# ---------------------- Local dev entry ----------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("scholar_discord_watcher:app", host="0.0.0.0", port=8080, reload=False)
