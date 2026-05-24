# Google Search Console — sitemap submission (round 34, item 1)

After v4.9.5 worker deploy, your 3 sub-sitemaps are LIVE:
  https://api.dchub.cloud/sitemap-index.xml
  https://api.dchub.cloud/sitemap-facilities.xml   (up to 50k URLs)
  https://api.dchub.cloud/sitemap-markets.xml
  https://api.dchub.cloud/sitemap-grids.xml

Note: these are on api.dchub.cloud (not dchub.cloud) because dchub.cloud/*
is routed to CF Pages. The api subdomain works for crawlers without
extra CF Workers Routes setup.

## Steps

### 1. Verify Search Console ownership (one-time, may already be done)

1. Open https://search.google.com/search-console
2. Add property: `https://dchub.cloud` (Domain property)
3. Verify via DNS TXT record at your registrar
   - If already verified from past work, skip.

### 2. Add api.dchub.cloud as a property (NEW)

1. Same Search Console UI → Add property
2. URL prefix: `https://api.dchub.cloud/`
3. Verify (DNS TXT or HTML file upload via the worker)

### 3. Submit the sitemap

1. Select the `api.dchub.cloud` property in left nav
2. Sitemaps → "Add a new sitemap"
3. Enter: `sitemap-index.xml`
4. Submit

Within 24-72h Google will start crawling the 21,000 facility pages.

### 4. Ping Google directly (optional, faster)

```bash
curl "https://www.google.com/ping?sitemap=https://api.dchub.cloud/sitemap-index.xml"
curl "https://www.bing.com/ping?sitemap=https://api.dchub.cloud/sitemap-index.xml"
```

These return a 200 quickly and queue your sitemap for re-fetch.

### 5. Monitor crawl rate

After 7 days, in Search Console:
- Coverage → see how many of your submitted URLs got indexed
- Performance → see which queries are bringing you traffic

Expected at 1k indexed: ~500 organic clicks/day from long-tail data center queries.

## Verify the sitemap is reachable

```bash
curl -sI https://api.dchub.cloud/sitemap-index.xml
# Expected: HTTP/2 200, content-type: application/xml

curl -sS https://api.dchub.cloud/sitemap-facilities.xml | head -10
# Expected: <urlset xmlns="..."> with <url> entries pointing at /facility/<id>
```
