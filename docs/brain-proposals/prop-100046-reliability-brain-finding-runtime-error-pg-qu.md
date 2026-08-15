<!-- fingerprint:11a66d56df0d8f81b47d0fb0a2cff63b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: runtime_error:PG query failed, sql snippet: INSERT INTO capacity_pipeline (

> Auto-captured from an **approved** brain prop item (#100046). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:41:12.817048Z · prop #100046_

## The approved recommendation

Approve the structural fix: (1) convert the capacity_pipeline ingest INSERT to an ON CONFLICT upsert (choose DO UPDATE with change tracking vs DO NOTHING), and (2) add operator/market/region key normalization upstream — or alternatively decide to first pull the full schema DDL and the 3 failing sample rows to confirm the conflicting constraint before any code change.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
