# LIVE Update Before Pat + Gordon Call

*Use these numbers — they match what the dashboard actually shows right now.*

## Two surprises Gordon could catch — pre-empt them

### 1. Cheyenne is currently CAUTION 44.8, not BUILD 69.5

The override list HAS Cheyenne calibrated for BUILD (reserve 26%, curtailment 12%,
600 MW stranded capacity), but the latest dashboard recompute is showing the
WECC ISO default (44.8) instead of the Cheyenne-specific override. Confirmed bug
in the recompute pipeline — slug_overrides aren't flowing through to the
materialized score.

**If Pat or Gordon ask about Cheyenne:**
> *"Cheyenne is one of our flagged contrarian markets — high reserve margin,
> 12% renewable curtailment, 600 MW of stranded capacity. The current dashboard
> shows it grouped with the WECC ISO average (44.8 / CAUTION) — that's an
> override-pipeline bug I'm shipping a fix for this week. The underlying market
> data still says BUILD; the score materialization is what's lagging."*

**Don't lead with Cheyenne in the call.** Use SPP cities + Montréal — those
ARE showing as BUILD on the dashboard right now (numbers in next section).

### 2. The chat returned HTTP 503 on first hit

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

The capabilities.json says 0 international ISOs ranked today. But the
**dashboard at /dcpi shows them**:
- Montréal (HQ) — BUILD 65.2
- Stockholm (NORDPOOL) — CAUTION 62.2
- Vancouver (BC) — CAUTION 40.4
- Toronto (IESO), Paris (ENTSOE-FR), Frankfurt (ENTSOE-DE), Amsterdam (ENTSOE-NL),
  London (NGESO), Osaka (KEPCO), Tokyo (TEPCO), Singapore (EMA), Sydney (AEMO),
  Melbourne (AEMO), Marseille (ENTSOE-FR) — all CAUTION or AVOID

**Honest framing:** *"International coverage is in the dashboard; the
machine-readable rollup in our capabilities JSON has a known data-quality
issue showing 0 — fixing this week. Live page is the source of truth right
now."*

---

## What Gordon walks out comfortable with

- **DCPI methodology is real** — two-axis (Excess × Constraint), audit trail
  to EIA/HIFLD/ISO inputs, transparent thresholds (BUILD when Excess ≥ 65
  AND Constraint ≤ 50)
- **Coverage is real** — 233 markets, 22 ISOs, refreshed daily at 06:00 UTC
- **The data WORKS in an AI context** — live demo, Claude composes three tool
  calls into a structured answer
- **The gaps are honestly named** — the override-pipeline bug, the international
  rollup glitch, the SLA at 99.5% not four-9s

He doesn't need everything to be perfect. He needs everything to be **honest
+ on a fix path.**

---

## Three things NOT to do today

1. **Don't say Cheyenne** unless Gordon asks. The dashboard contradicts it.
2. **Don't promise the international rollup is fixed.** Honest: this week.
3. **Don't oversell the chat.** It works ~95% of the time first hit. Pre-warm
   before the call so YOUR demo isn't the 5%.
