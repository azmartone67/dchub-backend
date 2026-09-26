<!-- fingerprint:253a27cfee1a0ec8bb02de825c375c31 -->
# Brain proposal — [reliability] Brain finding: enterprise_bot_present @ mcp_tool_calls: ip_hash=efcc85852e77 (seen x639)

> Auto-captured from an **approved** brain agenda item (#100101). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T20:16:23.942389Z · agenda #100101_

## The approved recommendation

Decide the classification of ip_hash=efcc85852e77 — (a) sanctioned enterprise bot: allowlist it, suppress the finding, and route it into the existing bind_email/paid-key conversion flow; or (b) unwanted traffic: rate-limit/block at the gateway. Separately approve the detector-architecture change from per-event firing to entity-keyed stateful findings across all Brain detectors.

## Rolled-up targets — class `enterprise_bot_present` (class collapse, 2026-09-26)

This doc is now the single obligation for **3 occurrences** of
`enterprise_bot_present`. The other 2 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `mcp_tool_calls: ip_hash=efcc85852e77` — was `agenda-100101-reliability-brain-finding-enterprise-bot-pres.md` (filed 2026-07-15)
- `mcp_tool_calls: ip_hash=ca46aa0b5d1a` — was `agenda-100251-reliability-brain-finding-enterprise-bot-pres.md` (filed 2026-09-06)
- `mcp_tool_calls: ip_hash=ca46aa0b5d1a` — was `inv-100555-enterprise-bot-present-observed-at-mcp-tool-ca.md` (filed 2026-09-07)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
