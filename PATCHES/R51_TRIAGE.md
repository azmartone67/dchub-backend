# R51 Triage — User's 33-item Report (2026-05-25)

User pinged a long list of broken/stale items. This doc maps each
item to a root cause + fix path, so r51's master shell can address
the highest-leverage ones and the rest stay tracked (not lost).

## Root-cause summary

**A. Self-DOS** (largest) — internal probes hitting Railway 151k+ req/day
from a single IP, causing platform-wide 503s. r51-A fixes via internal
circuit breaker + edge cache.

**B. Stale data crons** — DCPI markets, /daily, /reports/monthly all
showing old data. Cron either not firing or writing to wrong table.

**C. Missing API endpoints** — /api/v1/facilities/by-market and
/by-provider 404. r51-B adds them.

**D. Frontend nav gaps** — tax incentives, transactions, daily movers
missing from nav.

**E. Integration auth/tokens** — Twitter 401, Bluesky not set, ERCOT 401.

**F. Disagreeing facility count truth** — site says 21,400+, /daily
shows 12,877, Gemini sees 10,700. r51-B adds /api/v1/stats/canonical
as the single source of truth.

---

## 33-item map

| # | Item | Root cause | Fix in r51? | Future fix path |
|---|------|------------|-------------|-----------------|
| 1 | /daily data stale | B (cron) | indirect via canonical count | investigate daily microservice ingestion path |
| 2 | Railway logs noise | E (token auth) | no | refresh ERCOT, Bluesky, Twitter tokens; audit DCHub-Scheduler legacy-key warnings |
| 3 | Render flapping | infra | no (out of scope) | migrate Render off if possible; or accept |
| 4 | MCP funnel — only 9 conversions / 25k calls | conversion UX | no | rework paywall CTA, free-tier preview |
| 5 | DC Hub intelligence data | (healthy) | n/a | working |
| 6 | Worker data 60k invocations | (healthy) | n/a | working |
| 7 | Error monitoring — 61k 4xx, 1.6k 5xx | A | r51-A | resolved by circuit breaker + edge cache |
| 8 | Security data — 266k mitigated | (healthy WAF) | n/a | working |
| 9 | Traffic data — 266k req | (mostly self-traffic) | r51-A | edge cache reduces origin load |
| 10 | Main site / showing map again + 503 | A | r51-A | brain rotates homepage hero |
| 11 | DCPI more important/expansive? | B (cron freshness) | indirect | DCPI freshness watchdog (queued r51-D-followup) |
| 12 | /dc-hub-media empty | (mostly working — degraded distribution) | no | Twitter/Bluesky token refresh |
| 13 | Visitor intelligence incomplete | identity resolution | no | email backfill cron pending |
| 14 | Brain data | (healthy) | n/a | working |
| 15 | Glama listing | E (manual ops) | no | already listed; PATCHES/REGISTRY_SUBMISSIONS_r45/ has the others |
| 16 | DCPI Grand Forks updated 2026-05-11 (2wk stale) | B | no | DCPI cron is firing per /api/v1/cron/dcpi/health — investigate why individual market rows don't refresh |
| 17 | Lightcone MCP gateway add | E (manual) | no | open PR to lightconetech/mcp-gateway with our server card |
| 18 | Bing data 266 citations | (organic growth) | no | submit fresh sitemap entries |
| 19 | /ai improvements | (working) | n/a | already showing 96 platforms / 387k requests |
| 20 | LinkedIn partners post needed | content task | no | next round: write the synopsis |
| 21 | (skipped) | — | — | — |
| 22 | Groq detail tracking | service unavailable on /ai | r51-A | should clear once 503s stop |
| 23 | /reports/monthly missing data + count disagreement | C+F | r51-B (canonical) | front-end needs to fetch /api/v1/stats/canonical |
| 24 | /system-status broken with 503 | A | r51-A | resolved once origin stops 503'ing |
| 25 | /research/grid-intelligence error 1000 | infra (CF error 1000 = bad DNS or origin not reachable) | A | resolved by circuit breaker |
| 26 | Testimonials growth | content | no | seed more AI-citation testimonials |
| 27 | /ai-pipeline broken + ai-inventory 404s | A + C | **r51-B fixes 404s** | facilities/by-market + by-provider live |
| 28 | /ecosystem stale | B (cron) | no | ecosystem cron freshness check |
| 29 | /market-intelligence 503 | A | r51-A | resolved by circuit breaker |
| 30 | /capacity-pipeline slow + 503 | A | r51-A | resolved by edge cache |
| 30b | Transactions missing from nav | D | no | add to navigation HTML |
| 31 | Tax incentives map broken (3rd time) | D | no | NEEDS DEDICATED ROUND — see notes |
| 32 | Daily movers missing from nav | D | no | add to navigation HTML |
| 33 | /map not showing all sites again | A | r51-A | likely 401 on /api/auth/me; circuit breaker reduces origin pressure |

---

## What r51 master shell actually ships

- **r51-A**: Internal-bot circuit breaker (429s our own probes if they
  exceed 30 req/min). Resolves the root cause of items 7, 9, 10, 22,
  24, 25, 27, 29, 30, 33.

- **r51-B**: Adds `/api/v1/facilities/by-market`, `/by-provider`, and
  `/api/v1/stats/canonical`. Resolves 27 directly + provides a single
  truth source that 23 can adopt next.

- **r51-D**: L23 audit gains `internal_bot_storm` dim. Brain notices
  when its own probes are misbehaving — meta-loop closed.

## What r51 explicitly does NOT fix (queued)

1. **#31 Tax incentives map** — needs its own round. Frontend issue,
   3rd time asked. Highest priority for next session.
2. **#16, #28 cron freshness** — DCPI per-market refresh, ecosystem
   cron. Both need their cron yaml audited.
3. **#20 partners LinkedIn post** — content task, take next.
4. **#26 testimonials seed** — content task.
5. **#17 Lightcone MCP PR** — manual workflow.
6. **#30b, #32 nav gaps** — frontend HTML edits.
7. **#13 visitor intel email backfill** — needs admin run of the
   /upgrade-pool/backfill-emails endpoint.

Tracked here so they don't get lost. Next round picks from the top
of "queued" once r51 lands and verifies.
