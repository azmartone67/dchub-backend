<!-- fingerprint:130bd3047576694b2675ec16a0fb75df -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: map_layer_outside_coverage_error @ power_plants_nearby@non_us (seen x400)

> Auto-captured from an **approved** brain agenda item (#100249). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-05T17:07:41.851206Z · agenda #100249_

## The approved recommendation

In power_plant_intel.py power_plants_nearby() (around line 269), change the out-of-coverage branch to return HTTP 200 with an empty results array plus a coverage:false metadata field instead of raising the 400 map_layer_outside_coverage_error, and verify against the non-US canary lat=50.1109/lng=8.6821 that the response is now 200-empty.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
