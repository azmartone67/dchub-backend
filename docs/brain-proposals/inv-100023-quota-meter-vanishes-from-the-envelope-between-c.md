<!-- fingerprint:1628136a20d853459cbcd46fae98690d -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Quota meter VANISHES from the envelope between calls — observed from the anon seat on mcp: call 1 published remaining_full_today=0; call 2 published no remaining field at all (envelope keys 17 -> 8). The meter had already reached 0 on call 1, then stopped be... What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100023). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-04T17:57:01.963084Z · inv #100023_

## The approved recommendation

Choose: (1) authorize pulling the two raw envelopes plus quota-manager logs to confirm the state-transition-suppression hypothesis before any code change, or (2) if the pattern is already trusted, approve the minimal patch to dchub-mcp-server's envelope builder (explicit presence check so the quota block — including remaining_full_today: 0 — always serializes in exhausted state) plus a pinned-envelope regression test; and separately decide whether hiding quota fields from exhausted anon seats was ever intended behavior, since that determines whether this is a bug fix or a spec change.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
