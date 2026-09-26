<!-- fingerprint:f6ff15756357d5fb5d1dfdc04e9a6776 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — frontend_endpoint_slow (observed at: /snapshot). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100385). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-28T07:11:36.506883Z · inv #100385_

## The approved recommendation

Choose: (a) authorize a re-probe of /snapshot to confirm which backing API/route is currently slow, then apply the routes/deals_routes.py get_public_pipeline SWR-cache pattern to that path as a normal PR (not a mechanical fix); or (b) close this finding as likely stale if the re-probe shows /snapshot now renders under the 5s cap. No find-and-replace remedy is proposed because the responsible file is ambiguous among 5+ candidates.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-49-reliability-brain-finding-frontend-endpoint-s.md`, which stays
OPEN as the single obligation for `frontend_endpoint_slow`. This doc's target —
`/snapshot` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-49-reliability-brain-finding-frontend-endpoint-s.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-49-reliability-brain-finding-frontend-endpoint-s.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-49-reliability-brain-finding-frontend-endpoint-s.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-49-reliability-brain-finding-frontend-endpoint-s.md (class collapse)