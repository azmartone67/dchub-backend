# Mistral org agent — DC Hub Site Selection Analyst

Created 2026-07-18 **via the Agents API directly** (the Le Chat script path kept
stripping code blocks — this is the documented fallback, and it works end to end).

## What exists

- **Agent:** `DC Hub Site Selection Analyst` — id `ag_019f7477893671388ce6dc871b4cb6e7`,
  model `mistral-medium-latest`, `deployment_chat: true` (PATCHed after create;
  create ignores the field), `source: api`.
- **Connector attached:** `dchub_1` — id `019f69cc-6fac-7569-9e0e-5a069c574bf8`,
  MCP → `https://dchub.cloud/mcp`, `visibility: shared_workspace`, bearer auth with
  stored default credentials.
- **Verified live:** a `/v1/conversations` run with the agent executed
  `dchub_1_get_grid_scoreboard` and answered with cited live data
  ("SPP 37.9% renewable share, as of 2026-07-17 — DC Hub, dchub.cloud").

## The API recipe (what actually works)

1. `GET /v1/connectors` lists workspace connectors — MCP connectors created in
   Le Chat show up here with ids.
2. `POST /v1/agents` with
   `tools: [{"type": "connector", "connector_id": "<id>"}]` attaches one.
   (`type: "mcp"` is rejected; the error message enumerates the valid types —
   `code_interpreter, connector, document_library, function, image_generation,
   web_search, web_search_premium`.)
3. `PATCH /v1/agents/{id} {"deployment_chat": true}` deploys it to Le Chat.

Key: `MISTRAL_API_KEY` on Railway (dchub-backend service).

## Remaining manual step (only if teammates can't see it)

`deployment_chat: true` deploys the agent to Le Chat for the workspace. If it
shows up only for the key owner: Le Chat → Agents → DC Hub Site Selection
Analyst → Share → workspace/org. No API field for per-org visibility was found
on the agent object (the connector is already `shared_workspace`).

## Older duplicates worth pruning by hand

AI Studio playground leftovers with no tools attached: `dchub`
(`ag_019f69cd…`), `DCHUB` (`ag_019f27ec…`, `ag_019c47fa…`), `DCHYB`
(`ag_019c47f8…`). Delete from AI Studio if noise bothers anyone; the API
key can also `DELETE /v1/agents/{id}`.
