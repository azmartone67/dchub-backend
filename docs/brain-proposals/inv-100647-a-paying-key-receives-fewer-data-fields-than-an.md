<!-- fingerprint:7d0041b22d21c65960eccc4c3f706bc7 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — A paying key receives FEWER data fields than an anonymous caller (9 vs 11) — observed from the paid seat on mcp: paid: 9 data fields / 15707 bytes — anon: 11 data fields / 21965 bytes; present for anon but NOT for paid: ['auto_trial_bind_required', 'continuation']; anon control's... What is the root cause and the small

> Auto-captured from an **approved** brain inv item (#100647). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-15T05:11:22.914210Z · inv #100647_

## The approved recommendation

Capture and diff the full JSON payload bodies of the paid-key vs anonymous MCP response for one identical tool call (e.g. get_facility), field-by-field for the 9 shared fields, and confirm the only paid-side omissions are 'auto_trial_bind_required' and 'continuation'; if confirmed, mark this observation working-as-intended in the investigation log rather than opening a fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
