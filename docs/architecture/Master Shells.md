---
tags: [dchub, architecture, generated]
generated: true
source: scripts/generate_vault_map.py
---

# Master Shells

> [!warning] Generated file — do not edit by hand
> Re-run `python3 scripts/generate_vault_map.py` after any change to the tree. Hand edits are overwritten, and a hand-maintained map goes stale silently, which is the failure mode this whole map exists to prevent.

78 shells. A *master shell* is a read-only diagnostic with lanes; each lane names its actuator and fires nothing.

| shell | purpose | route | registered in | cron | kill |
|---|---|---|---|---|---|
| `actuation` | Actuation Master Shell (#39, 2026-07-28). | `/admin/actuation` | main.py | no | `ACTUATION_SHELL_DISABLE` |
| `adoption` | ADOPTION MASTER SHELL (#52, 2026-08-12). | `/admin/adoption` | main.py | no | `ADOPTION_SHELL_DISABLE` |
| `agent_adoption` | the AI-agent adoption funnel conductor (2026-07-23). | — | main.py | no | `AGENT_ADOPTION_SHELL_DISABLE` |
| `agent_expansion` | Agent Expansion Master Shell (#45, 2026-08-02). | `/admin/agent-expansion` | main.py | no | `AGENT_EXPANSION_SHELL_DISABLE` |
| `agent_onboarding` | AI-platform onboarding master shell (2026-07-03). ===================================================================================== | — | main.py | no | `AGENT_ONBOARDING_MASTER_DISABLED` |
| `agent_pay` | Agent-Pay Master Shell (#34, 2026-07-25). | `/admin/agent-pay-shell` | main.py | no | `AGENT_PAY_SHELL_DISABLE` |
| `agent_retention` | Agent Retention Master Shell (#49, 2026-08-02). | `/admin/agent-retention` | main.py | no | `AGENT_RETENTION_SHELL_DISABLE` |
| `agent_usefulness` | Agent-legibility / usefulness master shell (2026-07-02). ============================================================================================= | — | main.py | no | `AGENT_USEFULNESS_MASTER_DISABLED` |
| `agentic_loop` | DC Hub — AGENTIC LOOP master shell (#65, 2026-08-22). | `/admin/agentic-loop` | main.py | yes | `AGENTIC_LOOP_SHELL_DISABLE` |
| `agentic` | Agentic Master Shell (2026-07-18) — seven agent-native capabilities, one registry, one tick. Repo master-shell pattern (grid-data/depth/monetize): add | — | main.py | no | `AGENTIC_MASTER_DISABLED` |
| `agreement` | Agreement Master Shell (#37, 2026-07-27). | `/admin/agreement` | main.py | no | `AGREEMENT_SHELL_DISABLE` |
| `ai_adoption` | AI-Adoption Master Shell — one orchestrated loop whose single north-star is: *distinct external AI agents that call DC Hub / week*. | — | main.py | no | `AI_ADOPTION_MASTER_DISABLED` |
| `audience` | Audience / top-of-funnel master orchestrator (2026-06-29). ===================================================================================== | — | main.py | no | `AUDIENCE_MASTER_DISABLED` |
| `audit_closure` | Audit Closure Master Shell (#52, 2026-08-07). | `/admin/audit-closure` | main.py | yes | `AUDIT_CLOSURE_SHELL_DISABLE` |
| `backfunnel` | Back-of-Funnel Truth Master Shell (2026-07-06). | `/admin/backfunnel` | main.py | yes | `BACKFUNNEL_DISABLED` |
| `brain_ascension` | Brain Ascension Master Shell (#28, 2026-07-25). | `/admin/brain-ascension` | main.py | yes | `BRAIN_ASCENSION_SHELL_DISABLE` |
| `brain_autonomy` | thinking → ACTING (2026-08-17). | `/admin/brain-autonomy` | main.py | no | `BRAIN_AUTONOMY_SHELL_DISABLE` |
| `checkout_integrity` | Checkout Integrity Master Shell (#47, 2026-08-01). | `/admin/checkout-integrity` | main.py | yes | `CHECKOUT_INTEGRITY_SHELL_DISABLE` |
| `context_integrity` | Context-Integrity Master Shell (#63, 2026-08-11). | `/admin/context-integrity` | main.py | yes | `CONTEXT_INTEGRITY_SHELL_DISABLE` |
| `conversion_loop` | Agent→paid conversion-loop master shell (2026-07-04). ========================================================================================= | — | main.py | no | `CONVERSION_LOOP_MASTER_DISABLED` |
| `coverage` | Coverage & Media Master Shell (2026-07-04). | `/admin/coverage` | main.py | yes | `COVERAGE_SHELL_DISABLED` |
| `data_liveness` | Data Liveness Master Shell — GET /admin/data-liveness tick: /api/v1/admin/data-liveness/master-tick kill: DATA_LIVENESS_SHELL_DISABLE=1 | `/admin/data-liveness` | main.py | no | `DATA_LIVENESS_SHELL_DISABLE` |
| `dcpi_excess` | DCPI Excess-Data Master Shell (#26, 2026-07-24). | `/admin/dcpi-excess` | main.py | no | `DCPI_EXCESS_SHELL_DISABLE` |
| `deepdive` | Deep-Dive Command Deck (2026-07-08 wave). | `/admin/deepdive` | main.py | yes | `DEEPDIVE_DISABLED` |
| `depth` | the self-driving DEPTH ACTUATOR (2026-07-06). | — | main.py | no | `DEPTH_MASTER_DISABLED` |
| `distribution` | the self-driving DISTRIBUTION orchestrator (2026-07-03). | — | main.py | no | `DISTRIBUTION_MASTER_DISABLED` |
| `fix_closure` | Fix Closure Master Shell (#33, 2026-07-26). | `/admin/fix-closure` | main.py | yes | `FIX_CLOSURE_SHELL_DISABLE` |
| `fixwave` | Fix-Wave Master Shell (2026-07-03 deep-dive wave). | `/admin/fixwave` | main.py, cron_heartbeat.py | yes | `FIXWAVE_DISABLED` |
| `flywheel` | Flywheel Master Shell (2026-07-05). | `/admin/flywheel` | main.py | yes | `FLYWHEEL_DISABLED` |
| `freshness` | Freshness Master Shell — GET /api/v1/admin/freshness tick: /api/v1/admin/freshness/master-tick kill: FRESHNESS_SHELL_DISABLE=1 | — | main.py | no | `FRESHNESS_SHELL_DISABLE` |
| `frontend_reliability` | the self-driving FRONTEND-RELIABILITY orchestrator (2026-07-14). | — | main.py | no | `FRONTEND_RELIABILITY_MASTER_DISABLED` |
| `gap` | Gap Master Shell (2026-07-04) ======================================================= | `/admin/gaps` | main.py | yes | `GAPS_MASTER_DISABLED` |
| `graph` | Graph Master Shell (#49) — 2026-08-02. | `/admin/graph` | main.py | no | `GRAPH_SHELL_DISABLE` |
| `graph_spine` | Graph-Spine Master Shell (#36, 2026-07-26). | `/admin/graph-spine` | main.py | no | `GRAPH_SPINE_SHELL_DISABLE` |
| `grid_data` | the self-driving GRID / POWER / GAS aggregation orchestrator (2026-07-03). | — | main.py | no | `GRID_DATA_MASTER_DISABLED` |
| `growth_funnel` | Growth FUNNEL Master Shell (#53, 2026-08-08). | `/admin/growth-funnel` | main.py | yes | `GROWTH_FUNNEL_SHELL_DISABLE` |
| `growth_integrity` | shell #52, GROWTH INTEGRITY. | `/admin/growth-integrity-shell` | main.py | no | `GROWTH_INTEGRITY_SHELL_DISABLE` |
| `growth` | the self-driving GROWTH orchestrator (2026-07-03). | — | main.py | no | `GROWTH_MASTER_DISABLED` |
| `growthfix` | Growth-Loop Fix Master Shell (#26, 2026-07-24). | `/admin/growthfix` | main.py | yes | `GROWTHFIX_SHELL_DISABLE` |
| `handoff_truth` | master shell #44: HANDOFF TRUTH (2026-07-30). | — | main.py | no | `—` |
| `ingestion_freshness` | Ingestion Freshness Master Shell — GET /admin/ingestion-freshness tick: /api/v1/admin/ingestion-freshness/master-tick kill: INGESTION_FRESHNESS_SHELL_ | `/admin/ingestion-freshness` | main.py | no | `INGESTION_FRESHNESS_SHELL_DISABLE` |
| `ingestion_integrity` | can the producers still run? ============================================================================= | `/admin/ingestion-integrity` | main.py | no | `INGESTION_INTEGRITY_SHELL_DISABLE` |
| `integrity` | Integrity Master Shell (#25, 2026-07-24). | `/admin/integrity` | main.py | no | `INTEGRITY_SHELL_DISABLE` |
| `intelligence_expansion` | Intelligence Expansion Master Shell (#31, 2026-07-25). | `/admin/intelligence-expansion` | main.py | yes | `INTEL_EXPANSION_SHELL_DISABLE` |
| `inventory_acquisition` | Inventory-Acquisition Shell (#40, 2026-07-28). | `/admin/inventory` | main.py | no | `INVENTORY_SHELL_DISABLE` |
| `loop_control` | Loop Control Master Shell (#48, 2026-08-02). | `/admin/loop-control` | main.py | yes | `LOOP_CONTROL_SHELL_DISABLE` |
| `loop_flywheel` | Loop & Flywheel Master Shell (#29, 2026-07-25). | `/admin/loop-flywheel` | main.py | yes | `LOOP_FLYWHEEL_SHELL_DISABLE` |
| `media_growth` | the self-driving MEDIA GROWTH MANAGER (2026-07-15). | — | main.py | no | `MEDIA_GROWTH_DISABLED` |
| `media` | the self-driving MEDIA orchestrator (2026-07-03). | — | main.py | no | `MEDIA_MASTER_DISABLED` |
| `metering_honesty` | Metering Honesty Master Shell (#54) — GET /admin/metering-honesty tick: /api/v1/admin/metering-honesty/master-tick kill: METERING_HONESTY_SHELL_DISABL | `/admin/metering-honesty` | main.py | no | `METERING_HONESTY_SHELL_DISABLE` |
| `metric_integrity` | Metric & Automation Integrity Master Shell (#44) — 2026-07-30. | `/admin/metric-integrity` | main.py | no | `METRIC_INTEGRITY_SHELL_DISABLE` |
| `monetization` | the MONETIZE & RETAIN actuator (2026-07-11). | — | main.py | no | `MONETIZE_MASTER_DISABLED` |
| `onboarding` | Onboarding Master Shell (#43) — did the human who just paid us get looked after, end to end? (2026-07-29) | `/admin/onboarding` | main.py | no | `ONBOARDING_SHELL_DISABLE` |
| `payload` | Payload Master Shell (#38, 2026-07-27). | `/admin/payload` | main.py | no | `PAYLOAD_SHELL_DISABLE` |
| `persistence` | Persistence Master Shell (#41, 2026-07-29). | — | main.py | no | `PERSISTENCE_SHELL_DISABLE` |
| `pillars` | Moat Pillars 1-3 Master Shell (2026-07-13). | `/admin/pillars` | main.py | yes | `PILLARS_SHELL_DISABLED` |
| `platform_doors` | Platform-Doors Master Shell (#27, 2026-07-25). | `/admin/platform-doors` | main.py | no | `PLATFORM_DOORS_SHELL_DISABLE` |
| `precision_depth` | the "next tier" grid/gas/fiber depth actuator (shell #24, 2026-07-18). | — | main.py | no | `PRECISION_DEPTH_MASTER_DISABLED` |
| `press_pipeline` | PRESS PIPELINE TRUTH (2026-08-10). | — | main.py | no | `PRESS_PIPELINE_SHELL_DISABLED` |
| `published_truth` | DC Hub — PUBLISHED TRUTH master shell (#54, 2026-08-20). | `/admin/published-truth-shell` | main.py | no | `PUBLISHED_TRUTH_SHELL_DISABLE` |
| `qa_fixwave` | QA Fix-Wave Master Shell #22 (2026-07-16). | `/admin/qa-fixwave` | main.py | yes | `QA_FIXWAVE_DISABLED` |
| `rag` | the self-driving RAG orchestrator (2026-07-04). | — | main.py | no | `RAG_MASTER_DISABLED` |
| `registry_distribution` | Registry Distribution Master Shell — GET /api/v1/admin/registry-distribution tick: /api/v1/admin/registry-distribution/master-tick kill: REGISTRY_DIST | `/admin/registry-distribution` | main.py | no | `REGISTRY_DISTRIBUTION_SHELL_DISABLE` |
| `registry_freshness` | Registry Freshness Master Shell (2026-07-06). | `/admin/registry-freshness` | main.py | yes | `REGISTRY_FRESHNESS_DISABLED` |
| `relay_closure` | master shell #64: RELAY CLOSURE (2026-08-21). | `/admin/relay-closure-shell` | main.py | yes | `RELAY_CLOSURE_SHELL_DISABLE` |
| `reliability` | the self-driving RELIABILITY-RECOVERY orchestrator (2026-07-04). | — | main.py | no | `RELIABILITY_MASTER_DISABLED` |
| `revenue` | Revenue Master Shell (#50) — 2026-08-03. | `/admin/revenue` | main.py | no | `REVENUE_SHELL_DISABLE` |
| `roadmap` | Roadmap Master Shell #23 (2026-07-16). | `/admin/roadmap` | main.py | yes | `ROADMAP_SHELL_DISABLED` |
| `route_auth` | Route-Auth Hardening Master Shell (#41, 2026-07-31). | `/admin/route-auth-shell` | main.py | no | `ROUTE_AUTH_SHELL_DISABLE` |
| `selfheal` | Self-Heal Master Shell (2026-08-12). | — | main.py | no | `SELFHEAL_SHELL_DISABLE` |
| `seven_levers` | Seven Levers Master Shell (#32, 2026-07-25). | `/admin/seven-levers` | main.py | yes | `SEVEN_LEVERS_SHELL_DISABLE` |
| `stability` | DC Hub — STABILITY master shell (#55, 2026-08-20). | `/admin/stability-shell` | main.py | no | `STABILITY_SHELL_DISABLE` |
| `story_debt` | master shell: STORY DEBT (2026-08-17). | `/admin/story-debt` | main.py | no | `STORY_DEBT_SHELL_DISABLED` |
| `surface_integrity` | Surface Integrity Master Shell — GET /api/v1/admin/surface-integrity tick: /api/v1/admin/surface-integrity/master-tick kill: SURFACE_INTEGRITY_SHELL_D | — | main.py | no | `SURFACE_INTEGRITY_SHELL_DISABLE` |
| `surface_truth` | Surface Truth Master Shell (#30, 2026-07-25). | `/admin/surface-truth` | main.py | yes | `SURFACE_TRUTH_SHELL_DISABLE` |
| `thin_content` | Thin-Content Master Shell (2026-08-14). | — | main.py | no | `—` |
| `webmcp` | WebMCP Master Shell (2026-07-11, webmcp-lane). | `/admin/webmcp` | cron_heartbeat.py | yes | `WEBMCP_SHELL_DISABLE` |
| `white_glove_loop` | White-Glove Loop Master Shell (#45) — 2026-07-30. | `/admin/white-glove-loop` | main.py | no | `WHITE_GLOVE_LOOP_SHELL_DISABLE` |

## Overlapping name stems

Not proof of duplication — where to *look* for it.

- `adoption` ↔ `agent_adoption`, `ai_adoption`
- `depth` ↔ `precision_depth`
- `distribution` ↔ `registry_distribution`
- `fixwave` ↔ `qa_fixwave`
- `flywheel` ↔ `loop_flywheel`
- `freshness` ↔ `ingestion_freshness`, `registry_freshness`
- `growth` ↔ `media_growth`
- `integrity` ↔ `checkout_integrity`, `context_integrity`, `growth_integrity`, `ingestion_integrity`, `metric_integrity`, `surface_integrity`
- `onboarding` ↔ `agent_onboarding`
- `reliability` ↔ `frontend_reliability`

Related: [[Architecture Map]], [[Loop Graph]]
