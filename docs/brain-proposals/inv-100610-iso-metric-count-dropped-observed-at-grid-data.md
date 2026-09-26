<!-- fingerprint:3b47e92740b4773241479262c47cd081 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_dropped (observed at: grid_data: iso=EPE). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100610). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-11T04:10:28.018341Z · inv #100610_

## The approved recommendation

Pull the last 24h of ingestion logs for iso=EPE from the grid_data collector (start with routes/iso_eu_entsoe.py's ENTSO-E fetch path) and identify which specific metric_name API calls returned errors vs the 2 that succeeded, to confirm whether the failure is per-metric API errors, a mapping gap, or a rate limit before any code change.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100493-iso-metric-count-dropped-observed-at-grid-data.md`, which stays
OPEN as the single obligation for `iso_metric_count_dropped`. This doc's target —
`grid_data: iso=EPE` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100493-iso-metric-count-dropped-observed-at-grid-data.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100493-iso-metric-count-dropped-observed-at-grid-data.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100493-iso-metric-count-dropped-observed-at-grid-data.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100493-iso-metric-count-dropped-observed-at-grid-data.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100493-iso-metric-count-dropped-observed-at-grid-data.md (class collapse)