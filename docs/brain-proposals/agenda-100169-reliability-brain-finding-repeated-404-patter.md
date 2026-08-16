<!-- fingerprint:e9e5800649d004166d7420e9123d6e6c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/v1/infrastructure/transmission (seen x215)

> Auto-captured from an **approved** brain agenda item (#100169). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-05T05:17:11.779051Z · agenda #100169_

## The approved recommendation

Approve the two-part structural fix: (1) ship 404-handler attribution logging (referer/user-agent/origin, sampled) so this and future repeated_404_pattern findings become mappable to a target, then triage /api/v1/infrastructure/transmission based on what it reveals (add endpoint, fix frontend caller, or return 410 to bots); and (2) decide whether to fund the route-manifest CI contract check as the class-level fence, versus continuing per-route alias patches.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/agenda-100139-reliability-brain-finding-repeated-404-patter.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on agenda-100139-reliability-brain-finding-repeated-404-patter.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is agenda-100139-reliability-brain-finding-repeated-404-patter.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to agenda-100139-reliability-brain-finding-repeated-404-patter.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against agenda-100139-reliability-brain-finding-repeated-404-patter.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of agenda-100139-reliability-brain-finding-repeated-404-patter.md (spec-debt sweep #2)