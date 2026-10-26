#!/usr/bin/env python3
"""Engineering blog reader: aggregates tech company engineering blogs into one local feed.

Usage:
  python3 app.py              # start the reader at http://localhost:8787
  python3 app.py --backfill   # fetch archives (old posts) once, then exit
"""
import gzip
import html
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "posts.db"
PORT = 8787
USER_AGENT = "EngBlogReader/1.0 (personal RSS reader)"
REFRESH_EVERY = 60 * 60  # seconds
MAX_PAGES = 300
POLITE_DELAY = 0.5  # seconds between archive requests

status = {"running": None, "message": "Idle", "last_refresh": None}
status_lock = threading.Lock()
job_lock = threading.Lock()


def set_status(**kw):
    with status_lock:
        status.update(kw)


def load_sources():
    return json.loads((ROOT / "sources.json").read_text())


# ---------- database ----------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                summary TEXT DEFAULT '',
                author TEXT DEFAULT '',
                published TEXT,
                from_sitemap INTEGER DEFAULT 0,
                is_read INTEGER DEFAULT 0,
                saved INTEGER DEFAULT 0,
                added TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source);
        """)


def save_posts(source_id, posts, from_sitemap=False):
    """Insert posts; returns the number of URLs not seen before.

    Feed entries upgrade rows that only came from a sitemap (better title/summary)."""
    new = 0
    first_seen = None if from_sitemap else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db() as conn:
        for p in posts:
            cur = conn.execute("SELECT from_sitemap FROM posts WHERE url = ?", (p["url"],))
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO posts (source, url, title, summary, author, published, from_sitemap)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (source_id, p["url"], p["title"], p.get("summary", ""), p.get("author", ""),
                     p.get("published") or first_seen, int(from_sitemap)))  # some feeds omit dates
                new += 1
            elif row["from_sitemap"] and not from_sitemap:
                conn.execute(
                    "UPDATE posts SET title = ?, summary = ?, author = ?,"
                    " published = COALESCE(?, published), from_sitemap = 0 WHERE url = ?",
                    (p["title"], p.get("summary", ""), p.get("author", ""), p.get("published"), p["url"]))
    return new


# ---------- fetching & parsing ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child(el, name):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def child_text(el, *names):
    for name in names:
        c = child(el, name)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_summary(raw, limit=320):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def parse_feed(data):
    root = ET.fromstring(data)
    posts = []
    for el in root.iter():
        kind = local(el.tag)
        if kind == "item":  # RSS
            url = child_text(el, "link")
            date = child_text(el, "pubDate", "date", "published", "updated")
            summary = child_text(el, "description", "encoded")
            author = child_text(el, "creator", "author")
        elif kind == "entry":  # Atom
            url = ""
            for c in el:
                if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    url = c.get("href", "")
                    break
            date = child_text(el, "published", "updated")
            summary = child_text(el, "summary", "content")
            a = child(el, "author")
            author = child_text(a, "name") if a is not None else ""
        else:
            continue
        title = html.unescape(child_text(el, "title"))
        if not url or not title:
            continue
        posts.append({"url": url.split("?source=")[0], "title": title, "summary": clean_summary(summary),
                      "author": author, "published": parse_date(date)})
    return posts


def title_from_slug(url):
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-[0-9a-f]{10,12}$", "", slug)  # Medium post id suffix
    words = slug.replace("_", "-").split("-")
    return " ".join(w if w.isupper() else w.capitalize() for w in words if w) or url


def parse_sitemap(url, include, child_include, seen=None):
    """Yield (url, lastmod) for posts, following sitemap indexes."""
    seen = seen if seen is not None else set()
    if url in seen:
        return
    seen.add(url)
    root = ET.fromstring(fetch(url))
    if local(root.tag) == "sitemapindex":
        for sm in root:
            loc = child_text(sm, "loc")
            if loc and (not child_include or re.search(child_include, loc)):
                time.sleep(POLITE_DELAY)
                yield from parse_sitemap(loc, include, child_include, seen)
        return
    for u in root:
        loc = child_text(u, "loc")
        if loc and (not include or re.search(include, loc)):
            yield loc, child_text(u, "lastmod")


# ---------- jobs ----------

def refresh_all():
    sources = load_sources()
    total = 0
    for i, s in enumerate(sources, 1):
        set_status(message=f"Checking {s['name']} ({i}/{len(sources)})")
        try:
            total += save_posts(s["id"], parse_feed(fetch(s["feed"])))
        except Exception as e:  # one broken feed shouldn't stop the rest
            print(f"[refresh] {s['name']}: {e}", file=sys.stderr)
    set_status(last_refresh=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return total


def backfill_all():
    total = 0
    for s in load_sources():
        if s.get("paged"):
            sep = "&" if "?" in s["feed"] else "?"
            for page in range(2, MAX_PAGES + 1):
                set_status(message=f"Archives: {s['name']} page {page}")
                try:
                    posts = parse_feed(fetch(f"{s['feed']}{sep}paged={page}"))
                except Exception:
                    break  # past the last page (usually a 404)
                new = save_posts(s["id"], posts)
                total += new
                if not posts or new == 0:
                    break
                time.sleep(POLITE_DELAY)
        if s.get("sitemap"):
            set_status(message=f"Archives: {s['name']} sitemap")
            try:
                entries = [{"url": loc, "title": title_from_slug(loc), "published": parse_date(lastmod)}
                           for loc, lastmod in parse_sitemap(s["sitemap"], s.get("sitemap_include"),
                                                             s.get("sitemap_child_include"))]
                total += save_posts(s["id"], entries, from_sitemap=True)
            except Exception as e:
                print(f"[backfill] {s['name']} sitemap: {e}", file=sys.stderr)
        if s.get("enrich"):
            enrich_sitemap_posts(s)
    return total


def meta_content(page, prop):
    m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop), page)
    return html.unescape(m.group(1)).strip() if m else ""


def enrich_sitemap_posts(source):
    """Sitemaps only give URLs and last-modified dates; read each page once for its real title and date."""
    with db() as conn:
        rows = conn.execute("SELECT url FROM posts WHERE source = ? AND from_sitemap = 1",
                            (source["id"],)).fetchall()
    for i, row in enumerate(rows, 1):
        set_status(message=f"Archives: {source['name']} details {i}/{len(rows)}")
        try:
            page = fetch(row["url"]).decode("utf-8", "replace")
        except Exception:
            continue  # retried on the next archive run
        title = meta_content(page, "og:title")
        published = parse_date(meta_content(page, "article:published_time"))
        if title and published:
            with db() as conn:
                conn.execute("UPDATE posts SET title = ?, summary = ?, author = ?, published = ?,"
                             " from_sitemap = 0 WHERE url = ?",
                             (title, clean_summary(meta_content(page, "og:description")),
                              meta_content(page, "author"), published, row["url"]))
        time.sleep(POLITE_DELAY)


def run_job(name, fn):
    if not job_lock.acquire(blocking=False):
        return False

    def work():
        set_status(running=name)
        try:
            n = fn()
            set_status(message=f"{name.capitalize()} done: {n} new posts")
        finally:
            set_status(running=None)
            job_lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def scheduler():
    while True:
        run_job("refresh", refresh_all)
        time.sleep(REFRESH_EVERY)


# ---------- web server ----------

def query_posts(params):
    where, args = [], []
    if params.get("source"):
        where.append("source = ?")
        args.append(params["source"])
    if params.get("q"):
        for term in params["q"].split():
            where.append("(title LIKE ? OR summary LIKE ? OR author LIKE ?)")
            args += [f"%{term}%"] * 3
    if params.get("filter") == "unread":
        where.append("is_read = 0")
    elif params.get("filter") == "saved":
        where.append("saved = 1")
    if params.get("year"):
        where.append("substr(published, 1, 4) = ?")
        args.append(params["year"])
    sql = "FROM posts" + (" WHERE " + " AND ".join(where) if where else "")
    limit = min(int(params.get("limit", 50)), 200)
    offset = int(params.get("offset", 0))
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) " + sql, args).fetchone()[0]
        rows = conn.execute(
            "SELECT id, source, url, title, summary, author, published, is_read, saved " + sql +
            " ORDER BY published IS NULL, published DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": count, "posts": [dict(r) for r in rows]}


def source_stats():
    with db() as conn:
        rows = {r["source"]: dict(r) for r in conn.execute(
            "SELECT source, COUNT(*) AS total, SUM(is_read = 0) AS unread,"
            " MIN(published) AS oldest FROM posts GROUP BY source")}
        years = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(published, 1, 4) FROM posts WHERE published IS NOT NULL ORDER BY 1 DESC")]
    out = []
    for s in load_sources():
        r = rows.get(s["id"], {})
        out.append({"id": s["id"], "name": s["name"], "total": r.get("total", 0),
                    "unread": r.get("unread", 0) or 0, "oldest": r.get("oldest")})
    return {"sources": out, "years": years}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/posts":
            self.send_json(query_posts(params))
        elif u.path == "/api/sources":
            self.send_json(source_stats())
        elif u.path == "/api/status":
            with status_lock:
                self.send_json(dict(status))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if u.path == "/api/mark":
            field = {"read": "is_read", "saved": "saved"}.get(body.get("field"))
            if not field:
                return self.send_json({"error": "bad field"}, 400)
            with db() as conn:
                if body.get("all_unread"):  # mark everything matching as read
                    conn.execute("UPDATE posts SET is_read = 1" +
                                 (" WHERE source = ?" if body.get("source") else ""),
                                 [body["source"]] if body.get("source") else [])
                else:
                    conn.execute(f"UPDATE posts SET {field} = ? WHERE id = ?",
                                 (int(bool(body.get("value"))), body.get("id")))
            self.send_json({"ok": True})
        elif u.path == "/api/refresh":
            self.send_json({"started": run_job("refresh", refresh_all)})
        elif u.path == "/api/backfill":
            self.send_json({"started": run_job("archives", backfill_all)})
        else:
            self.send_json({"error": "not found"}, 404)


def main():
    init_db()
    if "--backfill" in sys.argv:
        print("Fetching latest posts…")
        refresh_all()
        print("Fetching archives (this can take a few minutes)…")
        print(f"Done: {backfill_all()} older posts added.")
        return
    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Engineering blog reader running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
