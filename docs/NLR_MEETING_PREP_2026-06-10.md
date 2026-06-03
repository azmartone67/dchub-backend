# NLR JSC Meeting Prep — Wednesday 2026-06-10, 12-2 PT

**Attendees:** Gabriel Zuckerman, Galen Maclaurin, Ian Christie (NLR) + Jonathan Martone (DC Hub)

**Goal:** Convert NLR's first-week usage of the keys + ask-questions energy into concrete next steps for the partnership. Bring real usage data, real endpoint health, and 2-3 forward-looking proposals.

---

## ⚠️ Key finding from 2026-06-03 pre-meeting audit

The `/api/v1/admin/partner-usage/reveal-nlr` query against the 3 active NLR keys returned **0 calls across all keys**, but root-causing the gap discovered we had **NO per-key usage tracking site-wide** — not just for NLR. `api_usage_meter` was orphaned (the `/track-usage` endpoint existed but was never called) and `api_keys.calls_today/calls_total` were SET=0 at INSERT but never INCREMENTed anywhere in the codebase.

This means **we don't actually know whether NLR used their keys this past week or not.** Gabe's email phrasing ("we've had some time to go through the endpoints") could mean either active API calls OR a thorough read of the docs / OpenAPI spec / dchub.cloud surface. Both are valid.

**Fixed in r78-c (2026-06-03)**: shipped an after-request middleware + background flush thread that captures every keyed API call into `api_endpoint_log` (per-call detail), `api_usage_meter` (per-day rollup), and bumps `api_keys.calls_today/calls_total`. From this point forward we have full per-endpoint visibility for every partner key.

### Meeting talking-point spin
This is actually a **positive story** for the meeting: lead with *"I just shipped real-time usage telemetry today — partnership transparency commitment from MOU Article XIII. From this point forward you can see exactly what you've used, and I can see what's working. Curious to hear from each of you directly — what HAVE you actually been doing with the keys this week?"*

---

## §1 — Pull the actual usage data BEFORE the meeting

I shipped a new admin endpoint just for this. Run from any shell with `DCHUB_ADMIN_KEY` exported:

```bash
curl -sS -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  "https://dchub-backend-production.up.railway.app/api/v1/admin/partner-usage/reveal-nlr?days=14" \
  | python3 -m json.tool
```

Returns (per-key + aggregate):
- `calls_today / calls_week / calls_month / calls_total`
- `first_call_date` + `most_recent_call`
- `active_days` in window
- `daily_calls[]` histogram for last 14 days (override with `?days=N`, max 365)

**Limitation:** `api_usage_meter` tracks per-day totals but NOT per-endpoint breakdown. To learn which specific endpoints they hit, **ask them in the meeting** (it's a great opening question anyway — see §4).

Pre-meeting paste-fill table — drop the JSON output into this template:

| Person | Key prefix | First call | Last call | Calls (total) | Active days (of 14) |
|---|---|---|---|---|---|
| Gabriel | `dchub_developer_jhfHONJx` | _____ | _____ | _____ | _____ |
| Galen | `dchub_developer_iWmhspMS` | _____ | _____ | _____ | _____ |
| Ian | `dchub_developer_jZ6bKqlr` | _____ | _____ | _____ | _____ |

**Engagement signal** (interpretation cheat sheet):
- All 3 active w/ >50 calls each → strong engagement, ready to deepen
- 1-2 active, 1 dormant → likely Ian is doing the bulk integration; Gabe/Galen sampled
- All <10 calls each → either they hit issues OR they're still in planning phase
- One very high (1000+) → automation/loop, possibly Ian's integration script

---

## §2 — Endpoint health verification (probed 2026-06-03)

The 10 endpoints in MOU Schedule A, probed at PJM/Ashburn coords (lat=39.04, lon=-77.48, state=VA — the slide-25 reference site from NLR's proposal):

| Endpoint | HTTP | Latency | Sample output / note |
|---|---|---|---|
| `/api/v1/site-forecast` | ✅ 200 | 1.6s | Composite **75.7** · Grade **A** · 88.5 percentile · 851 facilities w/i 100km · nearest substation 0.8km |
| `/api/v1/grid-intelligence` | ✅ 200 | 1.6s | (rich payload — reserve margin + queue depth) |
| `/api/v1/water/stress` | 🔒 402 anon | 1.4s | **Paywalled anonymously — NLR Developer keys MUST bypass; verify in meeting** |
| `/api/v1/fiber/intel` | ✅ 200 | 1.2s | (per-facility carrier intel) |
| `/api/v1/infrastructure` | ✅ 200 | 2.5s | HIFLD substations + FEMA hazard |
| `/api/v1/tax-incentives` | ✅ 200 | 1.1s | 50-state DC abatements |
| `/api/v1/social-acceptance-index` | ✅ 200 | 1.1s | **Bug: requires lat/lon, NOT state/county** — if Ian tried with state/county he'd have gotten an error |
| `/api/v1/climate-risk` | ✅ 200 | 1.3s | (climate-risk overlay) |
| `/api/v1/carbon-intensity` | ✅ 200 | 1.1s | **Great demo output**: 680 lb/MWh marginal · "40% gas, 31% nuclear, 14% coal, 8% solar, 7% other" · ISO-aware regional breakdown |

**Texas Triangle (ERCOT, Austin, lat=30.27 lon=-97.74 state=TX) — same probes:**

| Endpoint | HTTP |
|---|---|
| `/site-forecast` | ✅ 200 |
| `/grid-intelligence` | ✅ 200 |
| `/water/stress` | 🔒 402 anon (same — verify NLR bypass) |

### Issues to verify with NLR live in the meeting

| # | What to test live | Expected result | If broken |
|---|---|---|---|
| 1 | Ian's key against `/water/stress` | 200 with USGS data | Partner-key validator regression — I fix it that night |
| 2 | Ian's key against `/site-forecast` — check `deployment_forecast` block | Should contain `reference_scenario`/`high_dc_scenario` tables (full 2030-2050), NOT `"upgrade to Pro $199/mo"` | Tier resolution broken for partner keys |
| 3 | Ian's key against `/social-acceptance-index` with lat/lon | 200 with social signal | If he used state/county and got an error, we add a state/county fallback |

---

## §3 — Three NEW flagship features NLR hasn't seen yet

All three shipped in the past 7 days. None were in NLR's Schedule A. Each is directly relevant to reVeal's mission. Demo these to deepen the engagement.

### 3.1 Site Selection Canvas — `/api/v1/site-selection/canvas`

**Pitch:** "We just shipped DC Hub's flagship end-to-end siting product. It returns a ranked shortlist of markets with a `verdict` (BUILD / CAUTION / BLOCK), composite + constraint scores, and time-to-power estimates."

**Anonymous probe just now returned a 152-market shortlist.** Top-3 BUILD markets included Cheyenne, WY (composite 73.1, 10.8 months time-to-power) at the top.

**Why NLR cares:** This is reVeal's end-goal — go from raw inputs to a ranked siting decision. DC Hub now ships a working version. Open question for Galen: *would reVeal want to embed our Canvas verdict as a comparison anchor, or do you want to validate against Canvas as a baseline?*

**Demo move:** call with `?lat=39.04&lon=-77.48&verdicts=BUILD,CAUTION&limit=10` live in the meeting.

### 3.2 Grid + Gas Transition Sentinel (Radar) — `/api/v1/grid-transition/radar`

**Pitch:** "A forward-emergence detector — surfaces grids where data-center deployment is about to outpace transmission planning. The signal NLR currently has to assemble manually across FERC + ISO filings."

**Anonymous probe: 200 OK.**

**Why NLR cares:** The validation paper's most controversial finding is going to be in markets where reVeal projected slow buildout but DC Hub data shows fast buildout (or vice versa). The Transition Sentinel surfaces those gaps automatically.

**Demo move:** show Virginia + Texas + Ohio Radar output side-by-side. Highlight any "emergence" markers that reVeal would currently miss.

### 3.3 Deal Autopsy — `/api/v1/deal-autopsy`

**Pitch:** "Combines M&A deal flow with DCPI grid-reality — answers 'did this acquired site actually have the grid headroom the deal pitch claimed?'"

**Anonymous probe: 200 OK.**

**Why NLR cares:** Acquisition-driven buildout is a major confounder in any siting study. Deal Autopsy gives NLR a way to filter "real organic siting" from "M&A-driven repositioning" — improves the validation paper's signal-to-noise.

**Demo move:** pull a recent high-profile data-center M&A deal and show the grid-reality overlay.

---

## §4 — Questions to ask NLR (in priority order)

Don't bring a script — but these questions surface the info DC Hub needs most:

### A. Engagement intel
1. **Which endpoints did each of you hit?** (We have day counts but not per-endpoint; ask directly.)
2. **What surprised you in a good way?** (Find the moment of conversion.)
3. **What surprised you in a bad way?** (Bugs, missing data, confusing output, paywall hits.)
4. **What did you EXPECT to find that wasn't there?** (Roadmap signal.)

### B. Validation paper scoping (Stream B — MOU Article IX)
5. **Pilot region decision** — DC Hub recommendation: **PJM (Ashburn corridor) + ERCOT (Texas Triangle)**. Their reaction?
6. **Priority limitation** — DC Hub recommendation: **transmission hosting capacity**. Their counter?
7. **Authorship order** — currently *"Galen Maclaurin (NLR), Jonathan Martone (DC Hub), Gabriel Zuckerman (NLR)"* per MOU §9.2. Confirm or revise.
8. **Journal target** — affects cited-endpoint stability commitment (12 months from submission, per Schedule G § G.3).

### C. Integration intel (Ian-focused)
9. **What's the integration shape?** (Direct REST? Python SDK? Are you caching? At what cadence?)
10. **Auth wiring — any friction?** (X-API-Key vs Bearer, header handling, rate limit hits.)
11. **Were any of the 10 endpoint paths a 404 for you?** (Live aliases or paths that need fixing.)

### D. Forward-looking
12. **Do any of these 3 new flagship features (Canvas / Sentinel / Autopsy) fit into reVeal v2?** If yes → that's Stream C work.
13. **DOE prime contract number** (still placeholder in MOU Article I) — need for execution.
14. **NLR Legal status on MOU v1** — any preliminary read from counsel?

---

## §5 — Three strategic proposals to bring

Don't propose them all in 60 minutes. Pick the one that fits the meeting's tone.

### Proposal 1 — Joint demo at a target conference (LOW COST, HIGH VISIBILITY)
Identify one 2026 conference where both reVeal and DC Hub will be present (e.g., DCD Connect, Datacenter Forum, USENIX HotInfra, or NREL/DOE annual). Propose a **joint 30-minute demo** showing reVeal output side-by-side with DC Hub's live data on the same region. Splits the work, multiplies the audience, and gives the validation paper a public preview.

### Proposal 2 — Reciprocal data feed (MEDIUM COST, HIGH STRATEGIC VALUE)
DC Hub gets reVeal's modeled output as a feed (in addition to NLR consuming DC Hub data). Why: we can stand up a `/api/v1/reveal-projected-buildout` endpoint that returns reVeal's projections for any cell. That puts reVeal output directly into DC Hub's MCP server → consumed by Claude, ChatGPT, Perplexity. Massive distribution lift for NLR's research output, and the validation paper now has a public live-comparison view that anyone can query.

### Proposal 3 — Open-method "DC Hub × reVeal Bridge" package (Stream C activation)
Per MOU Article III § 3.3, propose we stand up `dchub-revealkit` (or similar — JSC-agreed name) **immediately after MOU execution**. Apache 2.0 or BSD-3-Clause. Initial committers: Galen + Ian + Jonathan. Initial 6-month scope: water-layer swap, grid-layer enrichment, validation-feed exporter. Lowers the methodology barrier for the next 5 academic groups that want to do reVeal-style work.

---

## §6 — Action items DC Hub takes away (typical post-meeting)

| Action | Owner | When |
|---|---|---|
| Fix any 402/404 reported by Ian | Jonathan | Same day |
| Confirm Developer-key Pro bypass for all paywalled endpoints | Jonathan | Same day |
| Add state/county fallback to `/social-acceptance-index` if Ian asked for it | Jonathan | This week |
| Send post-meeting summary email w/ confirmed pilot region + priority limitation | Jonathan | T+1 day |
| If MOU redlines surfaced, prep v2 | Jonathan | T+3 days |
| Schedule Stream C kickoff if Proposal 3 lands | Both | T+1 week |

---

## §7 — Pre-meeting checklist (Day-of)

- [ ] Pull usage data via `/api/v1/admin/partner-usage/reveal-nlr` (5 min)
- [ ] Fill §1 paste-fill table
- [ ] Verify §2 "Issues to verify with NLR" items DO still reproduce (or have been fixed)
- [ ] Open Site Selection Canvas / Grid Transition Radar / Deal Autopsy in browser tabs ready to demo
- [ ] Open NLR_MOU_v1.docx in case redline questions surface
- [ ] Test Zoom audio/screen-share 10 minutes early
- [ ] Bring: this doc, MOU v1, slide-25 reference (NLR's original proposal PDF)

---

## §8 — File pointers

| Item | Path |
|---|---|
| This briefing | `docs/NLR_MEETING_PREP_2026-06-10.md` |
| MOU v1 (sent) | `docs/NLR_MOU_v1.md` + `.docx` |
| Negotiation Playbook | `docs/NLR_PLAYBOOK.md` |
| Partnership Roadmap | `docs/NLR_PARTNERSHIP_ROADMAP.md` |
| Legal redline notes (historical) | `docs/NLR_LEGAL_REDLINE_NOTES.md` + `.docx` |
| Partner-usage admin endpoint | `routes/partner_key_issuer.py` (r78-a) |
