<!-- fingerprint:e3c34674d61d460aade889344650ffb1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — plead_product_gap:get_water_risk (observed at: dchub://product-lead/gap/tool/get_water_risk). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100627). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-12T10:56:28.325711Z · inv #100627_

## The approved recommendation

Run `grep -rn "get_water_risk" dchub-mcp-server/ dchub-backend/routes/` to locate the actual handler file and confirm whether the product-lead flag is a conversion/CTA leak (no code fix) or a live runtime error before any remedy is scoped.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100464-plead-product-gap-get-grid-intelligence-observe.md`, which stays
OPEN as the single obligation for `plead_product_gap`. This doc's target —
`get_water_risk @ dchub://product-lead/gap/tool/get_water_risk` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100464-plead-product-gap-get-grid-intelligence-observe.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100464-plead-product-gap-get-grid-intelligence-observe.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100464-plead-product-gap-get-grid-intelligence-observe.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100464-plead-product-gap-get-grid-intelligence-observe.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100464-plead-product-gap-get-grid-intelligence-observe.md (class collapse)