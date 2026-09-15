<!-- fingerprint:3c101c5570692217deec57cb9957576d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Quota meter does NOT move while it still has room to — observed from the anon seat on mcp: ai_capacity_index: call 1 remaining_full_today=2 ({'limit': 5}) then call 2 remaining_full_today=2 ({'limit': 7}) — IDENTICAL despite budget remaining What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100659). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-15T23:35:28.553707Z · inv #100659_

## The approved recommendation

Open routes/ai_capacity_index and inspect the handler that builds structuredContent.remaining_full_today: confirm whether remaining_full_today is emitted as a constant/default or read from the same quota object that supplies `limit`, then change it to compute remaining = limit − consumed_today from that single live quota state.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
