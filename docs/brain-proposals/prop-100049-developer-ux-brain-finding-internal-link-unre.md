<!-- fingerprint:b1dd768597a9f585f7a651d5fd7c0480 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: internal_link_unreachable @ /pockets/west-des-moines (seen x1)

> Auto-captured from an **approved** brain prop item (#100049). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T02:02:27.185540Z · prop #100049_

## The approved recommendation

Approve (a) a root-cause code fix to pocket-page internal-link generation sourced from the canonical route table, (b) a pre-deploy internal-link validation gate that fails builds on unreachable hrefs, and (c) detector aggregation of internal_link_unreachable by URL pattern — or direct that these remain per-instance patches. Also confirm whether the earlier high-volume spike was the same route bug before closing the recurring-finding theme.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
