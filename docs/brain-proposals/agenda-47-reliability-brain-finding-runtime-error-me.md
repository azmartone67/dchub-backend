# Brain proposal — [reliability] Brain finding: runtime_error:⚠️ Memory high: <n>MB > <n>MB limit, clearing caches @ dchub:

> Auto-captured from an **approved** brain agenda item (#47). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-02T09:16:46.308801Z · agenda #47_

## The approved recommendation

Approve one of: (a) instrumentation-first plan (memory time-series + per-cache stats + post-clear baseline logging) before any code change — recommended; (b) immediately convert runtime caches to bounded size-aware LRU eviction on the assumption caches are the cause; or (c) raise the memory limit now as a stopgap while (a) runs. Also decide whether to suppress/aggregate this recurring finding in the brain worklist so 32 duplicates stop consuming detector attention.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
