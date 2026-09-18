<!-- fingerprint:dd8ca864d192f28b672baef24c4dc9ee -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: funnel_step_collapse @ funnel:oauth_connector_identity (value 1,000,000)

> Auto-captured from an **approved** brain agenda item (#100268). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T01:00:08.301668Z · agenda #100268_

## The approved recommendation

Open dchub-mcp-server server.mjs, locate _claudeChallengeEligible and the OAuth connector callback that should write a durable identity, and trace why 0 of ~2,458 issued challenges persist an identity — add a landing-verification log at the callback's identity-write step to confirm whether the write is reached or silently failing.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
