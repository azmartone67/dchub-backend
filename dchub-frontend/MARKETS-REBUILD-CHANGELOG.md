# Markets Section Rebuild — v8.0

## What changed & why

The 60 market pages under `/markets/` were identical 90-line static HTML templates with hand-typed stats. They went stale the day they were written. The GDCI, MCP backend, and `/api/v1/*` layer already had richer data — the pages just weren't consuming it.

This rebuild replaces the static pages with **thin SEO wrappers** that are rendered by a single shared script consuming the live API. The site now **evolves on every page load** as the backend data evolves.

## Files touched

| File | Purpose | Was → Is |
|---|---|---|
| `markets/registry.json` | Single source of truth for static per-market metadata (lat/lon, ISO, country, tier, tagline, related) | _new · 59 markets_ |
| `css/market-page.css` | Shared stylesheet for all market pages and the index | _new_ |
| `js/market-page.js` | "Brain" renderer. Reads `<meta name="market-slug">`, fans out to 9 `/api/v1/*` endpoints in parallel, progressively paints each section, hides sections on failure | _new_ |
| `markets/<slug>.html` × 59 | Per-market SEO wrapper (title, description, OG, JSON-LD with geo, noscript fallback, `<meta name="market-slug">`) | _regenerated · 90 LOC → 44 LOC each_ |
| `markets/index.html` | Live market browser — search, tier chips, sort by tier/alpha/GDCI/pipeline/price | _rewritten · 1387 LOC → 180 LOC with live data_ |
| `scripts/generate-market-pages.py` | Idempotent generator — run after editing registry.json to regenerate all 59 pages | _new_ |
| `scripts/self-audit.js` | Weekly cron — audits every endpoint across every market, diffs vs last snapshot, flags movers, produces LinkedIn Top-10 payload | _new_ |
| `BUGS.md` | Living bug tracker. Already captures 2 real issues found during the rebuild (tier-config drift, string-in-numeric-field) | _new_ |

## What each market page now shows

1. **Hero** — flag, name, tier badge, GDCI score + weekly movement badge (live), tagline, description (static)
2. **Live KPIs** — facilities, operational MW, pipeline, avg $/kWh, vacancy (live from `/api/v1/markets/{slug}`)
3. **⚡ Live Grid Status** — current demand, peak today, reserve margin, interconnection queue, fuel mix (live from `/api/v1/grid?iso=…`)
4. **💰 Energy Pricing** — industrial retail rate, YoY change, state/ISO average (live from `/api/v1/energy/retail-rates?state=…`)
5. **🏢 Top Facilities** — 10 facilities with operator + power (live from `/api/v1/facilities?city=…`)
6. **🏗️ Pipeline & Absorption** — under-construction MW, planned, preleased %, top projects (live from `/api/v1/pipeline?market=…`)
7. **📰 Latest News** — 5 most recent market-tagged stories (live from `/api/v1/news?market=…`)
8. **🏛️ Tax Incentives** — state programs with savings estimates, US markets only (live from `/api/v1/tax-incentives?state=…`)
9. **🔌 Power Infrastructure** — substation count, transmission lines, gas pipelines, power plants within 50km (live from `/api/v1/infrastructure?lat=…&lon=…`)
10. **🌐 Fiber & Connectivity** — major carriers, long-haul route count, IX presence (live from `/api/v1/fiber/carriers?lat=…&lon=…`)
11. **🔗 Related Markets** — from registry
12. **Liveness stamp** — "X/Y sources responding · updated HH:MM:SS" with retry button if any fail

Every section hides gracefully if the API call returns 403/404/5xx. A 15-min client-side `sessionStorage` cache prevents duplicate fetches as the user navigates between markets.

## How to ship this

```bash
# 1. Verify generator output
python3 scripts/generate-market-pages.py
# → "generated 59 market pages in .../markets"

# 2. Smoke test locally
python3 -m http.server 8080
# Visit:  http://localhost:8080/markets/
#         http://localhost:8080/markets/los-angeles.html
#         http://localhost:8080/markets/frankfurt.html

# 3. Commit to a feature branch
git checkout -b markets-rebuild-v8
git add markets/ js/market-page.js css/market-page.css scripts/ BUGS.md MARKETS-REBUILD-CHANGELOG.md
git commit -m "Markets rebuild v8: live MCP-backed pages + self-audit cron"

# 4. Push + open PR
git push -u origin markets-rebuild-v8
gh pr create --title "Markets section rebuild v8 — live MCP-backed intelligence" \
             --body-file MARKETS-REBUILD-CHANGELOG.md
```

## Deploy-time risks

- **/api/v1/gdci and /api/v1/news endpoints** — used by the new index + market pages. If either is missing on the Railway backend, the pages render without that section (no hard failure). Verify routing exists before deploy.
- **Worker `/markets/registry.json`** — served as a static file. Make sure Cloudflare Pages ships it (it's in `markets/`). The worker only proxies `/api/*`, so static assets are handled by Pages directly.
- **Dark-theme assumption** — the new CSS uses the existing `--bg: #09090b` palette, consistent with `land-power-map.html`, `gdci.html`, etc. If any embedded iframe expects light mode, check after deploy.

## How to schedule the self-audit

Two options:

1. **Cloudflare Cron Trigger** (recommended — same infrastructure as your existing news cron):
   ```toml
   # wrangler.toml for the API proxy worker
   [triggers]
   crons = ["0 6 * * 1"]   # Mondays 06:00 UTC
   ```
   Port `scripts/self-audit.js` into a cron handler on the worker. The `fetch`-based code is already worker-compatible.

2. **GitHub Actions** (cleanest if you want checked-in reports):
   ```yaml
   # .github/workflows/self-audit.yml
   on: { schedule: [{ cron: '0 6 * * 1' }], workflow_dispatch: {} }
   jobs:
     audit:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-node@v4
           with: { node-version: '20' }
         - run: node scripts/self-audit.js
           env: { DCHUB_API_KEY: ${{ secrets.DCHUB_ENTERPRISE_KEY }} }
         - uses: actions/upload-artifact@v4
           with: { name: audit-report, path: scripts/.audit-report.json }
   ```

The audit's `buildLinkedInPayload` produces `scripts/.linkedin-top10.json`, which drops straight into your existing LinkedIn cron pipeline.

## What's next (not in this PR)

- **GDCI page rebuild** (`/gdci.html`) — same pattern: thin wrapper + live-data render script.
- **Grid Intelligence tool** (`/research/grid-intelligence/`) — interactive ISO picker, queue visualizer, scenario comparator.
- **LinkedIn auto-post integration** — wire `scripts/.linkedin-top10.json` into your existing cron post generator.

---

🤖 Generated with Claude Code
