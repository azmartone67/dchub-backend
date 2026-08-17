<!-- fingerprint:253a27cfee1a0ec8bb02de825c375c31 -->
# Brain proposal — [reliability] Brain finding: enterprise_bot_present @ mcp_tool_calls: ip_hash=efcc85852e77 (seen x639)

> Auto-captured from an **approved** brain agenda item (#100101). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T20:16:23.942389Z · agenda #100101_

## The approved recommendation

Decide the classification of ip_hash=efcc85852e77 — (a) sanctioned enterprise bot: allowlist it, suppress the finding, and route it into the existing bind_email/paid-key conversion flow; or (b) unwanted traffic: rate-limit/block at the gateway. Separately approve the detector-architecture change from per-event firing to entity-keyed stateful findings across all Brain detectors.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
