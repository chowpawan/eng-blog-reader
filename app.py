#!/usr/bin/env python3
"""Engineering blog reader: aggregates tech company engineering blogs into one static site.

Usage:
  python3 app.py update   # fetch new and archived posts into site/posts.json
  python3 app.py serve    # update, then serve the reader at http://localhost:8787 (re-updates hourly)
"""
import functools
import gzip
import html
import json
import re
import sys
import threading
import time
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA_PATH = SITE / "posts.json"
PORT = 8787
USER_AGENT = "EngBlogReader/1.0 (personal RSS reader; +https://github.com/chowpawan/eng-blog-reader)"
REFRESH_EVERY = 60 * 60  # seconds, serve mode only
MAX_PAGES = 300
MAX_ENRICH_PER_RUN = 700  # per blog; keeps each scheduled run bounded, the rest continues next run
POLITE_DELAY = 0.5  # seconds between archive requests


def log(msg):
    print(msg, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_sources():
    return json.loads((ROOT / "sources.json").read_text())


# ---------- storage ----------
# posts.json keeps short keys to stay small: s=source u=url t=title d=summary a=author p=published
# x marks a post known only from a sitemap or listing page: 1 = shown with a URL-derived title and
# approximate date, 2 = hidden until its page is read for details, 3 = page had no usable details after 3
# tries (stays hidden); n counts those tries.

def key(url):
    """Same post with and without a trailing slash counts once."""
    return url.rstrip("/")


class Store:
    def __init__(self):
        self.posts = {}
        if DATA_PATH.exists():
            for p in json.loads(DATA_PATH.read_text())["posts"]:
                old = self.posts.get(key(p["u"]))
                if old is None or (old.get("x") and not p.get("x")):  # keep the copy with real details
                    self.posts[key(p["u"])] = p

    def add(self, source_id, posts, from_sitemap=False, pending=False):
        """Add posts; returns how many URLs were new. Feed entries upgrade sitemap-only posts."""
        new = 0
        first_seen = None if from_sitemap else now_iso()  # some feeds omit dates
        for p in posts:
            rec = {"s": source_id, "u": p["url"], "t": p["title"], "d": p.get("summary", ""),
                   "a": p.get("author", ""), "p": p.get("published") or first_seen}
            if from_sitemap:
                rec["x"] = 2 if pending else 1
            old = self.posts.get(key(p["url"]))
            if old is None:
                self.posts[key(p["url"])] = rec
                new += 1
            elif old.get("x") and not from_sitemap:
                rec["p"] = p.get("published") or old["p"]
                self.posts[key(p["url"])] = rec
        return new

    def save(self):
        sources = [{"id": s["id"], "name": s["name"]} for s in load_sources()]
        posts = sorted(self.posts.values(), key=lambda p: p.get("p") or "", reverse=True)
        out = {"generated": now_iso(), "sources": sources, "posts": posts}
        SITE.mkdir(exist_ok=True)
        tmp = DATA_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(DATA_PATH)


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


def meta_content(page, prop):
    for tag in re.findall(r"<meta\b[^>]*>", page):
        if re.search(r'(?:property|name)=["\']%s["\']' % re.escape(prop), tag):
            m = re.search(r'content="([^"]*)"|content=\'([^\']*)\'', tag)
            if m:
                return html.unescape(m.group(1) or m.group(2) or "").strip()
    return ""


DATE_METAS = ("article:published_time", "publishDate", "publish_date", "date", "DC.date.issued")


def page_date(page, url="", text_dates=False):
    """Best-effort publish date: meta tags, JSON-LD, <time>, a /YYYY/MM/ URL, then (if allowed) a written date."""
    candidates = [meta_content(page, name) for name in DATE_METAS] + [
        (re.search(r'"datePublished"\s*:\s*"([^"]+)"', page) or [None, None])[1],
        (re.search(r'<time[^>]+datetime="([^"]+)"', page) or [None, None])[1]]
    for text in candidates:
        if parse_date(text):
            return parse_date(text)
    m = re.search(r"/(20\d\d|19\d\d)/(\d\d)/(?:(\d\d)/)?", url)
    if m:
        return f"{m[1]}-{m[2]}-{m[3] or '01'}T00:00:00Z"
    if not text_dates:  # a date written in the page text is often a different date, so it's opt-in
        return None
    m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                  r" (\d{1,2}), (20\d\d)\b", page)
    if m:
        return datetime.strptime(" ".join(m.groups()), "%B %d %Y").strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


# ---------- update ----------

def feeds(source):
    return source["feed"] if isinstance(source["feed"], list) else [source["feed"]]


def refresh(store):
    total = 0
    for s in load_sources():
        for feed in feeds(s):
            try:
                total += store.add(s["id"], parse_feed(fetch(feed)))
            except Exception as e:  # one broken feed shouldn't stop the rest
                log(f"[feed] {s['name']}: {e}")
    log(f"Feeds: {total} new posts")


def index_links(url, include):
    page = fetch(url).decode("utf-8", "replace")
    links = {urljoin(url, h) for h in re.findall(r'href="([^"#?]+)"', page)}
    return [u for u in sorted(links) if re.search(include, u)]


def backfill(store):
    total = 0
    for s in load_sources():
        pending = bool(s.get("enrich"))
        if s.get("paged"):
            for feed in feeds(s):
                sep = "&" if "?" in feed else "?"
                for page in range(2, MAX_PAGES + 1):
                    try:
                        posts = parse_feed(fetch(f"{feed}{sep}paged={page}"))
                    except Exception:
                        break  # past the last page (usually a 404)
                    new = store.add(s["id"], posts)
                    total += new
                    if not posts or new == 0:
                        break
                    time.sleep(POLITE_DELAY)
        if s.get("sitemap"):
            try:
                entries = [{"url": loc, "title": title_from_slug(loc), "published": parse_date(lastmod)}
                           for loc, lastmod in parse_sitemap(s["sitemap"], s.get("sitemap_include"),
                                                             s.get("sitemap_child_include"))]
                total += store.add(s["id"], entries, from_sitemap=True, pending=pending)
            except Exception as e:
                log(f"[sitemap] {s['name']}: {e}")
        for index_url in s.get("index", []):  # listing pages; "{n}" pages through 2, 3, ... until one adds nothing
            numbered = "{n}" in index_url
            for n in range(2, MAX_PAGES + 1) if numbered else [None]:
                try:
                    links = index_links(index_url.format(n=n) if numbered else index_url, s["index_include"])
                except Exception as e:
                    if not numbered:
                        log(f"[index] {s['name']}: {e}")
                    break
                entries = [{"url": u, "title": title_from_slug(u), "published": None} for u in links]
                new = store.add(s["id"], entries, from_sitemap=True, pending=pending)
                total += new
                if numbered and new == 0:
                    break
                time.sleep(POLITE_DELAY)
    log(f"Archives: {total} new posts")
    # Each blog is read one page at a time, but different blogs are read in parallel.
    workers = [threading.Thread(target=enrich, args=(store, s)) for s in load_sources() if s.get("enrich")]
    for w in workers:
        w.start()
    for w in workers:
        w.join()


def enrich(store, source):
    """Sitemaps and listing pages only give URLs; read each post's page once for its real title and date."""
    pending = [p for p in store.posts.values() if p["s"] == source["id"] and p.get("x") in (1, 2)]
    done = 0
    for p in pending[:MAX_ENRICH_PER_RUN]:
        try:
            page = fetch(p["u"]).decode("utf-8", "replace")
        except Exception:
            continue  # retried next run
        title = meta_content(page, "og:title") or html.unescape((re.search(r"<title>([^<]*)", page) or [0, ""])[1]).strip()
        title = re.sub(r"\s*(?:\||-|–|—)\s*[^|–—]{0,40}(?:Blog|Engineering|Amazon Web Services)\s*$", "", title)
        title = re.sub(r"^Stripe Blog:\s*", "", title)
        published = page_date(page, p["u"], source.get("text_dates", False))
        if title and published:
            p.update(t=title, d=clean_summary(meta_content(page, "og:description")),
                     a=meta_content(page, "author"), p=published)
            p.pop("x", None)
            p.pop("n", None)
            done += 1
        else:  # not a post (e.g. a category page), no date, or a temporary error page; give up after 3 runs
            p["n"] = p.get("n", 0) + 1
            p["x"] = 3 if p["n"] >= 3 else 2
        time.sleep(POLITE_DELAY)
    left = sum(1 for p in store.posts.values() if p["s"] == source["id"] and p.get("x") in (1, 2))
    log(f"Details: {source['name']} {done} posts updated, {left} left")


def update():
    store = Store()
    refresh(store)
    store.save()  # make new posts visible before the slower archive pass
    backfill(store)
    store.save()
    log(f"Saved {len(store.posts)} posts to {DATA_PATH.relative_to(ROOT)}")


# ---------- local server ----------

def serve():
    def loop():
        while True:
            try:
                update()
            except Exception as e:
                log(f"[update] {e}")
            time.sleep(REFRESH_EVERY)

    threading.Thread(target=loop, daemon=True).start()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *args: None
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    url = f"http://localhost:{PORT}"
    log(f"Engineering blog reader running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        update()
    else:
        serve()
