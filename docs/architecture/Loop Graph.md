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

| producer | consumer | kind | evidence |
|---|---|---|---|
| `mcp_traffic` | `brain_learn` | probe | code |
| `iso_extract` | `dcpi_recompute` | data | declared |
| `dcpi_recompute` | `auto_press_daily` | data | declared |
| `mcp_traffic` | `engagement_track` | data | declared |

## Source nodes (producer is OUTSIDE the board)

> [!note] A root is not a gap
> These have no upstream loop and never will. They must never be given an edge — an invented edge would be trusted exactly as much as a proven one.

| loop | external producer |
|---|---|
| `mcp_traffic` | external MCP clients (Claude, Cursor, agent directories) |
| `testimonial_ingest` | public HN / Reddit / MCP-directory citations |
| `iso_extract` | ISO grid telemetry pull (GitHub Actions, cron '5,25,45 * * * *') |

Related: [[Architecture Map]], [[Master Shells]]
