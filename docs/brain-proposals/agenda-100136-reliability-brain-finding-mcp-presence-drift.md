<!-- fingerprint:b36cfe2da57ce0f3f0b02202134cbd15 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_presence_drift_uncorrected @ mcp_presence_listings (seen x4)

> Auto-captured from an **approved** brain agenda item (#100136). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-25T04:09:48.579589Z · agenda #100136_

## The approved recommendation

Choose the root-cause path: (a) invest a human session now to complete DNS-TXT owner verification on the gated registries and approve building a manifest-triggered registry re-publish pipeline, or (b) accept these 4 findings as permanent human-loop items with a periodic manual correction SLA. Also confirm whether the drift check itself is current (vs the press-release conflict prior work flagged) before committing to (a).

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
