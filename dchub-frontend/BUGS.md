# DC Hub — Bug Tracker

_Updated: 2026-04-16_

Maintained by the self-audit job (`scripts/self-audit.js`) and manual review.
Report format: severity · component · observed · expected · repro · suggested fix.

---

## 🔴 Critical

_None currently flagged._

---

## 🟡 Warning

### W-001 · MCP tier-config drift (self-contradicting error payload)

**Observed:** Calling `get_market_intel`, `get_grid_intelligence`, `get_energy_prices`, `get_tax_incentives`, and `get_water_risk` from a Free-tier MCP session returns:

```json
{
  "error": "upgrade_required",
  "message": "🔒 'get_market_intel' requires a Developer license (you're on Free).",
  "current_tier": "Free",
  "required_tier": "Developer",
  "free_tier_tools": "search_facilities, get_facility, list_transactions, get_market_intel, get_news, get_pipeline, get_grid_data, get_grid_headroom, get_grid_intelligence, get_energy_prices, ...",
  "upgrade_url": "https://dchub.cloud/pricing?utm_source=mcp&utm_tool=get_market_intel"
}
```

The same payload that rejects the call **lists the same tool in `free_tier_tools`**. Either:

- The free-tier allow-list is correct and the enforcement code is wrong (should be allowed), **or**
- The enforcement code is correct and the `free_tier_tools` string is out of date.

**Expected:** The `free_tier_tools` advertisement must match the actual enforcement. Users are being told a tool is free while simultaneously being told to pay for it.

**Repro:** Make an unauthenticated or Free-tier MCP call to any tool named in the `free_tier_tools` string.

**Suggested fix:** Single source of truth for the tier matrix. Drive both the allow-list check and the `free_tier_tools` string in the error response from the same constant. Ideal place: `tools/tier-config.ts` (or wherever the Railway backend defines tool access) — export a `TIER_MATRIX` and derive both sides.

**Affected tools (observed):** `get_market_intel`, `get_grid_intelligence`, `get_energy_prices`, `get_tax_incentives`, `get_water_risk`. There may be more.

**Business impact:** Free-tier users get a confusing upgrade prompt for tools the error itself claims are free. Erodes trust in the gating system and the upgrade funnel.

---

### W-002 · Redacted field presented as a string, not null

**Observed:** `search_facilities` response on Free tier:

```json
"power_mw": "🔒 Upgrade to Developer"
```

**Expected:** Either `null` with a redaction flag in `_meta` (preferred), or a numeric placeholder like `-1`. Returning a lock-emoji string inside a numeric field breaks typed clients and any chart/sort that expects a number.

**Suggested fix:** Return `"power_mw": null` with `_meta.fields_redacted: ["power_mw"]` (already present) and let the client decide how to display the upgrade prompt.

**Business impact:** Any downstream analytics or widget that calls `.toFixed()` on `power_mw` throws. Our new market-page.js handles this defensively, but third-party API consumers won't.

---

## 🔵 Info / Watchlist

### I-001 · Markets pages were 100% static with hardcoded numbers

**Observed (before this PR):** All 60 pages under `/markets/` were 90-line HTML files with hand-typed stats that went stale the day they were written. No API integration.

**Fixed:** Rebuilt as thin wrappers over `/js/market-page.js`, which pulls live data from `/api/v1/markets/{slug}`, `/api/v1/grid`, `/api/v1/energy/retail-rates`, `/api/v1/facilities`, `/api/v1/pipeline`, `/api/v1/tax-incentives`, `/api/v1/infrastructure`, `/api/v1/fiber/carriers`, `/api/v1/news`, `/api/v1/gdci`.

### I-002 · `markets/index.html` cards were hardcoded

**Fixed:** Rewritten to merge `/markets/registry.json` (static metadata) with `/api/v1/markets/list` and `/api/v1/gdci?top=10` (live). Supports search, tier chips, and sort by tier/alpha/GDCI/pipeline/price.

### I-003 · No continuous self-audit

**Fixed:** Added `scripts/self-audit.js` — weekly cron that pings every endpoint across all 59 markets, diffs against last snapshot, flags movers, emails report, and produces a LinkedIn Top-10 payload.

---

## How the self-audit populates this file

Run the audit and newly discovered endpoint failures / tier drift are logged in `scripts/.audit-report.json` under `bugs_flagged`. Any entry with `severity: critical | warning` should be promoted to this file. The audit does not auto-rewrite this file — human review is the gate.

```bash
# Manual run
DCHUB_API_KEY=your_enterprise_key node scripts/self-audit.js

# Cron suggestion (weekly, Mon 06:00 UTC)
0 6 * * 1  node /srv/dchub/scripts/self-audit.js >> /var/log/dchub-audit.log 2>&1
```
