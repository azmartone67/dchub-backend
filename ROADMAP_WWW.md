# Phase WWW+ Roadmap — fully autonomous, never stale, source of truth

User vision (verbatim 2026-05-16):
> "i want the industry to use us as the source !!!! thank you for helping
> make our site fully autonomous, never stale, error free, and learning,
> and becoming more critical for the industry as the truth for data
> center and energy."

## What WWW shipped today

- **Site Sentinel** — `routes/site_sentinel.py` polls ~48 public URLs every
  radar cycle, persists results, surfaces unhealthy pages as brain findings
  (`site_sentinel_unhealthy:<path>`) so every breakage hits the heartbeat.
  Dashboard at `/sentinel`. JSON at `/api/v1/sentinel/scan` + `/findings`.
- **CSP fix** — added `cdn.jsdelivr.net` + `unpkg.com` to connect/script/
  style-src so `/tax-incentives` map (us-atlas) loads. Also re-aligned
  `_headers` CSP with the deployed runtime CSP (was stale — missing
  `plausible.io` + `cdnjs`).
- **Nav tagline** — `DC Hub · The live source of truth`.

## User-reported items now AUTO-DETECTED via Site Sentinel

Once the sentinel scan runs, each of these surfaces as a finding in
`/api/v1/brain/heartbeat`. The brain will flag them every cycle until fixed.

| # | Page | Symptom | Status |
|---|------|---------|--------|
| 1 | `/tax-incentives` | Map blocked by CSP | **FIXED in WWW** (CSP update) |
| 3 | `/capacity-pipeline` | Errors on load | Will surface as Sentinel finding |
| 4 | `/powered-shell` | API `/api/v1/powered-shell/markets` → 503 | Will surface as Sentinel finding |
| 16 | `/research/ercot-batch-zero` | 404 | Will surface as Sentinel finding |

## Items requiring code/data work (queued)

| # | Item | Owner-decision needed |
|---|------|------------------------|
| 2 | `/daily` stale, not picking up new sites | Bump cron freq + add brain detector for daily-report age |
| 5 | `/pocket-listings` + `/sites` missing main nav | Add `<script src="/js/dchub-nav.js" defer></script>` to those templates |
| 6 | `/dc-hub-media` missing nav + needs DCF/DCK press interaction | Same nav fix + add DCF/DCK to press-target list in `dchub_media.py` |
| 7 | `/developers` actively acquiring new AI agents? | Need conversion-funnel for /developers similar to MCP funnel |
| 8 | `/ai-integrations` lots of inactive MCP — auto-enable via brain | Build outbound-ping cron that wakes registered-but-dormant MCP clients |
| 9 | `/ai-deals` stale since 2026-04-26 | Add `ai_deals_stale` brain detector + sourcer |
| 10 | `/ai-inventory` stuck at 12,553 facilities | Audit discovery cron — bump cadence or add sources |
| 11 | `/ai-wars` underutilized — revamp | UX rethink, not a bug |
| 12 | `/assets` stale | Same root cause as #10 (discovery cron) |
| 13 | `/state-of-the-data-center` revamp? | UX rethink |
| 14 | `/cited-by` — show on site + push externally | DC Hub Media campaign |
| 15 | `/system-status` as Pro feature | Pricing decision |
| 16 | Beef up `/research/grid-intelligence` | Content work |
| 17 | Combine `/gdci` with DCPI? | UX decision |
| 18 | Find Gemini quotes for `/testimonials`; cross-pollinate to other AIs | Outreach campaign |
| 19 | Rewrite `/architecture`; replit migration audit; vet Railway/CF/Neon | Architecture review |
| 20 | `/pricing` font + missing pro annual price | CSS + Stripe config |
| 21 | `/about` dynamic via brain refresh | New brain action: rotate about copy |
| 22a | `/advertise` — eyeball + enterprise tracking + banner sales | Build ad-server lite |
| 22b | `/faq` dynamic | New brain action: refresh FAQ from recent support questions |
| 23 | Capture spare capacity from operators + broker fees + promo codes | New product surface |
| 24 | ISO data-center batch tracking; operator build locations on L+P map | New data source ingest |

## Next-phase candidates (XXX onwards)

- **Phase XXX** — Page Staleness Detector. Read the user's "stale" list
  (#2, #9, #10, #12, #13) into a manifest with `max_age_days` per page;
  surface findings when exceeded. Currently the Sentinel only catches
  HTTP failures + body-too-small.
- **Phase YYY** — Nav-injection auditor. Sentinel can fetch each page
  and verify `dchub-nav.js` is loaded (string scan); surface
  `nav_missing:<path>` findings. Fixes #5 and #6 automatically.
- **Phase ZZZ** — Outbound MCP ping cron. Wake dormant agents from
  `mcp_call_log` whose `last_call_at > 14d` with a polite re-discovery
  ping. Closes #8.
- **Phase AAAA** — Acquisition funnel for `/developers`. Mirror the
  MCP conversion funnel: visits → key claimed → first MCP call → 7d
  retention. Closes #7.
- **Phase BBBB** — Spare-capacity surface. New surface organism with
  intake form + broker-credit tracking. Closes #23.

## How to extend the Sentinel manifest

Edit `routes/site_sentinel.py:_MANIFEST`. Each entry is one URL with:
- `path` — the URL path
- `category` — `critical` / `high` / `normal`
- `min_bytes` — fail if response body is smaller than this
- `label` — human-readable name for the dashboard

Add a URL there → next radar cycle picks it up → brain heartbeat
surfaces failures.
