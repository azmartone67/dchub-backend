# 📧 READY-TO-SEND: CBRE Data Center Solutions outreach

**Status:** Copy → paste → send. All proof URLs live.

---

## Step 1 — Get the recipient

CBRE Research's data center practice has rotated leads. As of writing
the current contacts most likely to respond on a research-partnership
ask:

- **Pat Lynch** — Senior Managing Director, CBRE Data Center Solutions
  - LinkedIn: https://www.linkedin.com/in/pat-lynch-2068b8/
  - Email pattern: `pat.lynch@cbre.com` (CBRE uses `first.last@cbre.com`)

- **Gordon Dolven** — Director, Americas Data Center Research, CBRE
  - LinkedIn: https://www.linkedin.com/in/gordondolven/
  - Email pattern: `gordon.dolven@cbre.com`

- **Andrea Cross** — Americas Head of Office Research at CBRE (sometimes
  fields DC research requests)
  - Email pattern: `andrea.cross@cbre.com`

**Recommended:** send to Gordon Dolven first (researcher, will read it).
CC Pat Lynch (decision-maker, won't read but will be aware).

> ⚠️ Verify on LinkedIn before sending — names + roles rotate. If neither
> is in the role any more, search "CBRE Data Center Research" on LinkedIn
> for the current head.

---

## Step 2 — Send this email

**From:** `partnerships@dchub.cloud` (or your founder address)
**To:** `gordon.dolven@cbre.com`
**Cc:** `pat.lynch@cbre.com`
**Subject:** Free live data-center intelligence for CBRE Research (CC-BY-4.0)

---

Hi Gordon,

I run [DC Hub](https://dchub.cloud) — a real-time data center intelligence
platform tracking 21,000+ facilities across 178 countries with continuous
freshness (60-second SLA on the headline surfaces).

I want to offer CBRE Research free, no-strings access to our underlying data —
specifically for the supply, pipeline, and absorption signals that move
between your quarterly reports.

Three things that may be useful:

1. **Weekly stat sheet:** https://dchub.cloud/industry/pulse
   Schema.org Dataset, CC-BY-4.0 (free to cite with attribution, no
   licensing review needed). Designed exactly for analyst use.

2. **Live citation telemetry:** https://dchub.cloud/cited-by
   You can see in real time which AI agents (ChatGPT, Claude, Perplexity,
   Gemini) are calling our MCP server for DC intelligence. We're already
   the source AI agents cite — and that distribution compounds for
   whoever ends up co-authoring with us.

3. **Direct API access:** Free tier of 200 calls/day is yours immediately
   for any CBRE researcher who wants it — `POST https://dchub.cloud/api/v1/keys/claim`
   returns a key in one curl. Documentation:
   https://dchub.cloud/.well-known/mcp.json

**What I'm asking:** a 20-minute call to walk you through what we track,
what's free vs paid, and where our live signals could feed CBRE's
existing quarterly cadence. The pitch is simple — your quarterly report
becomes the synthesis layer; our live JSON becomes the always-current
evidence base your readers can verify in real time. You cite us, we get
analyst-grade distribution.

What's a good time this week or next?

Thanks,
[your name]
[your title], DC Hub
[your phone]
https://dchub.cloud

P.S. — If a partnership is too much, even just an informal "what would
make this most useful for analyst workflows?" call would help us shape
the data presentation. No agenda beyond making this genuinely useful
for research desks like CBRE's.

---

## Step 3 — Track the send

Add a row to `outreach/sent_log.csv` (create if missing):

```csv
date,recipient,company,subject,status,follow_up_date,notes
2026-05-18,gordon.dolven@cbre.com,CBRE,Free live DC intelligence (CC-BY-4.0),sent,2026-05-22,
```

## Step 4 — Day 4 follow-up (if no reply)

**Subject:** Re: Free live data-center intelligence for CBRE Research (CC-BY-4.0)

Gordon — quick bump on the below. Did this land?

If easier than a call, just reply with the single data type that would
be most useful for your next CBRE report (pipeline MW by metro? recent
M&A by quarter? DCPI rankings?). Happy to send a sample JSON pull so
you can see whether it fits your workflow.

— [your name]

---

## Step 5 — Day 11 specific-value follow-up

Send a single concrete data point. Example:

**Subject:** One number that should be in CBRE's next NoVA report

Gordon — saw your most recent NoVA H2 report. One signal we're seeing
that I think is worth a sentence in the next edition:

Per DC Hub's live tracker as of [date]:
- NoVA pipeline: [X GW] across [Y projects] under construction
- [Z]% pre-leased — vs your H1 figure of [N]%
- DCPI score moved from [A] → [B] over the last 30 days (verdict: [BUILD/CAUTION])

Full breakdown + methodology: https://dchub.cloud/industry/pulse

Citation-clean, CC-BY-4.0. Free to use without attribution overhead.

Worth a call to set up a feed for your next report cycle?

— [your name]

---

## Notes

- **Don't BCC people.** Looks spammy.
- **Don't attach anything.** All proof is on URLs.
- **If they reply asking for sample data**, send: ONE specific market
  (their NoVA report area), in clean JSON, fewer than 10 fields. Don't
  fire-hose them with a full pull.
- **If they ask "what's the catch"**, the answer is: we want citation
  + distribution into your enterprise readership. No fees. CC-BY-4.0
  is the license, attribution is the trade.

## Variants for JLL + Gartner

See `cbre_jll_pitch.md` for the JLL + Gartner drafts. Send those AFTER
you see how CBRE responds — the responses will teach us how to refine
the pitch before scaling it.
