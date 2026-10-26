# Engineering Reader

One local feed for engineering blogs from Meta, Netflix, Airbnb, Cloudflare, Stripe, AWS, GitHub,
Slack, Discord, Dropbox, Spotify, Pinterest, Shopify and Google. Pure Python standard library — nothing to install.

```sh
python3 app.py              # opens http://localhost:8787, checks for new posts every hour
python3 app.py --backfill   # one-off: pull old posts from archives, then exit
```

- **Fetch old posts** (sidebar button or `--backfill`) pages back through WordPress feeds (Meta, GitHub, Slack)
  and reads sitemaps (Netflix, Cloudflare). Sitemap-only posts get a title from the URL until the feed supplies a real one.
- Search, filter by company/year, Unread / Saved tabs. Opening a post marks it read.
- Add a blog by appending to `sources.json` (`feed` required; optional `paged`, `sitemap`, `sitemap_include` regex,
  `sitemap_child_include` regex). Restart not needed — sources are re-read on each refresh.
- Everything is stored in `posts.db` (SQLite). Delete it to start over.
