---
tags: [dchub, architecture, generated]
generated: true
source: scripts/generate_vault_map.py
---

# Loop Graph

> [!warning] Generated file — do not edit by hand
> Re-run `python3 scripts/generate_vault_map.py` after any change to the tree. Hand edits are overwritten, and a hand-maintained map goes stale silently, which is the failure mode this whole map exists to prevent.

Shell #49's graph. Every row of `/api/v1/system/loops` carries `input_status`, so a loop running on dead input can no longer report a green board.

## Probed loops

`auto_press_daily`, `brain_learn`, `dcpi_recompute`, `engagement_track`, `iso_extract`, `mcp_traffic`, `testimonial_ingest`

## Declared edges (producer → consumer)

_none readable_

## Source nodes (producer is OUTSIDE the board)

> [!note] A root is not a gap
> These have no upstream loop and never will. They must never be given an edge — an invented edge would be trusted exactly as much as a proven one.

_none declared_

Related: [[Architecture Map]], [[Master Shells]]
