<!-- fingerprint:e3ca14a97f279a7dd0f9c4391372c0a2 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: enterprise_bot_present @ mcp_tool_calls: ip_hash=ca46aa0b5d1a (seen x867)

> Auto-captured from an **approved** brain agenda item (#100251). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-06T10:48:24.597436Z · agenda #100251_

## The approved recommendation

Add a UA+ip_hash allowlist classification step to the enterprise_bot_present detector in dchub-backend so that calls matching known probe UAs (e.g. patestautomation-mcp-listability-probe) are marked benign at ingestion and never emitted as a finding; open a PR against dchub-backend that locates the enterprise_bot_present rule (the COUNT(*) over mcp_tool_calls near dchub-backend-fix.py:77-94) and gates it, and confirm ip_hash=ca46aa0b5d1a maps to that UA before merging.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100101-reliability-brain-finding-enterprise-bot-pres.md`, which stays
OPEN as the single obligation for `enterprise_bot_present`. This doc's target —
`mcp_tool_calls: ip_hash=ca46aa0b5d1a` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100101-reliability-brain-finding-enterprise-bot-pres.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100101-reliability-brain-finding-enterprise-bot-pres.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100101-reliability-brain-finding-enterprise-bot-pres.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100101-reliability-brain-finding-enterprise-bot-pres.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of agenda-100101-reliability-brain-finding-enterprise-bot-pres.md (class collapse)