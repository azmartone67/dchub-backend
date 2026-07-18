<!-- fingerprint:253a27cfee1a0ec8bb02de825c375c31 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: enterprise_bot_present @ mcp_tool_calls: ip_hash=efcc85852e77 (seen x926)

> Auto-captured from an **approved** brain agenda item (#100114). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-18T17:06:48.584568Z · agenda #100114_

## The approved recommendation

Choose the enforcement stance for high-volume unauthenticated ip_hashes: (A) auto-throttle at a threshold you set, (B) hard-block, or (C) throttle + funnel into bind_email/enterprise-key conversion — and approve the detector change from per-call re-firing to per-entity aggregation with auto-close only on verified enforcement landing.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
