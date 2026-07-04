# DC Hub × Microsoft Copilot Studio — MCP integration runbook (2026-07)

DC Hub's MCP server (`https://dchub.cloud/mcp`, streamable HTTP, protocol
2025-03-26) is wire-compatible with Copilot Studio today — verified live
2026-07-03 (POST initialize → 200 `text/event-stream`, anonymous OK).

## Path 1 — the recommended first-class wizard (no files needed, GA)

1. copilotstudio.microsoft.com → open or create an agent
2. Settings → enable **Generative orchestration** (hard requirement for MCP)
3. **Tools → Add a tool → New tool → Model Context Protocol**
4. Fill in:
   - Server name: `DC Hub Intelligence`
   - Server description: `Live data-center infrastructure intelligence: facility search and scoring across 21,000+ facilities, market power-index rankings, real-time grid telemetry and headroom, energy prices, interconnection queues, fiber, water risk, tax incentives, and tracked M&A.`
   - Server URL: `https://dchub.cloud/mcp`
5. Authentication: **None** works (free tier, 10 calls/day). For full depth pick
   **API key** → Type: `Header` → name: `X-API-Key` — the connection creator
   pastes a DC Hub key (get one at https://dchub.cloud/pricing).
6. Create → *Create a new connection* → Add to agent. Individual tools can be
   deselected on the MCP server settings page.

Prereqs: Microsoft Entra **work** account (personal accounts can't use Copilot
Studio), Copilot Studio license or 60-day trial, a Power Platform environment.

## Path 2 — certifiable cross-tenant custom connector (this folder's YAML)

`dchub-mcp.yaml` here is a Power Platform custom-connector definition
(Swagger 2.0 dialect) with `x-ms-agentic-protocol: mcp-streamable-1.0`.
Import via Power Apps → Custom connectors → New custom connector →
**Import an OpenAPI file**. Use this path only when you want the connector
certified/distributed across tenants; the wizard (Path 1) is otherwise better.

## Agent Store (Microsoft Commercial Marketplace) submission

Store-eligible from Copilot Studio: **custom agents published in multitenant
mode** (public preview — listing shows a Preview banner). Flow:

1. Publish the agent to the *Teams and Microsoft 365 Copilot* channel in
   multitenant mode → Channels → Edit Details → Availability Options →
   export the manifest .ZIP.
2. Hand-edit manifest `validDomains`: include `api.botframework.com` + exactly
   one geo token domain matching your Dataverse geo (US =
   `unitedstates.token.botframework.com`). No wildcards, no Microsoft domains.
3. Partner Center → *Microsoft 365 and Copilot* program → new offer → upload ZIP.

Validation gotchas (policy 1140.9, validation guidelines 2026-05):
- Descriptions must have **no URLs, no emojis, no instructional phrases**
  ("if the user says X") — anywhere in agent/tool/parameter descriptions.
- Value prop must exceed base Copilot (DC Hub's live grid/facility data qualifies).
- Keep commerce/checkout links out of the store-facing toolset (`unlock_more_data`
  should be excluded from the agent's enabled tool list before submission).
- All server calls must come from the publisher's verified root domain
  (dchub.cloud must be domain-verified on the publisher account).

## Owner-gated checklist (can't be automated)

1. Microsoft Entra **work tenant** + account for the business entity
   (M365 Business Basic is enough to start).
2. Copilot Studio license or 60-day trial on that tenant.
3. Partner Center enrollment (Microsoft AI Cloud Partner Program) — business
   verification takes ~3-5 business days.
4. Join the *Microsoft 365 and Copilot* program in Partner Center.
5. Entra publisher verification + verify the dchub.cloud domain.
6. Confirm https://dchub.cloud/privacy and https://dchub.cloud/terms resolve
   (required listing fields).
7. Put a paid reviewer API key in Partner Center test notes (mirror of the
   Claude Directory reviewer key — see mcp_dev_keys `dir-review-*`).
8. Decide monetization: BYOL (users buy keys on dchub.cloud — simplest) vs a
   transactable SaaS offer (needs payout/tax profiles).

Timeline expectation: 2-4 weeks end-to-end with one review iteration
(~3-4 business days per response).
