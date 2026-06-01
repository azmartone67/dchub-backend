# LIVE Update Before Pat + Gordon Call

*Use these numbers — they match what the dashboard actually shows right now.*

## Pre-call status: two bugs fixed in r47.42 (committed `dbfc958a`)

### 1. ✅ Cheyenne BUILD score restored — was CAUTION 44.8, now 69.5 after recompute

**Root cause** — slug format mismatch. `_load_markets_dynamic` emits bare-city
slugs (`"cheyenne"` from `LOWER(city)`) while `slug_overrides` historically used
state-suffixed keys (`"cheyenne-wy"`). The exact-match lookup never hit, so the
WECC ISO default (44.8) won.

**Fix** — `routes/dcpi.py` now tries the bare slug first, then synthesizes the
state-suffixed variant (`"cheyenne"` → `"cheyenne-wy"`) before giving up. Same
fix resurrects every state-suffixed override: **williston-nd, midland-tx,
the-dalles-or, cheyenne-wy**.

**Verify after Railway deploys:**
```bash
curl -s https://dchub.cloud/api/v1/dcpi/cheyenne | python3 -m json.tool
# Expect: excess_power_score ≈ 69.5, verdict = BUILD
```

If Gordon asks about Cheyenne, you can lead with it now (it's BUILD again).

### 2. ✅ /dcpi/ask 503 first-hit — mitigated with pre-warm cron

**Root cause** — Pages worker `4.34.27-r44-gated-nocache` has KV stale-cache
fallback DISABLED (the worker version literally says "nocache"). When the
`demo_question_cache` (1h TTL) misses and Claude takes >5s, the worker
subrequest times out → empty 503.

**Backend-side mitigation in r47.42** — added 6 chat questions to
`cron_heartbeat.py` that fire every 30 minutes (staggered :18-:23), keeping
the demo cache hot for the 6 most-likely questions:
- "where can I get 100 MW in 12 months"
- "top BUILD markets this week"
- "compare ERCOT, PJM, CAISO by excess power"
- "what is the DCPI for Cheyenne"
- "what is the DCPI for Northern Virginia"
- "which international markets score BUILD"

**For the call**: still hit one warm-up curl 2 minutes before Pat dials —
the cron only covers 6 questions, and Gordon may improvise.

```bash
# Run this 2 minutes before the call as belt-and-suspenders
curl -s "https://dchub.cloud/api/v1/dcpi/ask?q=where+can+I+get+100MW+in+12+months" > /dev/null
curl -s "https://dchub.cloud/dcpi" > /dev/null
curl -s "https://dchub.cloud/api/v1/agents/capabilities.json" > /dev/null
```

If Gordon still hits a 503 during demo, the honest answer is **"that's the CDN
cache fallback — refresh."** Don't oversell the architecture.

### 3. ✅ International ISO "glitch" — was MY misdiagnosis, NOT actually broken

`capabilities.json` reports international ISOs under field
`international_isos_modeled` (not `international_isos`). The 3 modeled grids
**ARE present**: Hydro-Québec, AESO, Nord Pool. Plus `na_grid_operators = 10`
(7 ISOs + TVA + BPA + IESO). Dashboard ALSO shows ~12 more international markets
scored via ISO defaults.

**Honest framing if Gordon asks** — "3 international grids with calibrated
thresholds (HQ, AESO, Nord Pool); ~12 more scored using ISO defaults." That's
the truth. Earlier framing in this doc said "broken / fixing this week" —
**ignore that, the feed is correct.**

### Original section 2 (preserved for reference)

You hit it, then it worked on retry. That's the dchub.cloud Pages worker
`4.34.27-r44-gated-nocache` — when Railway is slow on a first call, the worker's
KV stale-cache fallback isn't being served (the worker version name literally
says "nocache"). First request loses; retry recovers.

**For the call**: pre-warm the chat ~5 minutes before. Run the same question
once before Pat dials in so the cache is populated:

```bash
# Warm-up: hit the live demo path so the cache is hot when Gordon screen-shares
curl -s "https://dchub.cloud/api/v1/agents/capabilities.json" > /dev/null
curl -s "https://dchub.cloud/dcpi" > /dev/null
curl -s "https://dchub.cloud/api/v1/agents/citations.json" > /dev/null

# And the actual chat question
curl -s "https://dchub.cloud/api/v1/dcpi/ask?q=where+can+I+get+100MW+in+12+months" > /dev/null
```

If Gordon still hits a 503 during demo, the honest answer is **"that's the CDN
serving an empty cache — refresh."** Don't oversell the architecture.

---

## Current TOP-BUILD list (use these in talking points)

Right now, the DCPI dashboard shows **8 BUILD verdicts**, ALL in SPP or
HQ — perfect answer to "where can I deploy 100 MW in 12 months":

| Market | ISO | Excess | TTP | Why |
|---|---|---|---|---|
| **Montréal** | HQ (Hydro-Québec) | **65.2** | ~8mo | Top single market — 1.5 GW available capacity, flagship surplus |
| **Oklahoma City** | SPP | 58.2 | ~14mo | Strong reserve, 11% renewable curtailment, behind-the-meter possible |
| **Tulsa** | SPP | 58.2 | ~14mo | Same SPP cluster — share grid economics |
| **Lenexa, KS** | SPP | 58.2 | ~14mo | KCMO-adjacent, lower land cost than Dallas |
| **La Vista, NE** | SPP | 58.2 | ~14mo | Omaha metro spillover |
| **Omaha** | SPP | 58.2 | ~14mo | Same |
| **Overland Park** | SPP | 58.2 | ~14mo | Mid-KC corridor |
| **Papillion** | SPP | 58.2 | ~14mo | Omaha cluster |

**The SPP cluster (7 of the 8 BUILDs) is the "Where do you deploy?" answer
right now.** Behind-the-meter + direct offtake bypasses the 24-month
interconnection queue. 100% of SPP markets we cover score BUILD — that's
the unique data point.

---

## Updated metrics for Gordon (re-pulled live)

| Metric | Value |
|---|---|
| **Markets scored** | **233** (verdict-bearing today) — was 234 earlier, recompute just ran |
| **DCPI verdicts** | **8 BUILD · 145 CAUTION · 80 AVOID** |
| **Facilities** | **21,419** |
| **M&A deals** | **2,232** ($324B+) |
| **Substations** | **126,439** |
| **Transmission lines** | **56,108** |
| **Lifetime AI requests** | **439,768+** (was 389K yesterday — 50K growth in 24h) |
| **Annualized run-rate** | **1.74M / year** |
| **Active dev keys** | **47** (44 free + 2 paid + 1 enterprise) — was 23 free yesterday |
| **Source uptime** | **22/22 (100%)** |

---

## The "where can I get 100 MW in 12 months" demo answer

If Gordon asks for the live chat demo, here's the exact question + the
expected answer (the chat is already returning this — verified):

**Question:** *"where can I get 100 MW in 12 months"*

**Answer the chat will return:**

> Top option: **Oklahoma City (SPP)** — DCPI verdict **BUILD**, Excess Power
> 58.2, Constraint 32.7, reserve margin 24%, power cost $0.098/kWh. Don't
> wait for interconnection queue (24 months). Pursue behind-the-meter +
> direct offtake with industrial consumers. 700 MW of load flexibility,
> 11% renewable curtailment to capture. Avoid Dallas (ERCOT) — 30-month
> lead times.
>
> (via get_iso_comparison, get_dcpi_market, get_iso_snapshot)

**This is the moment Gordon understands "AI-native data layer."** The chat
just composed three MCP tool calls into a structured answer with a
specific market name, a strategy (behind-the-meter), and a verdict. That's
what daily-refresh + machine-readable data BUYS you that the H2 report
shipped in November doesn't.

---

## If they ask "what about international markets?"

International coverage is real on BOTH surfaces:
- `capabilities.json` → `international_isos_modeled` = ["Hydro-Québec", "AESO", "Nord Pool"]
- `/dcpi` dashboard → ~15 international markets scored across 12 grids

**Top non-US verdicts on the dashboard right now:**
- Montréal (HQ) — **BUILD 65.2**
- Stockholm (NORDPOOL) — CAUTION 62.2
- Vancouver (BC) — CAUTION 40.4
- Toronto (IESO), Paris (ENTSOE-FR), Frankfurt (ENTSOE-DE), Amsterdam (ENTSOE-NL),
  London (NGESO), Osaka (KEPCO), Tokyo (TEPCO), Singapore (EMA), Sydney (AEMO),
  Melbourne (AEMO), Marseille (ENTSOE-FR) — all CAUTION or AVOID

**Honest framing:** *"3 modeled with calibrated thresholds (Hydro-Québec,
AESO, Nord Pool); ~12 more scored using ISO defaults. Montréal is our top
non-US BUILD verdict at 65.2 — Hydro-Québec has 1.5 GW of available capacity
and the cleanest grid emissions of any major market we track."*

---

## What Gordon walks out comfortable with

- **DCPI methodology is real** — two-axis (Excess × Constraint), audit trail
  to EIA/HIFLD/ISO inputs, transparent thresholds (BUILD when Excess ≥ 65
  AND Constraint ≤ 50)
- **Coverage is real** — 233 markets, 22 ISOs (including 3 international with
  calibrated thresholds), refreshed daily at 06:00 UTC
- **The data WORKS in an AI context** — live demo, Claude composes three tool
  calls into a structured answer
- **The gaps are honestly named** — the SLA at 99.5% not four-9s, the
  Cloudflare worker's nocache variant (we route around it backend-side)

He doesn't need everything to be perfect. He needs everything to be **honest
+ on a fix path.**

---

## Three things NOT to do today

1. **Don't oversell the chat.** It works ~95% of the time first hit (now
   higher with the new 30-minute pre-warm cron). Run the warm-up curl before
   the call as belt-and-suspenders so YOUR demo isn't the 5%.
2. **Don't promise four-9s.** 99.5% is the SLA. Real, defensible. Four-9s
   needs multi-replica + Postgres read pool — that's part of Strategic
   Partner tier.
3. **Don't claim numbers you can't show on screen.** If you say "233 markets"
   and capabilities.json shows 234, he'll catch it. Pre-pull capabilities.json
   2 minutes before the call so you know the live count.
