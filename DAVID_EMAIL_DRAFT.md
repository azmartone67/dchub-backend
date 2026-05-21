# Draft response to David Keasey
**Subject:** RE: Marketing opportunities + eyeball numbers for Jarrett

---

David —

Great timing. Pulled the numbers this morning so you have real ammo for the conversation with Jarrett, not vibes. Here's what's defensible:

## The audience, by the numbers

**AI agent reach (the part that's growing fast):**
- **387,567** lifetime requests across **19 distinct AI platforms**
- Last 7 days alone:
    - Claude — 153 sessions
    - Gemini — 173 sessions
    - Copilot — 142 sessions
    - ChatGPT — 47 sessions
    - Plus Perplexity, Grok, Glama, DeepSeek, and 11 others
- **23,009 MCP tool calls in the last 7 days** (~3,300/day) — every one of these is an AI agent pulling DC Hub data on behalf of a real person asking about facilities, markets, M&A, or capacity.

**Why this matters for a pocket listing:** when someone asks ChatGPT or Claude "what's available in Northern Virginia under 50MW?" or "who's selling powered shells in Cheyenne?" — Claude/ChatGPT call DC Hub's `search_facilities` or `get_market_intel` tool. A featured pocket listing in our DCPI dataset is cited verbatim back to the user. That's a placement no banner ad on a static publication can match.

**Human audience (the targeted part):**
- **~70 newsletter subscribers** — industry insiders we've vetted: operators, brokers, analysts, a few hyperscaler infra folks. Two sends/week land in 60-70 inboxes apiece.
- **~14 direct human visitors / 30d** on the public site (honestly small — we're early). Trending up but not where the eyeballs story lives yet.

**Data depth (why people show up at all):**
- 21,382 tracked facilities · 13,512 M&A deals · 1.5M MW pipeline
- 126,085 substations · 52,244 transmission lines · 6,476 fiber routes
- 7 ISOs live · 276 markets covered

**Citation footprint:**
- Source-of-truth score: 35/100 (niche source — visible in the data-center category, not yet mainstream)
- Cited by name in real Claude + Gemini answers (we have receipts at dchub.cloud/cited-by)

## The friends-and-family product I'd put in front of Jarrett

Three tiers, priced for an early-trial cohort:

### 1. **Pocket Listing of the Week** — $1,500 / week (intro F&F rate: $500)
- Top-of-fold placement in the Tuesday + Friday digest (60-70 industry inboxes)
- Featured row in `/pockets` (the public Pocket of Power browser) with "SPONSORED" badge
- **The AI-citation kicker:** the listing's metadata enters our MCP tool catalog for 7 days, so any AI agent asking about that market gets the listing as a top result. We can show the spike in his market's tool-call count post-placement.
- Includes: 1 banner image, 200 words of copy, 3 metrics he chooses (MW, $/kW, T-T-P, etc.), CTA link.
- Reporting: weekly stat sheet — newsletter opens, click-throughs to his listing page, AI agent queries that touched his pocket.

### 2. **Banner in the Weekly Update** — $750 / week (F&F: $250)
- 728×120 banner in the Friday digest header
- Standard newsletter ad, but it lands in a hyper-targeted inbox set
- Click-tracking included

### 3. **Sponsored Anecdote** — $300 per insertion (F&F: $100)
- A "field note" — 80-word write-up of a market shift or a deal pattern, attributed: *"Brought to you by [Sponsor]. Their take: ..."*
- Indexable by AI agents because it sits in our prose feed
- Cheapest entry point — good for a 2-3 placement trial

## What I'd propose for the trial

- **Run one Pocket Listing of the Week placement for Jarrett at $500** (F&F rate, half off).
- I commit to a written readout one week later: newsletter opens, click-throughs, AI tool-call spike against his market. If the numbers don't move, we credit it forward.
- If it works, we package it up and start charging full freight, and you get a referral cut on anyone he sends us.

The thing I'd watch out for: I'd rather underprice the friends-and-family trial than overpromise on eyeball counts and have him feel like he got a bag of vapor. The growth story here is AI agent reach + a tight newsletter — not a CNBC-tier human audience yet. Honest framing keeps the relationship strong if his first placement is more about validation than ROI.

Happy to draft the actual one-pager for Jarrett if you want to push this forward. Just say the word.

— J

---

## Numbers source (so you can verify before sending)

All metrics are pulled live as of {today}:
- AI platform breakdown: `dchub.cloud/api/ai/tracking`
- MCP funnel: `dchub.cloud/api/v1/mcp/funnel`
- Source-of-truth score: `dchub.cloud/api/v1/media/source-of-truth`
- Audience signals: `dchub.cloud/api/v1/audience/summary`
- Site stats: `dchub.cloud/api/v1/stats`

If anything changes materially before you send this, ping me and I'll re-pull.
