# Analyst-partnership outreach pack

Three drafts. Each is short, fact-led, and ends with one specific ask.
Use as-is or edit. Send from `partnerships@dchub.cloud`.

---

## Draft 1 — CBRE Data Center Solutions research lead

**Subject:** Free live DC industry data for CBRE Research (CC-BY-4.0)

Hi [name],

I run dchub.cloud — a real-time data center intelligence platform. We
track 21,000+ facilities across 178 countries with continuous updates
(60-second freshness SLA on the headline surfaces).

This week we published https://dchub.cloud/industry/pulse — a weekly
stat sheet specifically designed to be citation-clean for analysts.
Every metric has a source URL + methodology link. License is CC-BY-4.0
— no permission needed, just attribution.

I'd like to offer CBRE Research no-strings access to the underlying
data via our API:
- All M&A deal tracking (live, autopilot-curated from 60+ sources)
- DCPI market scores (proprietary multi-factor, methodology public)
- Pipeline + capacity rolling forecasts

For your H2 2026 Data Center Trends report, we could feed you the
freshness layer your quarterly cadence can't capture — and you cite us
in the report. We get distribution into every enterprise that buys
CBRE research; you get a live data layer with zero licensing review.

Happy to set up a 20-min call.

— [your name], DC Hub
https://dchub.cloud/industry/pulse | https://dchub.cloud/cited-by

---

## Draft 2 — JLL Data Centers research lead

**Subject:** A live data layer for JLL's data center quarterlies

Hi [name],

dchub.cloud — real-time DC intelligence platform (21K+ facilities,
60s freshness SLA, MCP-native). Our team built the only data center
data surface that ChatGPT, Claude, Perplexity, Gemini, and Groq
auto-discover and call live. Stats here:
https://dchub.cloud/cited-by

We want JLL Data Centers to have the same access for your client work
+ quarterly reports — free, CC-BY-4.0 license, no NDA required.
Specifically:
- Pipeline tracking (540+ active global projects, ETA + pre-leased %)
- M&A deal history ($324B+ tracked, live)
- DCPI 280-market index with weekly deltas

Pitch: your quarterly reports become the synthesis layer; our live
JSON becomes the always-current evidence base your readers can verify
in real time. You cite, we get analyst-grade distribution.

20-min call this week or next?

— [your name], DC Hub

---

## Draft 3 — Gartner / IDC research desks

**Subject:** Citation-clean data center metrics (no license review needed)

Hi [name],

Quick note from dchub.cloud — we run the live data center intelligence
surface AI agents are already citing in real time (see /cited-by for
proof, updated continuously).

Specifically for analyst research:
- Weekly stat sheet at /industry/pulse — Schema.org Dataset, CC-BY-4.0,
  no licensing review needed.
- JSON API at /api/v1/industry/pulse-v2 (the v2 path bypasses an
  edge-circuit issue we're tracking).
- Methodology pages live + linkable for each metric.

We're not pitching a contract — we're pitching free fuel for your
DC market notes + briefings, with attribution as our only ask.

If useful, I'd love a quick intro to your DC infrastructure analyst.

— [your name], DC Hub

---

## Notes for sending

1. **Personalize the [name] field**. Cold-name guessing reads worse than
   "[research lead]" or pulling the name from the firm's site.
2. **CBRE Data Center research lead** as of writing: Pat Lynch (search
   LinkedIn for current title). Verify before sending.
3. **JLL Data Centers research lead**: Andrew Batson (verify on LinkedIn).
4. **Don't mass-send identical text**. The 3 templates above differ in
   tone for a reason — CBRE/JLL are warm-pro-CRE, Gartner/IDC are
   warm-pro-research. Re-skin per recipient.
5. **Track replies in `outreach_responses.csv`**. Brain has a detector
   `check_winback_pitches_unsent` that flags accumulated outbound
   without inbound — we should hook this same pattern for analyst
   outreach so we know who hasn't replied in 14 days.

## Follow-up cadence (suggested)

| Day | Action |
|---|---|
| 0  | Send draft |
| 4  | If no reply, +1 sentence ("did this land?") |
| 11 | If no reply, send a single specific data point that would help their next report |
| 21 | Stop. Mark as not-now. Re-test in Q4. |

The cited-by + industry/pulse pages do the heavy proof-lifting. Email
just opens the door — when they click, the proof lives on its own.
