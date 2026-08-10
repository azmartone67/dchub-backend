<!-- fingerprint:bebe5064ee6b12a26220e06af5eb885c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — glama advertises 33 tools (canon 82) — observed from the none seat on registry: listing prose says 33 tools; the platform's own live canon says 82 (under) What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100081). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T19:31:54.121015Z · inv #100081_

## The approved recommendation

Choose the fix path: (a) first verify anon tools/list returns 82 and fix the gateway if it's truncated, then re-crawl glama; or (b) skip verification and just trigger a glama re-submit now. Also decide whether to extend the existing regfresh sentinel to diff glama's tool_count against live tools/list so this class of drift is fenced permanently.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
