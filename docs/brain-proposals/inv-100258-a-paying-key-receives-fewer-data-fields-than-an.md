<!-- fingerprint:a680e10609204c220d53bf788a3c7e3f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — A paying key receives FEWER data fields than an anonymous caller (8 vs 9) — observed from the paid seat on mcp: paid: 8 data fields / 14791 bytes — anon: 9 data fields / 17891 bytes; present for anon but NOT for paid: ['machine_pay']; anon control's remaining budget was not stat... What is the root cause and the smalle

> Auto-captured from an **approved** brain inv item (#100258). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-19T05:20:07.866583Z · inv #100258_

## The approved recommendation

Choose between (a) accepting this as intended payer-aware offer gating and closing with a parity doc note after a one-time diff confirms machine_pay is offer-only metadata, or (b) if the diff shows machine_pay contains real data, authorizing a targeted patch to the field gate (likely the PR #203 code path in dchub-mcp-server) so paid seats receive full data while still suppressing offer fields.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
