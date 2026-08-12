---
tags: [dchub, architecture, generated]
generated: true
source: scripts/generate_vault_map.py
---

# Brain Layers

> [!warning] Generated file — do not edit by hand
> Re-run `python3 scripts/generate_vault_map.py` after any change to the tree. Hand edits are overwritten, and a hand-maintained map goes stale silently, which is the failure mode this whole map exists to prevent.

20 numbered layers. Each reports in isolation; L14 reads ACROSS them, L18 consolidates outcomes into lessons, and L16 tracks whether past predictions came true.

| layer | module | purpose |
|---|---|---|
| **L5** | `brain_layer5_codegen.py` | Phase r43-B (2026-05-27) — Brain Layer 5 free-form codegen. |
| **L6** | `brain_layer6_predictive.py` | Brain L6 — Predictive (2026-05-19). |
| **L7** | `brain_layer7_evolving.py` | Brain L7 — Self-Evolving (2026-05-19). |
| **L8** | `brain_layer8_orchestrator.py` | Brain L8 — Orchestrator (2026-05-19). |
| **L9** | `brain_layer9_conversational.py` | Brain L9 — Conversational (2026-05-19). |
| **L11** | `brain_layer11_qa_agent.py` | Brain L11 — QA Agent (2026-05-18). |
| **L12** | `brain_layer12_expansion.py` | Brain L12 — Site Expansion Tracker (2026-05-18). |
| **L13** | `brain_layer13_upgrade_nudge.py` | Brain L13 — Upgrade Nudge (2026-05-18). |
| **L14** | `brain_layer14_causal.py` | Brain L14 — Causal Reasoner (2026-05-18). |
| **L14** | `brain_layer14_slo_burn.py` | brain_layer14_slo_burn — auto-file a brain finding when /api/v1/slo/error-budget reports soft_burn or hard_burn. Closes the loop: the next path that starts 5xxing automatically gets investig |
| **L15** | `brain_layer15_auto_action.py` | Brain L15 — Auto-Action (2026-05-19). |
| **L15** | `brain_layer15_tool_calibration.py` | Tool Calibration Drift Detector |
| **L16** | `brain_layer16_self_critique.py` | Brain L16 — Self-Critique (2026-05-19). |
| **L18** | `brain_layer18_memory_consolidation.py` | Brain L18 — Memory Consolidation (2026-05-19). |
| **L19** | `brain_layer19_awareness.py` | Brain L19 — Awareness (2026-05-19). |
| **L20** | `brain_layer20_durability.py` | Brain L20 — Durability Guard (2026-05-19). |
| **L21** | `brain_layer21_autopilot.py` | Brain L21 — Auto-Pilot (2026-05-19). |
| **L22** | `brain_layer22_auto_code.py` | Brain L22 — Auto-Code (2026-05-19). |
| **L22** | `brain_layer22_pr_writer.py` | Phase r57 (2026-05-25). |
| **L23** | `brain_layer23_lifecycle.py` | Phase r35 (2026-05-25). |

Related: [[Architecture Map]], [[Context Integrity]]
