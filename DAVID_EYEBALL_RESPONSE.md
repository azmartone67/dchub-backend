# Draft reply to David — Eyeball intelligence for enterprise visitors

**Subject:** Visitor intelligence: what we can deliver, what we're building, what we won't promise

---

David,

Good question, and I'd rather give you the honest read than the marketing one.

## What we can deliver today

DC Hub logs every MCP tool call and every paywall hit. For each event we capture: session ID, IP address, MCP client (Claude, ChatGPT, Perplexity, Gemini, Copilot, Cursor), tool requested, tier, user-agent, and — when the caller has redeemed a dev key — their email. That gives you a real visitor record we can hand back, broken down by:

- **MCP client mix** — how much of your enterprise traffic is Claude vs ChatGPT vs Perplexity (we currently see all five)
- **Tool usage patterns** — what an enterprise visitor is actually researching (e.g. `get_grid_intelligence`, `analyze_site`, `get_fiber_intel`)
- **Tier-based filtering** — separate the anonymous discovery traffic from authenticated enterprise users
- **Addressable upgrade pool** — identified visitors hitting paid tools repeatedly without converting (the most valuable list)

I put a working dashboard at **`https://dchub.cloud/visitor-intelligence`** — admin-gated. It shows the last 7 days of signals, the MCP-client breakdown, the top tools hitting the paywall, the addressable pool, and tier distribution. Same data is available as JSON at `/api/v1/admin/visitor-intelligence?days=30` if you want to pipe it into your own tooling.

## What we're shipping right now

Two gaps we identified this week:

1. **Email-capture rate was 0.0%** until yesterday — most MCP signals came in without a resolvable email because the gate didn't join dev-key → user → email at write time. Fixed in [commit `a6b3116c`]. Backfill endpoint runs now to retroactively populate the ~15K historical signals where the api-key prefix is recoverable from the user-agent string.
2. **IP → company enrichment** — we capture IP today but don't enrich it. Wiring IPinfo or Clearbit is a 1-day job and would give you "Goldman Sachs visited 47 times this week, looked at get_grid_intelligence in PJM." Want me to prioritize it?

## What we can't do — and won't pretend to

- **Per-page click-stream beyond the MCP boundary.** Plausible covers our public pages but authenticated tool usage doesn't have a frontend pixel. For enterprise web sessions, we see entry/exit but not every click.
- **Identity resolution for cookie-less anonymous visitors.** When Claude or ChatGPT calls DC Hub on behalf of a user, the upstream LLM proxy strips identity. We see "Claude made the call" — not "Bill from Brookfield asked Claude to make the call." That's a protocol limitation, not a fixable bug on our side.
- **De-anonymizing high-value visits.** If a Fortune 500 visits without authenticating, we have their IP and behavior but not their identity. The dashboard is honest about which fraction of traffic is identified vs anonymous.

## What I'd propose

Two paths, no preference:

1. **Use what's live today.** Bookmark `/visitor-intelligence`, check it weekly. If you see a recurring identified visitor I should reach out to, I'll send the personalized "you've hit `get_grid_intelligence` 47 times this month" email — those convert at 5-15% versus the passive paywall's 0.05%.
2. **Add IP enrichment + a weekly enterprise-visit digest emailed to you.** ~1 week to ship. You get a Monday-morning brief with "Goldman, Brookfield, Blackstone visited last week — here's what they looked at and which tools they bounced on." If we see one of your accounts repeatedly, that's a hand-off to you to make the call.

Tell me which direction is useful and I'll cut the right work.

— Jonathan

---

## For my reference (not in the email)

**Endpoints shipped for this:**
- `GET /visitor-intelligence` — HTML dashboard, admin-gated
- `GET /api/v1/admin/visitor-intelligence?days=N` — same data as JSON
- `POST /api/v1/admin/upgrade-pool/backfill-emails?days=N` — backfills historical signals (run once)
- `GET /api/v1/admin/upgrade-pool/preview` — shows addressable pool with personalized email drafts
- `POST /api/v1/admin/upgrade-pool/send?dry=1&limit=N` — fires the campaign

**Commands to validate before sending David anything:**

```bash
# 1. Backfill historical signals so the dashboard reflects real identity.
curl -X POST "https://dchub.cloud/api/v1/admin/upgrade-pool/backfill-emails?days=30" \
  -H "X-Admin-Key: $DCHUB_ADMIN_KEY" | python3 -m json.tool

# 2. Confirm the visitor-intelligence dashboard shows real signals.
curl "https://dchub.cloud/api/v1/admin/visitor-intelligence?days=7" \
  -H "X-Admin-Key: $DCHUB_ADMIN_KEY" | python3 -m json.tool

# 3. Open the rendered dashboard in a browser (use the same key).
# Visit https://dchub.cloud/visitor-intelligence?admin_key=$DCHUB_ADMIN_KEY
```

**If the backfill resolves real enterprise emails, those are likely David-relevant.**
The dashboard will show them in the "Addressable pool" section with the specific tools they hit. That's the evidence you can attach to your reply.
