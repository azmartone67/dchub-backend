# Phase FF — Source-of-Truth Hardening

**Date:** 2026-05-17
**Owner:** Jonathan
**Status:** Design (pre-implementation)
**Predecessors:** DDDDD (auto-mint trial), EEEEE (anon grace), FFFFF–JJJJJ (autonomy patterns), schema-rescue (#244), three-bugs (#245)

---

## Why now

We've spent ~26 PRs in one session turning DC Hub into an autonomous brain — the *production* side is healthy. The diagnostic that triggered Phase FF: when the user asked "don't we have more than 20K facilities now? I see 12,553," it surfaced that **our own website was lying about us**. Three compounding problems:

1. **API truth vs. frontend display drift.** `/api/v1/stats` was returning the legacy `facilities` table count (12,553) instead of the real `discovered_facilities` count (21,374). Frontend HTML hardcoded everything from 11,000+ to 50,000+ across 59 files. AI agents scraping the page see one number, MCP clients see another.
2. **The actual CF Pages source repo (`azmartone67/dchub-frontend`) had drifted 7 phases behind the backend's checked-in mirror.** Worker live = `4.12.0-ccc`. Local backend mirror = `4.18.0-schema-rescue`. Real frontend repo = `4.12.2-deploy-force`. Result: 9 of 13 critical pages (`/operators`, `/transparency`, `/sentinel`, `/vs`, etc.) 404'd because the live worker didn't know to forward them to Railway.
3. **Brain-radar 404 spam in Railway logs.** Four `_TOOL_API_MAPPING` entries pointed at routes that don't exist (`/api/v1/market-intel`, `/water-risk`, `/grid`, `/intelligence-index`). Self-inflicted log noise.

The brain is alive and the press cadence is healthy, but the **trust surface** — what a curious operator, journalist, or AI sees — was stale. Phase FF closes that gap.

## Shipping in Phase FF (this session)

| # | Change | File / location | Status |
|---|--------|-----------------|--------|
| 1 | `/api/v1/stats.data.total_facilities` flipped 12,553 → 21,374 (PR #245) | `main.py:9596-9607` | ✅ live |
| 2 | `competitor_intel` blueprint rename `_v2` (PR #245) | `routes/competitor_intel.py:31` | ✅ live |
| 3 | `citation_hunter.py` SyntaxWarning fix (PR #245) | `routes/citation_hunter.py:22` | ✅ live |
| 4 | Real frontend `_worker.js` + `_routes.json` sync (11 new paths + prefix matcher), bump to `4.19.0-master-shell` | `~/dchub-frontend/_worker.js`, `~/dchub-frontend/_routes.json` | ⏳ ready to push |
| 5 | `js/live-count.js` — single source of truth, paints `.dc-live-count` + `.dc-live-count-plus` + `[data-stat]` from `/api/v1/stats` | `~/dchub-frontend/js/live-count.js` | ⏳ ready to push |
| 6 | Wire live count on `index`, `ai`, `about`, `ai-facts`, `ai-hub`, `ai-agents`, `agent-hub` | 7 HTML files | ⏳ ready to push |
| 7 | Brain-radar `_TOOL_API_MAPPING` — fix 3 dead paths + remove 1 with no web counterpart | `routes/brain_consistency_radar.py:172` | ⏳ ready to commit |
| 8 | This design doc | `docs/PHASE_FF_DESIGN.md` | ⏳ ready to commit |

## Out of scope for FF (next session candidates)

- **Frontend-mirror auto-sync.** The `dchub-backend/dchub-frontend/` directory drifting from `~/dchub-frontend/` was the *root cause* of #4 above. Options: (a) git submodule, (b) CI job that fails the backend PR if the mirror diverges from the frontend repo's `main`, (c) delete the mirror entirely and edit only the real repo. Recommend (c) — the mirror gives a false sense of progress.
- **Remaining 50+ hardcoded counts in the rest of the frontend HTML.** Phase FF only touches the 7 highest-traffic pages. A follow-up sweep using `live-count.js` on every page that mentions facilities would close the rest.
- **`ai_citations` baseline-seed null platform error.** User mentioned this in Railway logs but search of `routes/ai_citation_tracker.py` and `winback_outreach.py` didn't produce a smoking gun for "null platform". Likely needs an actual Railway log line to chase. Punt until reproducible.
- **Static-meta-description updates on the 49 secondary pages still saying `20,000+`.** Search engines do read these. Could be batched into a single PR with `sed` once the live-count proves out.
- **Phase 282 routing sanity test in CI.** A 30-line GitHub Action that hits every path in `PHASE_282_RAILWAY_PATHS` post-deploy and fails if any returns < 200. Would have caught the worker drift months ago.

## Success metric (measured 24h after deploy)

- Schema saturation score (`/api/v1/schema/audit`) climbs from **15.4% → ≥ 80%** (was blocked by 9 unreachable pages).
- Brain-radar findings drop by 4 "endpoint 404" entries.
- `/api/v1/stats.data.total_facilities` continues to climb (currently 21,374; brain has been adding ~1.5K/wk verified).
- Visible counts on the top 7 pages match `/api/v1/stats` on every refresh (verify in browser).

## What this teaches us

The most embarrassing trust gap wasn't a brain failure — it was a deployment-pipeline blind spot. **The actual CF Pages source was a different repo than the one we'd been editing.** That's worth a runbook entry of its own. Adding a CI guard (next-phase candidate above) prevents recurrence.
