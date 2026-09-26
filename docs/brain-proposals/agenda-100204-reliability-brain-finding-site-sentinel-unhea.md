<!-- fingerprint:cd9a79a6414d1d0ec1baa210ba238daa -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: site_sentinel_unhealthy:/mcp#workos-oauth-challenge @ https://dchub.cloud/m

> Auto-captured from an **approved** brain agenda item (#100204). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-17T09:41:31.943129Z · agenda #100204_

## The approved recommendation

Decide the intended steady state: (A) OAuth challenge ON is the invariant — approve pinning DCHUB_OAUTH_CHALLENGE_DISABLE=0 in deployment config plus a CI/boot assertion that anon /mcp initialize must return 401; or (B) challenge OFF is intentional for anonymous agent onboarding — then update the sentinel's expected-state so it stops firing, and close the 200 findings as by-design. Either way, approve adding sentinel finding dedup so one root cause never yields 200 worklist entries.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-84-reliability-brain-finding-site-sentinel-unhea.md`, which stays
OPEN as the single obligation for `site_sentinel_unhealthy`. This doc's target —
`/mcp#workos-oauth-challenge @ https://dchub.cloud/m` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-84-reliability-brain-finding-site-sentinel-unhea.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-84-reliability-brain-finding-site-sentinel-unhea.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-84-reliability-brain-finding-site-sentinel-unhea.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-84-reliability-brain-finding-site-sentinel-unhea.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-84-reliability-brain-finding-site-sentinel-unhea.md (class collapse)