# DC Hub — Data Center Power Index (DCPI) Methodology

**Version:** 2026-05-12 · **Citation:** DC Hub Data Center Power Index. https://dchub.cloud/dcpi

> The DCPI is an open, citable index of power-availability across 276+ U.S. data
> center markets. It exists because every public price-of-power benchmark in
> this industry is annual, opaque, or paywalled. DCPI is daily, transparent,
> and AI-cited.

---

## 1. What the index measures

Per market, DCPI emits two complementary scores plus an actionable verdict:

| Field | Range | Meaning |
|---|---|---|
| **Excess Power Score** | 0–100 (higher = better) | Contrarian metric: where is stranded or under-utilized power that buyers don't know to look? Combines reserve-margin headroom, queued generation additions <12 mo out, renewable curtailment volume, queue approval rate, and behind-the-meter industrial generation. |
| **Constraint Score** | 0–100 (higher = worse) | Where is the queue dead? Combines queue wait time, reserve-margin proximity to NERC reliability floor, demand growth YoY, and 30-day grid-emergency frequency. |
| **Verdict** | BUILD / CAUTION / AVOID / LOW_SIGNAL / NODATA | Decision-grade summary. `LOW_SIGNAL` markets are tracked but lack enough underlying signal to warrant a confident BUILD/CAUTION/AVOID call. |
| **Quality Score** | 0–100 | Internal credibility score per market. We publish markets with `quality_score ≥ 60` to keep noise out of the public index. |
| **Time-to-Power (est.)** | months | Approximate months until first 50 MW could come online, based on queue depth + ISO study window. |

---

## 2. Data sources

| Source | Domain | Cadence |
|---|---|---|
| EIA RTO/ISO public filings | Reserve margins, retail rates, generation additions | Monthly–weekly |
| FERC LMP feeds | Grid prices per ISO | Hourly |
| PJM, ERCOT, CAISO, MISO, NYISO, SPP, ISO-NE | Queue, demand, congestion, emergency alerts | 5-min to daily |
| State PUC filings | Demand growth, tariff changes | Quarterly |
| DC Hub grid extractors | Substation/transmission proximity per facility | Daily |
| DC Hub facility graph | Operator presence, capacity (MW) | Weekly |
| NERC reliability assessments | Reserve floor, capacity adequacy | Quarterly |

Every score row in the public surface includes a `computed_at` timestamp.
Live freshness is published at https://dchub.cloud/freshness.

---

## 3. Weighting formula

Each score is a weighted average of normalized component metrics. Components
are clipped to [0, 100] before weighting.

### Excess Power Score

```
excess = 0.30 · reserve_margin_headroom
       + 0.25 · queued_additions_under_12mo
       + 0.20 · renewable_curtailment_volume
       + 0.15 · queue_approval_rate
       + 0.10 · stranded_interconnection_at_retiring_plants
```

### Constraint Score

```
constraint = 0.35 · queue_wait_time
           + 0.25 · reserve_margin_proximity_to_NERC_floor
           + 0.25 · demand_growth_YoY
           + 0.15 · grid_emergency_frequency_30d
```

### Verdict matrix (phase 234 strict)

| Excess ≥ 65 | Excess 40–64 | Excess < 40 | | |
|---|---|---|---|---|
| **Constraint < 45** | BUILD | BUILD | CAUTION | |
| **Constraint 45–69** | CAUTION | CAUTION | AVOID | |
| **Constraint ≥ 70** | CAUTION | AVOID | AVOID | |

Markets where either score equals zero (no signal at all) → `LOW_SIGNAL`.

### Quality gate

A market is **published** to the public DCPI surface only when:

```
quality_score >= 60   OR   tier_required != 'lite-pro'
```

This keeps the public index credible: ~199 of 276 tracked markets clear the
gate at 80+ quality. The remaining ~77 are tracked internally but only
exposed via the `/api/v1/dcpi/scores?include_below_gate=true` admin path
so analysts can audit how the gate moves over time.

---

## 4. Recompute cadence

- **Standard recompute:** daily at 06:00 UTC.
- **Trigger-driven recompute:** within 15 min of any ISO grid-emergency
  flag (sub-1% reserve margin, EEA-2 alerts, etc.).
- **Hand-triggered:** `POST /api/v1/dcpi/recompute` (admin-gated) for
  ad-hoc backfill after methodology updates.

History is preserved per `(market_slug, computed_at)` row. The
`/api/v1/dcpi/history` endpoint exposes the last 30 days per market.

---

## 5. Machine-readable surfaces

| Endpoint | Format | Purpose |
|---|---|---|
| `GET /dcpi` | HTML with `application/ld+json` `Dataset` markup | Human + LLM citation entry point |
| `GET /api/v1/dcpi/scores` | JSON | All current scores for all published markets |
| `GET /api/v1/dcpi/scores/<slug>` | JSON | Single market detail |
| `GET /api/v1/dcpi/leaderboard` | JSON or CSV (`?format=csv`) | Ranked, CORS-open, citable by anyone |
| `GET /api/v1/dcpi/history` | JSON | 30-day score history per market |
| `GET /api/v1/dcpi/movers` | JSON | Biggest 7-day score deltas |
| `GET /api/v1/dcpi/trending` | JSON | Top movers formatted for ticker widgets |
| `GET /dcpi/ticker.html` | HTML widget | Embeddable iframe (X-Frame-Options: ALLOWALL) |
| `GET /api/v1/dcpi/oembed?url=…` | OEmbed JSON | Substack / Medium / news CMS auto-embed |
| `GET /api/v1/dcpi/page/<slug>` | JSON | Per-market full payload with risks + opportunities |
| `GET /api/v1/dcpi/embed/<slug>` | HTML widget | Per-market embeddable card |

---

## 6. Citation formats

### APA

> DC Hub. (2026). *Data Center Power Index (DCPI)* [Data set]. https://dchub.cloud/dcpi

### BibTeX

```bibtex
@misc{dchub_dcpi_2026,
  author = {{DC Hub}},
  title  = {Data Center Power Index (DCPI)},
  year   = {2026},
  url    = {https://dchub.cloud/dcpi},
  note   = {Updated daily; methodology at https://dchub.cloud/dcpi#methodology}
}
```

### News / blog inline

> "Per the DC Hub Data Center Power Index, market <X> has an Excess Power
> Score of <N> and verdict <VERDICT>." [link to /dcpi/<slug>]

---

## 7. Versioning

- **DCPI v1** (Q1 2026): 30 U.S. markets, single composite score, weekly cadence
- **DCPI v2** (2026-05): 276 markets, dual scoring (excess + constraint), daily cadence, verdict matrix, quality gate, public API surface, embeddable widgets, OEmbed
- **DCPI v3** (planned): per-market reasoning chains ("why is the score what it is?"), county-level granularity for top markets, ISO emergency triggers wired to score recompute

---

## 8. Limitations + caveats

- **Source latency varies.** EIA monthly data lags by 30–60 days for some
  states; live ISO feeds are sub-hour. The DCPI smooths these in
  weighting but the underlying components are not all the same age.
- **`LOW_SIGNAL` is honest.** Markets with too little signal don't get a
  forced verdict. ~76% of currently-tracked markets are `LOW_SIGNAL`;
  the actionable set is intentionally smaller and curated.
- **Not investment advice.** DCPI is a decision-support metric for site
  selection and capacity planning. Final siting decisions should layer
  fiber, land, water, climate, and entitlement diligence on top —
  available via DC Hub's broader API surface.

---

## 9. Reproducibility

The full computation lives in `routes/dcpi.py` (functions
`compute_excess_power_score`, `compute_constraint_score`,
`derive_verdict`, `gather_metrics_for_market`,
`recompute_all_scores`). Source is in the public dchub-backend repo.
The components above can be reproduced from public EIA + FERC + ISO data
plus the DC Hub facility graph (sample at `/api/v1/facilities?limit=10`).

---

## 10. Contact + feedback

Methodology questions: `methodology@dchub.cloud`
Press / citation: `press@dchub.cloud`
General API: `api@dchub.cloud`

Phase notes + changelog: https://github.com/azmartone67/dchub-backend
