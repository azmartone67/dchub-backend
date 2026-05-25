# MCP Registry Submission Drafts — Round 45 (2026-05-25)

The L23 lifecycle audit has been flagging `registry_presence` as weak
because we're listed on only 4 of 11 MCP registries (Smithery, Glama,
mcp.so, PulseMCP). The other 7 are form-based or require GitHub PRs.

This directory contains **ready-to-paste submission drafts** for each
pending registry, so a 30-second manual submission closes the gap.

The drafts mirror what `/api/v1/admin/outreach/draft-submissions`
generates dynamically from the live server-card — committing them as
static files for easy review + offline reference.

## Pending registries (in priority order)

| Registry | Type | Action | File |
|----------|------|--------|------|
| awesome-mcp-servers | GitHub PR | Auto via workflow (needs `PR_SUBMIT_TOKEN`) | [awesome-mcp-servers.md](./awesome-mcp-servers.md) |
| MCPHub | Form | Paste at https://mcphub.io/submit | [mcphub.md](./mcphub.md) |
| Lobehub | Form | Paste at https://lobehub.com/mcp/submit | [lobehub.md](./lobehub.md) |
| MCP Hive | Form | Paste at https://mcphive.com/submit | [mcp-hive.md](./mcp-hive.md) |
| ToolHive | Form | Paste at https://toolhive.io/submit | [toolhive.md](./toolhive.md) |
| Yellowmcp | Form | Paste at https://yellowmcp.com/submit | [yellowmcp.md](./yellowmcp.md) |
| Anthropic Connector Directory | Sales contact | Email contact (no public form) | [anthropic-directory.md](./anthropic-directory.md) |

## Live alternative
For an always-fresh version pulling live stats:

```bash
curl -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/outreach/draft-submissions | jq
```

## After submission
Once a registry confirms our listing, run the existing outreach audit:

```bash
curl -X POST -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/outreach/mcp-registry/submit
```

This refreshes the `outreach_submissions` ledger which the L23
lifecycle audit's `_audit_registry_presence` cross-references.
