<!-- fingerprint:0f27c40b02593f6830c1f62d4c50277b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [data_coverage] 4965 verified vs 22001 tracked facilities (17036 in the unverified discovery pile)

> Auto-captured from an **approved** brain agenda item (#100115). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-19T12:11:07.370862Z · agenda #100115_

## The approved recommendation

Approve the sequencing choice: run the US ISO-cluster sweep (PJM first, then WECC) as the primary verification batch, with the Digital Realty/Equinix provider-batch as the parallel second track — or redirect effort to international rate-laggards like Brazil (13% verified) if geographic breadth matters more than absolute conversion volume. Also decide whether provider-name normalization (Equinix dedupe) should ship before the provider batch runs.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
