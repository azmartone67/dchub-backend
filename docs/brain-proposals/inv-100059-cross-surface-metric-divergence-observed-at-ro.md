<!-- fingerprint:f16554695ddc7c58e679e5ba0dd5f407 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — cross_surface_metric_divergence (observed at: routes/quarterly_report.py:67). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100059). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T08:51:23.307522Z · inv #100059_

## The approved recommendation

Approve a small code change to routes/quarterly_report.py that replaces the hardcoded markets literal with a live read of get_canonical_stats()/markets_phrase() (and audit the sibling hardcodes in routes/state_of_power.py, routes/competitive_seo.py, and agent_hub.py in the same PR) — or explicitly accept a literal bump to 320 knowing it will drift again.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100042-cross-surface-metric-divergence-observed-at-ro.md`, which stays
OPEN as the single obligation for `cross_surface_metric_divergence`. This doc's target —
`routes/quarterly_report.py:67` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100042-cross-surface-metric-divergence-observed-at-ro.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100042-cross-surface-metric-divergence-observed-at-ro.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100042-cross-surface-metric-divergence-observed-at-ro.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100042-cross-surface-metric-divergence-observed-at-ro.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of inv-100042-cross-surface-metric-divergence-observed-at-ro.md (class collapse)