# Engineering Reader

One feed for 35 engineering blogs — Meta, Netflix, Airbnb, Cloudflare, Stripe, AWS, GitHub, Slack, Discord,
Dropbox, Spotify, Pinterest, Shopify, Google, Google Research, Microsoft, Apple ML, NVIDIA, OpenAI, Hugging Face,
Lyft, Instacart, Booking.com, Square, Etsy, Figma, Canva, Salesforce, Mozilla, Snap, Yelp, Grab, Zalando, Walmart
and Expedia — including their archives.

Not included: Uber (its blog rejects non-browser requests) and LinkedIn (its robots.txt disallows all crawlers).

Archives can't be fetched for Pinterest, Walmart and Expedia (Medium blocks archive pages), Airbnb's
pre-2026 Medium posts, or Etsy (archive pages return 403) — those show only what their feeds list.

**Live site:** https://chowpawan.github.io/eng-blog-reader/

## How it works

- `app.py update` reads each blog's RSS/Atom feed, then finds old posts by paging back through feeds,
  reading sitemaps, or reading archive/category pages, and writes everything to `site/posts.json`.
  Posts found that way are read once for their real title and date (up to 700 per blog per run, so a
  big archive fills in over a few runs); until then they're hidden.
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
`sitemap` + `sitemap_include` / `sitemap_child_include` regexes, `index` (listing pages such as a
category page) + `index_include` regex for post links, and `enrich` (read each sitemap post's page
once for its real title and date).
