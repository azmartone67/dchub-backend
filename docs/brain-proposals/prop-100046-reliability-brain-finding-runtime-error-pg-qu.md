<!-- fingerprint:11a66d56df0d8f81b47d0fb0a2cff63b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: runtime_error:PG query failed, sql snippet: INSERT INTO capacity_pipeline (

> Auto-captured from an **approved** brain prop item (#100046). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:41:12.817048Z · prop #100046_

## The approved recommendation

Approve the structural fix: (1) convert the capacity_pipeline ingest INSERT to an ON CONFLICT upsert (choose DO UPDATE with change tracking vs DO NOTHING), and (2) add operator/market/region key normalization upstream — or alternatively decide to first pull the full schema DDL and the 3 failing sample rows to confirm the conflicting constraint before any code change.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100041-reliability-brain-finding-runtime-error-sync.md`, which stays
OPEN as the single obligation for `runtime_error`. This doc's target —
`[reliability] Brain finding: runtime_error:PG query failed, ` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100041-reliability-brain-finding-runtime-error-sync.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100041-reliability-brain-finding-runtime-error-sync.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100041-reliability-brain-finding-runtime-error-sync.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100041-reliability-brain-finding-runtime-error-sync.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of prop-100041-reliability-brain-finding-runtime-error-sync.md (class collapse)