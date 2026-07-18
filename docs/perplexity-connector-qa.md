# Perplexity connector QA checklist (one page)

Source: platform QA session 2026-07-18. Goal: verify DC Hub connector calls in
Perplexity are REAL tool invocations, not web answers that happen to cite us.

## Evidence standard

A true DC Hub call shows **both** a DC Hub citation in the answer **and** a
connector-invocation event in Perplexity's audit logs. Citation without a
connector event = the model answered from retrieved web content (our SEO/GEO
surface working, but not the connector).

## Procedure

1. In Perplexity: Settings → Connectors → add DC Hub, MCP server URL
   `https://dchub.cloud/mcp` (auth blank for free tier, or
   `Authorization: Bearer <dch_live_… key>`). Recipe: dchub.cloud/integrations/perplexity
2. Run the six starter prompts (one per recipe): market_selection,
   grid_and_queue, water_risk, whats_changed, site_analysis,
   hyperscaler_activity.
3. Export the session's audit logs (supported plans expose connector usage).
4. Tag each prompt:

| Prompt | DC Hub citation? | Connector event in logs? | Label |
|---|---|---|---|
| 1–6 | y/n | y/n | `connector_called` / `web_only` / `locked_preview` |

- citation + event → `connector_called` ✅
- citation, no event → `web_only` (GEO reach, not the connector)
- gated/preview response → `locked_preview` (expected on free tier for paid tools)

## What we act on

- `web_only` majority → connector selection problem: tighten tool descriptions
  for the "perplexity" lane in the tool tuner (citation-title phrasing).
- `locked_preview` on the wrong tools → tier-gating review.
- Verified `connector_called` session → the screenshot/log is the BD evidence
  for the curated App Connectors list submission.
