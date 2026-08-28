---
tags: [dchub, architecture, generated]
generated: true
source: scripts/generate_vault_map.py
---

# Architecture Map

> [!warning] Generated file — do not edit by hand
> Re-run `python3 scripts/generate_vault_map.py` after any change to the tree. Hand edits are overwritten, and a hand-maintained map goes stale silently, which is the failure mode this whole map exists to prevent.

Entry point for the DC Hub backend. Generated from the tree, so it cannot quietly go stale.

| | count |
|---|---|
| route modules | 770 |
| master shells | 78 |
| numbered brain-layer modules | 20 |
| probed loops | 7 |
| declared loop edges | 4 |
| typed source nodes | 3 |

_Layer modules outnumber layer numbers — L14, L15 and L22 each ship more than one module._

## Notes

- [[Master Shells]] — what each shell is for, and whether anything runs it
- [[Brain Layers]] — L4…L23 and their jobs
- [[Loop Graph]] — producers, consumers, and roots
- [[Context Integrity]] — the envelope, the meter, and the open findings
- [[Admin Cache Leak]] — why `/api/v1/*` reads must be cache-busted

## Before you propose new work

> [!important] Query the fix history first
> ```bash
> curl -sS -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
>   "https://dchub.cloud/api/v1/admin/brain/rag/retrieve?q=YOUR+QUESTION&k=5&corpus=fix_history"
> ```
> Closed issues, fix commits and resolved findings are embedded there. An audit on 2026-08-11 re-proposed three already-shipped capabilities because it grepped the repo instead. Also check `routes/brain_capability_ledger.py`.
