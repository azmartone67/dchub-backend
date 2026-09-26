<!-- fingerprint:4ef64c207e5e9720fab23e2b81bd5c2c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — iso_metric_count_zero_24h (observed at: grid_data: iso=WAUW). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100507). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-04T04:20:37.874757Z · inv #100507_

## The approved recommendation

Open routes/iso_orchestrator.py and trace the fan-out aggregate code that WAUW registers under to identify its concrete ingest module and scheduler entry, then check that module's last successful run timestamp against the ~35h stall to confirm whether the loop stopped or the upstream feed went silent.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100138-iso-metric-count-zero-24h-observed-at-grid-dat.md`, which stays
OPEN as the single obligation for `iso_metric_count_zero_24h`. This doc's target —
`grid_data: iso=WAUW` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100138-iso-metric-count-zero-24h-observed-at-grid-dat.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100138-iso-metric-count-zero-24h-observed-at-grid-dat.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100138-iso-metric-count-zero-24h-observed-at-grid-dat.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100138-iso-metric-count-zero-24h-observed-at-grid-dat.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100138-iso-metric-count-zero-24h-observed-at-grid-dat.md (class collapse)