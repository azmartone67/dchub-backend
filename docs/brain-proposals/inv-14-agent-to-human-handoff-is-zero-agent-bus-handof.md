# Brain proposal — Agent-to-human handoff is zero: agent_bus_handoffs has 0 rows in 14 days even though 65 real agents/week use the MCP server and the 06-28 handoff redesign shipped ONE free taste + ONE $10 human CTA in server.mjs. Is the handoff CTA actually reaching agents in tool responses, are agents relaying it to their humans, and what would make the human show up?

> Auto-captured from an **approved** brain inv item (#14). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T23:52:30.908502Z · inv #14_

## The approved recommendation

Choose the fix order: (A) first repair /mcp.json 5xx and confirm via deploy history that the 06-28 redesign is actually live, then re-measure CTA reach once traffic recovers; or (B) invest now in CTA instrumentation (per-response CTA presence logging + synthetic end-to-end handoff test into agent_bus_handoffs) despite the collapsed traffic. Also decide whether to accept the -96% traffic drop as the primary incident, superseding the handoff question.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
