<!-- fingerprint:d4f73bc1dfd609a7f0eecf38618f706c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — These are the measured, currently-failing critical constraints on DC Hub's actuation loop: 0 real of 2 total relay_opens — every other row is our own probe traffic (human-simulated / dchub-ops-verify). This is the 77→0 cliff, stated exactly. 79/112 drafts (70.5%) in 30d were refuted for citing prior work that is NOT in their own evidence block. The reasoner and critic are fine; the RETRIEVAL step starves them — it cites prior findings by id without inlining their content. 0 MCP-attributed of 9 total conversions/30d — web:pricing-page=6, organic_no_mcp_touch=3. 15 platforms connected and zero agent-attributed dollars: a 16th integration changes nothing until lane 1 closes. Given these, what is the SINGLE highest-leverage change, and what would prove within 7 days that it worked?

> Auto-captured from an **approved** brain inv item (#100018). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-28T07:21:45.831329Z · inv #100018_

## The approved recommendation

Approve prioritizing the retrieval content-hydration fix (inline cited prior-work content into draft evidence blocks) as the ONLY change shipped this week — explicitly deferring the 16th platform integration and all MCP-conversion funnel work — with the 7-day kill/keep gate being: new-draft refutation-for-uncited-prior-work near zero AND ≥1 real non-probe relay_open. If refutation drops but real relay_opens stay at 0, redirect immediately to a relay-lane root-cause investigation instead.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
