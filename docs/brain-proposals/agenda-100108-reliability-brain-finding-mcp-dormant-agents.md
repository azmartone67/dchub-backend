<!-- fingerprint:b6a8b5779703aeb9ba1897aaf132e990 -->
# Brain proposal — [reliability] Brain finding: mcp_dormant_agents_present @ /api/v1/bots/dormant (seen x50)

> Auto-captured from an **approved** brain agenda item (#100108). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T20:15:28.789151Z · agenda #100108_

## The approved recommendation

Choose the structural fix: (A) build the agent lifecycle state machine with transition-only alerting plus a substance-gated resolution rule (larger change, stops recurrence structurally), (B) only wire pre-dormancy bind_email nudges for high-usage anonymous agents (smaller change, creates a winback channel but the detector keeps firing), or (C) both, sequenced A-then-B or B-then-A. Also decide whether the 14-day dormancy threshold should be re-validated before any build.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
