<!-- fingerprint:f7ae6792d98a63c8382d7cf3c5742ca3 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — A paying key receives FEWER data fields than an anonymous caller (9 vs 10) — observed from the paid seat on mcp: paid: 9 data fields / 14939 bytes — anon: 10 data fields / 22776 bytes; present for anon but NOT for paid: ['continuation']; anon control's remaining budget was not st... What is the root cause and the small

> Auto-captured from an **approved** brain inv item (#100466). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T08:55:43.985796Z · inv #100466_

## The approved recommendation

Diff the actual contents of the 'continuation' field from the anon 22776-byte response payload against the paid response for the same tool call, and grep dchub-mcp-server/server.mjs and the PR #316 diff for the 'continuation' emitter to confirm it holds nudge/upsell messaging rather than a pagination cursor — record the finding in brain_findings before shipping any tier-logic change.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
