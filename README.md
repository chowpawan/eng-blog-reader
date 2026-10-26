# Engineering Reader

One feed for engineering blogs from Meta, Netflix, Airbnb, Cloudflare, Stripe, AWS, GitHub,
Slack, Discord, Dropbox, Spotify, Pinterest, Shopify and Google — including their archives.

**Live site:** https://chowpawan.github.io/eng-blog-reader/

## How it works

- `app.py update` reads each blog's RSS/Atom feed, pages back through WordPress feeds (Meta, GitHub, Slack)
  and reads sitemaps (Netflix, Cloudflare) for old posts, then writes everything to `site/posts.json`.
- A GitHub Action (`.github/workflows/update.yml`) runs that every 3 hours, commits the new `posts.json`,
  and deploys `site/` to GitHub Pages. Run it on demand from the Actions tab ("Run workflow").
- `site/index.html` is a static page: search, filter by company/year, Unread / Saved tabs.
  Read and saved state is stored in your browser (localStorage), so it's per device.

Pure Python standard library — nothing to install.

```sh
python3 app.py update   # refresh site/posts.json
python3 app.py serve    # local copy at http://localhost:8787, updates hourly
```

## Adding a blog

Append to `sources.json`: `id`, `name`, `feed` are required. Optional: `paged` (WordPress `?paged=N` archives),
`sitemap` + `sitemap_include` / `sitemap_child_include` regexes, and `enrich` (read each sitemap post's page
once for its real title and date).
