<!-- fingerprint:f65a3d64420eaa5cbd344c2f8e73cdee -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — glama lists 0 tool(s) for a remote-capable server — observed from the none seat on registry: glama record `tools` has 0 entries; canon tools/list = 82. Page: https://glama.ai/mcp/servers/qa3uoznre7 What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100080). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-10T19:31:57.780958Z · inv #100080_

## The approved recommendation

Choose the remediation path: (A) if an external unauthenticated initialize+tools/list against the public remote endpoint fails, ship the transport/auth fix first; (B) if it succeeds, skip code changes and just trigger a glama re-scan / re-submit the qa3uoznre7 listing (possibly via glama support); and decide whether to add a recurring registry-freshness check so glama tool-count drift auto-alerts instead of resurfacing as repeat findings.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
